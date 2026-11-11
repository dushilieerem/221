"""策略回测执行编排。

向量化和事件驱动策略都通过这里完成源码/目录资产解析、行情准备、
QuantCore 引擎调用以及 API 响应组装。``runner.py`` 仅保留兼容门面，
历史调用路径和响应契约不变。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

import pandas as pd

from app.services.factor.backtest_support import (
    _StrategyPanels,
    _asset_meta,
    _benchmark_equity,
    _load_benchmark_equity,
    _resolve_research_assets,
    _resolve_strategy_symbols,
)
from app.services.factor.result_serializers import (
    _compute_annual_returns,
    _compute_drawdowns,
    _compute_equity_with_drawdown,
    _sample_positions,
    _serialize_event_execution_timeline,
    _serialize_rebalance_logs,
    _serialize_vectorized_execution_timeline,
)
from app.schemas.backtest import (
    DateRange,
    EventFillInfo,
    EventOrderInfo,
    EventPositionSnapshot,
    StrategyBacktestMeta,
    StrategyBacktestRequest,
    StrategyBacktestResponse,
)
from app.services.factor.serializers import date_range_from, metrics_to_dict


def _instantiate_vectorized_strategy(strategy_class, panels):
    """按策略构造函数的行情字段参数注入向量化面板。

    旧适配器无论策略实际需要什么都只传 ``close``，导致同时依赖 OHLC、成交额
    或市值的结构化策略无法启动。字段名与构造函数参数名一致时按需懒加载；有
    默认值的非行情参数继续由策略自身提供。
    """

    try:
        parameters = inspect.signature(strategy_class.__init__).parameters.values()
    except (TypeError, ValueError):
        return strategy_class(close=panels.close)

    positional: list[object] = []
    keyword: dict[str, object] = {}
    missing: list[str] = []
    for parameter in parameters:
        if parameter.name == "self":
            continue
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if parameter.name in panels:
            value = panels[parameter.name]
        elif parameter.default is not inspect.Parameter.empty:
            continue
        else:
            missing.append(parameter.name)
            continue

        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            keyword[parameter.name] = value

    if missing:
        raise TypeError(
            f"{strategy_class.__name__} 缺少可注入的行情字段参数: {', '.join(missing)}"
        )
    return strategy_class(*positional, **keyword)


def _strategy_run_identity(req: StrategyBacktestRequest, data_engine) -> dict[str, str | None]:
    """本次回测绑定的数据发布与规格指纹，供报告自证可复现。

    与因子回测同口径：``spec_hash`` 覆盖请求参数与数据发布，不含运行期 id。
    """

    release_id = getattr(data_engine, "release_id", None) if data_engine is not None else None
    payload = {
        "request": req.model_dump(mode="json"),
        "data_release_id": release_id,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"data_release_id": release_id, "spec_hash": digest}



def _factor_refs_code_hash(refs) -> str:
    """factor_refs 模式的稳定代码身份，进入响应 meta 与 spec_hash。"""

    payload = [
        {"library": ref.library, "factor": ref.factor, "direction": ref.direction}
        for ref in refs
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _build_factor_refs_strategy(refs, top_n: int = 30, rebalance_days: int = 20):
    """用内置因子合成 ``@signal`` 分数，复用 FactorPortfolioStrategy 的选股/调仓。

    只接受库名+因子名；因子通过当前 SQLite 内置因子加载器解析，不执行调用方
    源码。方向 negative 取负号，各因子先做截面 zscore 再等权合成。
    """

    from app.models.factor_backtest import FactorBacktestSpec
    from app.services.factor.loader import load_factor_from_spec
    from app.services.strategy.vectorized.factor_strategy import (
        EveryNTradingDays,
        FactorPortfolioStrategy,
        TopN,
        signal,
    )

    loaded = []
    for ref in refs:
        try:
            factor_obj = load_factor_from_spec(
                spec=FactorBacktestSpec(
                    factor_name=ref.factor,
                    library_names=(ref.library,),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成 422 可读错误
            raise ValueError(
                f"内置因子加载失败: {ref.library}/{ref.factor}: {exc}"
            ) from exc
        loaded.append((factor_obj, -1.0 if ref.direction == "negative" else 1.0))

    class FactorRefsStrategy(FactorPortfolioStrategy):
        selector = TopN(top_n)
        rebalance = EveryNTradingDays(rebalance_days)

        @signal
        def composite_score(self):
            parts = []
            for factor_obj, sign in loaded:
                values = factor_obj.compute(self._panel)
                if not isinstance(values, pd.DataFrame):
                    raise TypeError(
                        f"内置因子 {factor_obj.name!r} 必须返回日期×标的 DataFrame"
                    )
                if sign < 0:
                    values = -values
                parts.append(self.zscore(values))
            total = parts[0]
            for part in parts[1:]:
                total = total + part
            return total / float(len(parts))

    return FactorRefsStrategy


def run_strategy_backtest(
    req: StrategyBacktestRequest,
    *,
    data_engine=None,
) -> StrategyBacktestResponse:
    """策略回测：自定义策略代码 -> 指标 + 净值。"""
    try:
        from app.services.strategy.vectorized.registry import get_backtest_engine
        from app.models.strategy import BacktestConfig, BacktestSignals

        if data_engine is None:
            raise RuntimeError("StrategyExperiment 必须提供 DataEngine")
        universe, universe_snapshot, benchmark_snapshot = _resolve_research_assets(
            req,
            data_engine=data_engine,
        )

        strategy_id = None
        strategy_version = None
        code_hash = None
        strategy_code = req.strategy_code
        expected_entrypoint = None
        if req.factor_refs is not None:
            strategy_class = _build_factor_refs_strategy(
                req.factor_refs,
                req.top_n or 30,
                req.rebalance_days or 20,
            )
            code_hash = _factor_refs_code_hash(req.factor_refs)
        else:
            if req.strategy_id is not None:
                from app.services.catalog.strategy_catalog import StrategyCatalog

                asset = StrategyCatalog().load(req.strategy_id, req.strategy_version)
                if asset.execution_mode != req.engine_mode:
                    raise ValueError(
                        f"策略 {req.strategy_id} 的 execution_mode 为 {asset.execution_mode}，"
                        f"与请求 engine_mode={req.engine_mode} 不一致"
                    )
                strategy_code = asset.source_code
                strategy_id = asset.strategy_id
                strategy_version = asset.version
                code_hash = asset.code_hash
                expected_entrypoint = asset.entrypoint

            # 直接提交源码时也要给出可复现凭证：与策略目录同口径，对源码
            # UTF-8 字节做 SHA-256；否则聚宽直跑结果的 meta.code_hash 为 null。
            if code_hash is None and strategy_code is not None:
                code_hash = hashlib.sha256(strategy_code.encode("utf-8")).hexdigest()

            # 聚宽方言：模块级 initialize + 兼容层，不要求策略类。
            if req.strategy_dialect == "joinquant":
                from app.services.security.code_guard import assert_safe_user_code
                from app.services.strategy.compat import load_joinquant_strategy
                from app.services.strategy.compat.factors import FactorStore
                from app.services.strategy.compat.finance import FinanceNamespace
                from app.services.strategy.compat.fundamentals import FundamentalStore
                from app.services.strategy.compat.index import (
                    IndexMembership,
                    IndexPriceStore,
                )
                from app.services.strategy.compat.industry import IndustryStore
                from app.services.strategy.compat.loader import strip_joinquant_imports
                from app.services.strategy.compat.master import SecurityMaster

                cleaned = strip_joinquant_imports(strategy_code)
                if req.strategy_id is None:
                    assert_safe_user_code(cleaned, kind="策略")
                compat_data_dir = getattr(data_engine, "data_dir", None)
                # 新发布视图在 <materialized>/free/compat 提供聚宽兼容目录树；
                # 没有时回退到 MODELSCOPE_ROOT / staging。
                compat_root = None
                if compat_data_dir is not None:
                    candidate = Path(compat_data_dir).parent / "free" / "compat"
                    if candidate.is_dir():
                        compat_root = candidate
                fundamentals = (
                    FundamentalStore(compat_root)
                    if compat_root is not None
                    else FundamentalStore()
                )
                factors = (
                    FactorStore(root=compat_root, data_engine=data_engine)
                    if compat_root is not None
                    else FactorStore(data_engine=data_engine)
                )
                strategy_obj = load_joinquant_strategy(
                    strategy_code,
                    fundamentals=fundamentals,
                    factors=factors,
                    master=(
                        SecurityMaster(compat_root)
                        if compat_root is not None
                        else None
                    ),
                    finance=(
                        FinanceNamespace(compat_root)
                        if compat_root is not None
                        else None
                    ),
                    index=(
                        IndexMembership(compat_data_dir)
                        if compat_data_dir is not None
                        else None
                    ),
                    industry=(
                        IndustryStore(compat_data_dir)
                        if compat_data_dir is not None
                        else None
                    ),
                    index_prices=(
                        IndexPriceStore(compat_data_dir)
                        if compat_data_dir is not None
                        else None
                    ),
                )
                return _run_event_strategy_backtest(
                    req,
                    strategy_obj,
                    strategy_id,
                    strategy_version,
                    code_hash,
                    data_engine=data_engine,
                )

            # 编译策略代码（P0-11 静态护栏；容器隔离后续）
            from app.services.security.code_guard import assert_safe_user_code

            if req.strategy_id is None:
                assert_safe_user_code(strategy_code, kind="策略")
            namespace = {"__name__": "user_strategy"}
            exec(compile(strategy_code, '<strategy>', 'exec'), namespace)
            strategy_classes = [
                obj for obj in namespace.values()
                if isinstance(obj, type)
                and obj.__module__ == "user_strategy"
                and hasattr(obj, "generate_signals" if req.engine_mode == "vectorized" else "initialize")
            ]
            if len(strategy_classes) != 1:
                method = "generate_signals" if req.engine_mode == "vectorized" else "initialize"
                raise ValueError(f"策略代码必须且只能定义一个包含 {method} 方法的类")
            strategy_class = strategy_classes[0]
            if expected_entrypoint is not None and strategy_class.__name__ != expected_entrypoint:
                raise ValueError("策略索引 entrypoint 与源码中的策略类不一致")
            if req.engine_mode == "event":
                return _run_event_strategy_backtest(
                    req,
                    strategy_class,
                    strategy_id,
                    strategy_version,
                    code_hash,
                    data_engine=data_engine,
                )
        from app.services.strategy.vectorized.factor_strategy import FactorPortfolioStrategy

        from app.models.data import Panel
        provider = data_engine.provider
        from app.utils.data import first_panel_date

        date_probe = data_engine.get_task_panels(
            req.start,
            req.end,
            fields=["close"],
            symbols=[],
        )
        raw_symbols, symbol_mapping, declared_symbols = _resolve_strategy_symbols(
            provider,
            universe,
            strategy_class,
            as_of=first_panel_date(date_probe),
        )
        raw_panels = data_engine.get_task_panels(
            req.start,
            req.end,
            symbols=raw_symbols,
        )
        panels = Panel(
            _StrategyPanels(
                raw_panels, raw_symbols, symbol_mapping
            ),
            index_loader=lambda name: data_engine.get_index(name, req.start, req.end),
        )
        close = panels.close
        filter_info = _strategy_filter_info(data_engine, panels, req)
        allow = filter_info["mask"]
        structured = issubclass(strategy_class, FactorPortfolioStrategy)
        from app.services.factor.market_data import _get_stock_names
        stock_names = _get_stock_names()
        factor_values = None

        # 结构化策略在策略源码内定义因子；自由策略接收收盘价面板自行生成信号。
        if structured:
            strategy = strategy_class()
            if allow is not None:
                # 在排名前剔除：不合格标的的分数置空后退出 TopN，由次优标的
                # 补足；非调仓日仍由 rebalance 掩码保持 NaN。
                strategy._eligibility_mask = allow
            signals = strategy.generate_signals(panels)
        else:
            strategy = _instantiate_vectorized_strategy(strategy_class, panels)
            signals = strategy.generate_signals()
        # 开盘价只在 next_open 口径下被引擎使用；顺手带上不额外读盘
        # （panels 已装载全部字段）。
        open_prices = panels.open if "open" in panels else None
        if isinstance(signals, BacktestSignals):
            bt_signals = signals
            if bt_signals.open is None and req.execution_timing == "next_open":
                bt_signals.open = open_prices
        elif isinstance(signals, pd.DataFrame):
            bt_signals = BacktestSignals(
                target_weights=signals, close=close, open=open_prices
            )
        else:
            raise TypeError(
                "generate_signals 必须返回 pandas.DataFrame 或 BacktestSignals"
            )
        if allow is not None and not structured:
            # 自由策略逐日给出目标权重、没有调仓日 NaN 语义；被剔除的标的必须
            # 写 0 才真正退出，写 NaN 会被 NativeEngine 的前值填充继续持有。
            aligned = allow.reindex(
                index=bt_signals.target_weights.index,
                columns=bt_signals.target_weights.columns,
            ).fillna(False)
            bt_signals.target_weights = bt_signals.target_weights.mask(
                ~aligned, other=0.0
            )

        # 跑回测
        bt_config = BacktestConfig(
            engine="native",
            initial_capital=req.initial_capital,
            commission_rate=req.commission_rate,
            stamp_tax=req.stamp_tax,
            slippage_rate=req.slippage_rate,
            execution_timing=req.execution_timing,
        )
        bt_engine = get_backtest_engine("native")
        bt_result = bt_engine.run(bt_signals, bt_config)

        equity = bt_result.equity_curve
        returns = bt_result.returns

        # 算指标
        from app.models.metric import MetricContext
        from app.services.metric.engine import MetricEngine

        metric_engine = MetricEngine()
        ctx = MetricContext(
            equity_curve=equity,
            returns=returns,
            factor_values=factor_values,
            forward_returns=None,
            positions=bt_result.positions,
            periods_per_year=252,
        )
        metrics = metric_engine.calculate_all(ctx)

    except Exception as exc:  # noqa: BLE001
        return StrategyBacktestResponse(
            error=f"{type(exc).__name__}: {exc}",
            meta=None,
        )

    final_equity = float(equity.iloc[-1]) if equity is not None and len(equity) else None
    metrics = metrics_to_dict(metrics)
    if equity is not None and len(equity):
        metrics["total_return"] = float(equity.iloc[-1] / equity.iloc[0] - 1)

    equity_points = _compute_equity_with_drawdown(equity) if equity is not None else None

    drawdowns = _compute_drawdowns(equity) if equity is not None else []
    annual_returns = _compute_annual_returns(equity) if equity is not None else []
    try:
        benchmark_equity = _benchmark_equity(
            req.benchmark,
            benchmark_snapshot,
            equity.index,
            data_engine=data_engine,
        )
    except Exception as exc:  # noqa: BLE001
        return StrategyBacktestResponse(
            error=f"{type(exc).__name__}: {exc}",
            meta=None,
        )
    benchmark_curve = _compute_equity_with_drawdown(benchmark_equity)
    common_dates = equity.index.intersection(benchmark_equity.index)
    excess_return = None
    if len(common_dates):
        strategy_return = equity.loc[common_dates[-1]] / equity.loc[common_dates[0]] - 1
        benchmark_return = benchmark_equity.loc[common_dates[-1]] / benchmark_equity.loc[common_dates[0]] - 1
        excess_return = float(strategy_return - benchmark_return)

    positions = None
    if req.include_positions and bt_result.positions is not None:
        positions = _sample_positions(bt_result.positions, stock_names)
    rebalance_logs = None
    if req.include_rebalance_logs:
        rebalance_logs = _serialize_rebalance_logs(bt_result.rebalance_logs, stock_names)
    execution_timeline = None
    if req.include_execution_timeline:
        execution_timeline = _serialize_vectorized_execution_timeline(
            bt_result.rebalance_logs, stock_names,
        )

    cost_meta = _cost_meta(req, engine_mode="vectorized")
    return StrategyBacktestResponse(
        metrics=metrics,
        equity_curve=equity_points,
        drawdowns=drawdowns,
        annual_returns=annual_returns,
        date_range=DateRange(**date_range_from(equity)),
        final_equity=final_equity,
        benchmark_curve=benchmark_curve,
        excess_return=excess_return,
        meta=StrategyBacktestMeta(
            strategy=strategy_class.__name__,
            universe=req.universe if universe_snapshot is None else universe_snapshot.asset_id,
            benchmark=req.benchmark,
            universe_asset=_asset_meta(universe_snapshot),
            benchmark_asset=_asset_meta(benchmark_snapshot),
            engine_mode="vectorized",
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            code_hash=code_hash,
            factor_refs=(
                [ref.model_dump(mode="json") for ref in req.factor_refs]
                if req.factor_refs else []
            ),
            **_strategy_run_identity(req, data_engine),
            filter_suspended=req.filter_suspended,
            filter_st=req.filter_st,
            filter_limit_up=req.filter_limit_up,
            filter_limit_down=req.filter_limit_down,
            filter_status=filter_info["status"],
            **cost_meta,
            trading_symbols=list(declared_symbols) or None,
        ),
        positions=positions,
        rebalance_logs=rebalance_logs,
        execution_timeline=execution_timeline,
        error=None,
    )


def _cost_meta(req: StrategyBacktestRequest, *, engine_mode: str) -> dict:
    """回显本次实际生效的交易成本参数与建模口径。

    两种 engine_mode 对成本的建模方式不同，回显出来让结果可以自证：
    向量化按换手率计提、没有单笔概念；事件模式按笔成交、滑点体现在成交价上。
    """

    same_close = req.execution_timing == "same_close"
    execution = {
        "execution_timing": req.execution_timing,
        "execution_model": {
            "signal": "at_close",
            "fill_price": req.execution_timing,
            "fill_session": "T" if same_close else "T+1",
            "fill_field": "open" if req.execution_timing == "next_open" else "close",
            "data_visibility": (
                "through_fill_day_close" if same_close else "through_previous_close"
            ),
        },
    }
    if engine_mode == "event":
        return {
            **execution,
            "commission_rate": req.commission_rate,
            "stamp_tax": req.stamp_tax,
            "slippage_rate": req.slippage_rate,
            "min_commission": req.min_commission,
            "cost_model": {
                "commission": "per_fill_amount_times_rate_floored_at_min_commission",
                "stamp_tax": "per_sell_fill_amount_times_rate",
                "slippage": "price_adjusted_buy_up_sell_down",
                "min_commission": "per_fill_floor",
                "timing": "at_fill",
            },
        }
    return {
        **execution,
        "commission_rate": req.commission_rate,
        "stamp_tax": req.stamp_tax,
        "slippage_rate": req.slippage_rate,
        # 换手率模型没有「单笔成交」，无法表达按笔下限。
        "min_commission": None,
        "cost_model": {
            "commission": "turnover_times_rate",
            "stamp_tax": "sell_turnover_times_rate",
            "slippage": "turnover_times_rate",
            "min_commission": "not_applicable",
            "timing": "per_rebalance",
            "note": "佣金与滑点同项相加，单独设置其中一个结果相同",
        },
    }


def _strategy_filter_info(data_engine, panels, req: StrategyBacktestRequest) -> dict:
    """按请求的过滤开关构造策略侧可交易性掩码。

    返回 ``{"mask", "status", "warnings"}``，``mask`` 为 ``True=允许交易``。
    与因子回测口径一致：退市剔除恒开启，所以掩码不再有 ``None``（不剔除）
    这个状态，即使四个可选开关全关也必须构造掩码。
    """

    return data_engine.build_filter_mask(
        panels,
        filter_suspended=req.filter_suspended,
        filter_st=req.filter_st,
        filter_limit_up=req.filter_limit_up,
        filter_limit_down=req.filter_limit_down,
    )


def _preload_panels(panels, fields) -> None:
    """并发预读字段面板；12 个字段串行加载约占启动时间的一半。"""

    names = [field for field in fields if field in panels]
    if len(names) <= 2:
        return
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(4, len(names))) as pool:
        list(pool.map(lambda field: panels[field], names))


def _rename_symbols(frame: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """面板列名换成交易所代码；不复制数据块，避免 5000 列 rename 的开销。"""

    columns = [mapping.get(str(column), column) for column in frame.columns]
    if columns == list(frame.columns):
        return frame
    renamed = frame.copy(deep=False)
    renamed.columns = columns
    return renamed


def _run_event_strategy_backtest(
    req: StrategyBacktestRequest,
    strategy_class,
    strategy_id: str | None,
    strategy_version: str | None,
    code_hash: str | None,
    *,
    data_engine=None,
) -> StrategyBacktestResponse:
    """运行日频事件策略，并将审计结果转成 API 契约。"""
    try:
        from app.models.event import EventBacktestConfig
        from app.services.strategy.event.engine import EventBacktestEngine
        from app.models.metric import MetricContext
        from app.services.metric.engine import MetricEngine

        if data_engine is None:
            raise RuntimeError("StrategyExperiment 必须提供 DataEngine")
        provider = data_engine.provider
        strategy = strategy_class() if isinstance(strategy_class, type) else strategy_class
        required_fields = (
            set(getattr(strategy, "required_fields", ()))
            | set(getattr(strategy, "required_market_state", ()))
            | {"open", "high", "low", "close"}
        )
        if req.strategy_dialect == "joinquant":
            # 聚宽源码会直接 get_price(fields=['money'/'volume'])，
            # 事件策略自身不一定声明这些字段。
            required_fields |= {"volume", "amount", "turnover"}
        from app.utils.data import first_panel_date

        date_probe = data_engine.get_task_panels(
            req.start,
            req.end,
            fields=["close"],
            symbols=[],
        )
        raw_symbols, symbol_mapping, declared_symbols = _resolve_strategy_symbols(
            provider,
            req.universe,
            strategy,
            as_of=first_panel_date(date_probe),
        )
        # 聚宽策略首日就要看上一交易日数据（选股、涨跌停、历史窗口），
        # 多加载一段预热期；权益曲线仍从 req.start 开始。
        panel_start = req.start
        warmup = req.strategy_dialect == "joinquant"
        if warmup:
            # 聚宽策略的 get_price(count=N) 直接对应 N 个交易日的历史；红利这类
            # 策略要看 780 个交易日（约 3.1 年），400 天预热会把窗口截断，
            # 波动率等指标因此失真（实测 601288 的 780 日周波动率 0.0311 vs
            # 聚宽 0.0246）。这里按最长常用窗口留足 1200 个自然日。
            panel_start = (
                pd.Timestamp(req.start) - pd.Timedelta(days=1200)
            ).strftime("%Y-%m-%d")
        # 无条件加载 listed_status：退市剔除恒开启，不依赖 filter_st 开关。
        # 聚宽策略还会 get_price(fields=['paused'])，需要逐日停牌状态。
        listed_status_available = True
        required_fields.add("listed_status")
        if req.strategy_dialect == "joinquant":
            required_fields.add("up_and_down_status")
        while True:
            try:
                panels = data_engine.get_task_panels(
                    panel_start,
                    req.end,
                    fields=sorted(required_fields),
                    symbols=raw_symbols,
                )
                break
            except KeyError:
                # 当前数据版本没有状态字段时按因子回测的口径跳过对应过滤，
                # 其余字段照常加载；其余缺失字段仍然照常报错。
                if "up_and_down_status" in required_fields:
                    required_fields.discard("up_and_down_status")
                    continue
                if "turnover" in required_fields:
                    # 换手率是聚宽策略的可选字段，缺数据时按未提供处理。
                    required_fields.discard("turnover")
                    continue
                if "listed_status" in required_fields:
                    listed_status_available = False
                    required_fields.discard("listed_status")
                    continue
                raise
        _preload_panels(panels, sorted(required_fields))
        event_panels = {
            field: _rename_symbols(panels[field], symbol_mapping)
            for field in required_fields
        }
        # 涨跌停价优先使用数据侧权威结果（含板块/ST 与 2026-07-06 后的规则）；
        # 数据集缺失时引擎回退到「昨收 × 板块比例」的本地推算。
        if data_engine.data_dir is not None:
            from app.services.data.limit_prices import load_limit_panels

            limit_symbols = [
                symbol_mapping.get(str(symbol), str(symbol))
                for symbol in raw_symbols
            ]
            try:
                raw_limits = load_limit_panels(
                    data_engine.data_dir,
                    limit_symbols,
                    start=panel_start,
                    end=req.end,
                )
            except FileNotFoundError:
                raw_limits = None
            if raw_limits:
                for field, panel in raw_limits.items():
                    event_panels[field] = _rename_symbols(panel, symbol_mapping)
        index = data_engine.get_index(req.benchmark, req.start, req.end)
        benchmark_symbol = getattr(strategy, "benchmark_symbol", None)
        if benchmark_symbol:
            required_index_fields = {"open", "high", "low", "close"}
            missing_index_fields = required_index_fields - set(index.columns)
            if missing_index_fields:
                raise ValueError(
                    f"基准 {req.benchmark} 缺少日线字段: {sorted(missing_index_fields)}"
                )
            for field in required_index_fields:
                event_panels[field] = event_panels[field].copy()
                event_panels[field][benchmark_symbol] = index[field].reindex(
                    event_panels[field].index
                )

        # 聚宽方言 + 未复权撮合：用现金分红模型（派息入现金、送股增股），
        # 与聚宽 ``use_real_price=True`` 的账户口径一致；数据集缺失时保留
        # 再投资口径并在 assumptions 里标注。
        company_action_model = "reinvest"
        dividend_tax_rate = 0.0
        if (
            req.strategy_dialect == "joinquant"
            and req.execution_price_source == "unadjusted"
            and data_engine.data_dir is not None
        ):
            from app.services.data.dividends import load_dividend_panels

            # raw_symbols 是无后缀代码（000001），分红/涨跌停数据集用交易所
            # 代码（000001.SZ）；先按 symbol_mapping 换成交易所代码再过滤。
            dividend_symbols = [
                symbol_mapping.get(str(symbol), str(symbol))
                for symbol in raw_symbols
            ]
            try:
                raw_dividends = load_dividend_panels(
                    data_engine.data_dir,
                    dividend_symbols,
                    start=panel_start,
                    end=req.end,
                )
            except FileNotFoundError:
                raw_dividends = None
            if raw_dividends:
                for field, panel in raw_dividends.items():
                    event_panels[field] = _rename_symbols(panel, symbol_mapping)
                company_action_model = "cash"
                # 聚宽真实价格模式统一按 20% 预扣红利税（除权日按税前 80% 入账）。
                dividend_tax_rate = 0.2

        execution_panels = None
        if req.execution_price_source == "unadjusted":
            from app.services.paper_trading.price_view import load_unadjusted_panels

            try:
                raw_execution = load_unadjusted_panels(
                    data_engine,
                    start=panel_start,
                    end=req.end,
                    symbols=raw_symbols,
                    fields=("open", "close"),
                )
            except FileNotFoundError as exc:
                raise ValueError(str(exc)) from exc
            execution_panels = {
                field: _rename_symbols(raw_execution[field], symbol_mapping)
                for field in ("open", "close")
            }

        # 复权撮合时，涨跌停价是未复权口径；读取同日未复权收盘价做比例换算，
        # 否则 adjusted price 会被误判成 LIMIT_UP/LIMIT_DOWN。
        limit_reference_close = None
        if (
            req.execution_price_source == "adjusted"
            and data_engine is not None
            and ("upper_limit" in event_panels or "lower_limit" in event_panels)
        ):
            from app.services.paper_trading.price_view import load_unadjusted_panels

            try:
                raw_close = load_unadjusted_panels(
                    data_engine,
                    start=panel_start,
                    end=req.end,
                    symbols=raw_symbols,
                    fields=("close",),
                )["close"]
                close_panel = event_panels["close"]
                limit_reference_close = _rename_symbols(
                    raw_close, symbol_mapping
                ).reindex(
                    index=close_panel.index, columns=close_panel.columns
                ).ffill()
            except FileNotFoundError:
                limit_reference_close = None

        # 策略显式声明的 ETF（如货币基金 511880）单独并入面板：ETF 不在
        # 股票池数据里，来自视图的 free/etf_daily，按 symbol + time join。
        etf_symbols: set[str] = set()
        etf_aliases: dict[str, str] = {}
        if req.extra_symbols:
            from app.services.data.etf import load_etf_panels
            from app.services.strategy.compat.runtime import normalize_security

            extra_symbols = {
                normalize_security(symbol) for symbol in req.extra_symbols
            }
            if data_engine.data_dir is None:
                raise ValueError("加载 ETF 行情需要行情目录")
            etf_panels = load_etf_panels(
                data_engine.data_dir,
                sorted(extra_symbols),
                start=panel_start,
                end=req.end,
            )
            etf_symbols = set(extra_symbols)
            # 策略里写死的 511880.XSHG 与引擎代码 511880.SH 的别名，
            # 持仓和订单一律按策略侧写法回报。
            etf_aliases = {
                normalize_security(symbol): symbol for symbol in req.extra_symbols
            }
            for field, panel in etf_panels.items():
                if field in event_panels:
                    event_panels[field] = event_panels[field].join(
                        panel.reindex(index=event_panels[field].index), how="left"
                    )
                if execution_panels is not None and field in execution_panels:
                    execution_panels[field] = execution_panels[field].join(
                        panel.reindex(index=execution_panels[field].index), how="left"
                    )

        intraday_price = None
        intraday_store = None
        prefetch_dates = None
        data_dir = getattr(data_engine, "data_dir", None)
        if req.timed_execution and data_dir is not None:
            from app.services.data.intraday import MinuteStore

            store = MinuteStore(data_dir)
            intraday_store = store

            def _intraday_price(date, time, symbol):
                # 分钟数据缺失（例如 2025-01-15 之前）时回退到日线近似。
                try:
                    return store.price_at(date, time, symbol)
                except Exception:  # noqa: BLE001
                    return None

            intraday_price = _intraday_price

            # 单日分钟文件的 parquet 页脚解析约 0.4 秒，逐日串行会占到
            # 事件回测总耗时的三成以上；后台按交易日顺序预读，与策略计算重叠。
            close_panel = event_panels.get("close")
            if close_panel is not None:
                trading_dates = close_panel.loc[
                    pd.Timestamp(req.start) : pd.Timestamp(req.end)
                ].index
                if len(trading_dates) > 1:
                    prefetch_dates = trading_dates

        # 聚宽首日也有 previous_date；引擎从预热面板取真实上一交易日。
        first_previous_date = None
        active_start = req.start if warmup else None

        engine = EventBacktestEngine(
            event_panels,
            EventBacktestConfig(
                initial_capital=req.initial_capital,
                commission_rate=req.commission_rate,
                min_commission=req.min_commission,
                stamp_tax_rate=req.stamp_tax,
                slippage_rate=req.slippage_rate,
                execution_timing=req.execution_timing,
                filter_st=req.filter_st and listed_status_available,
                execution_price_source=req.execution_price_source,
                company_action_model=company_action_model,
                dividend_tax_rate=dividend_tax_rate,
                timed_execution=req.timed_execution,
            ),
            execution_panels=execution_panels,
            intraday_price=intraday_price,
            limit_reference_close=limit_reference_close,
            first_previous_date=first_previous_date,
            active_start=active_start,
            etf_symbols=etf_symbols,
            symbol_aliases=etf_aliases,
        )
        try:
            if intraday_store is not None and prefetch_dates is not None:
                intraday_store.prefetch(prefetch_dates, window=8, workers=8)
            result = engine.run(strategy)
        finally:
            if intraday_store is not None:
                intraday_store.close()
        equity = result.equity_curve
        metrics = metrics_to_dict(MetricEngine().calculate_all(MetricContext(
            equity_curve=equity, returns=result.returns, periods_per_year=252,
        )))
        metrics["total_return"] = float(equity.iloc[-1] / equity.iloc[0] - 1)
        benchmark_equity = _load_benchmark_equity(
            req.benchmark,
            equity.index,
            data_engine=data_engine,
        )
        common_dates = equity.index.intersection(benchmark_equity.index)
        excess_return = None
        if len(common_dates):
            excess_return = float(
                equity.loc[common_dates[-1]] / equity.loc[common_dates[0]]
                - benchmark_equity.loc[common_dates[-1]] / benchmark_equity.loc[common_dates[0]]
            )
        orders = [EventOrderInfo(
            order_id=order.order_id, symbol=order.symbol, quantity=order.quantity,
            target_value=order.target_value, side=order.side, status=order.status,
            reason_code=order.reason_code,
            submitted_at=order.submitted_at.isoformat() if order.submitted_at else None,
        ) for order in result.orders]
        fills = [EventFillInfo(
            order_id=fill.order_id, symbol=fill.symbol, side=fill.side,
            quantity=fill.quantity, price=fill.price, timestamp=fill.timestamp.isoformat(),
            commission=fill.commission, stamp_tax=fill.stamp_tax,
        ) for fill in result.fills]
        event_positions = [EventPositionSnapshot(
            timestamp=snapshot.timestamp.isoformat(), cash=snapshot.cash,
            total_value=snapshot.total_value, positions=snapshot.positions,
            sellable_quantities=snapshot.sellable_quantities,
        ) for snapshot in result.position_snapshots]
        events = [{
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in event.items()
        } for event in result.events]
        execution_timeline = None
        if req.include_execution_timeline:
            execution_timeline = _serialize_event_execution_timeline(
                orders, fills, event_positions, events,
            )
        return StrategyBacktestResponse(
            metrics=metrics,
            equity_curve=_compute_equity_with_drawdown(equity),
            drawdowns=_compute_drawdowns(equity),
            annual_returns=_compute_annual_returns(equity),
            date_range=DateRange(**date_range_from(equity)),
            final_equity=float(equity.iloc[-1]),
            benchmark_curve=_compute_equity_with_drawdown(benchmark_equity),
            excess_return=excess_return,
            meta=StrategyBacktestMeta(
                strategy=getattr(strategy_class, "__name__", type(strategy).__name__),
                universe=req.universe,
                benchmark=req.benchmark, engine_mode="event", strategy_id=strategy_id,
                strategy_version=strategy_version, code_hash=code_hash,
                **_strategy_run_identity(req, data_engine),
                filter_suspended=req.filter_suspended,
                filter_st=req.filter_st,
                filter_limit_up=req.filter_limit_up,
                filter_limit_down=req.filter_limit_down,
                # 撮合层对停牌与涨跌停的拦截始终开启、不受请求开关影响，
                # 如实标注以免使用方误以为关掉开关就能在停牌日成交。
                filter_status={
                    "is_suspended": "matcher_always_on",
                    "is_st": (
                        "disabled"
                        if not req.filter_st
                        else ("enabled" if listed_status_available else "skipped_missing_field")
                    ),
                    "is_limit_up": "matcher_always_on",
                    "is_limit_down": "matcher_always_on",
                    # 退市剔除与停牌/涨跌停一样，在事件引擎里恒开启、无开关。
                    "is_delisted": (
                        "matcher_always_on"
                        if listed_status_available
                        else "skipped_missing_field"
                    ),
                },
                **_cost_meta(req, engine_mode="event"),
                execution_price_source=req.execution_price_source,
                trading_symbols=list(declared_symbols) or None,
            ),
            orders=orders,
            fills=fills,
            event_positions=event_positions,
            events=events,
            assumptions=result.assumptions,
            execution_timeline=execution_timeline,
        )
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("RE_DEBUG"):
            import traceback

            traceback.print_exc()
        error_code = None
        unsupported_api = None
        # 只有聚宽方言下的 NotImplementedError 才表示「调用了不支持的 API」。
        if isinstance(exc, NotImplementedError) and req.strategy_dialect == "joinquant":
            error_code = "unsupported_api"
            unsupported_api = str(exc)
        return StrategyBacktestResponse(
            error=f"{type(exc).__name__}: {exc}",
            error_code=error_code,
            unsupported_api=unsupported_api,
        )


__all__ = ["run_strategy_backtest", "_run_event_strategy_backtest"]
