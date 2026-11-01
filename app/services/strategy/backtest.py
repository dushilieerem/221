"""策略回测执行编排。

向量化和事件驱动策略都通过这里完成源码/目录资产解析、行情准备、
QuantCore 引擎调用以及 API 响应组装。``runner.py`` 仅保留兼容门面，
历史调用路径和响应契约不变。
"""

from __future__ import annotations

import inspect
import os

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

        # 聚宽方言：模块级 initialize + 兼容层，不要求策略类。
        if req.strategy_dialect == "joinquant":
            from app.services.security.code_guard import assert_safe_user_code
            from app.services.strategy.compat import load_joinquant_strategy
            from app.services.strategy.compat.factors import FactorStore
            from app.services.strategy.compat.fundamentals import FundamentalStore
            from app.services.strategy.compat.index import (
                IndexMembership,
                IndexPriceStore,
            )
            from app.services.strategy.compat.industry import IndustryStore
            from app.services.strategy.compat.loader import strip_joinquant_imports

            cleaned = strip_joinquant_imports(strategy_code)
            if req.strategy_id is None:
                assert_safe_user_code(cleaned, kind="策略")
            compat_data_dir = getattr(data_engine, "data_dir", None)
            strategy_obj = load_joinquant_strategy(
                strategy_code,
                fundamentals=FundamentalStore(),
                factors=FactorStore(data_engine=data_engine),
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
            panel_start = (
                pd.Timestamp(req.start) - pd.Timedelta(days=400)
            ).strftime("%Y-%m-%d")
        # 无条件加载 listed_status：退市剔除恒开启，不依赖 filter_st 开关。
        listed_status_available = True
        required_fields.add("listed_status")
        try:
            panels = data_engine.get_task_panels(
                panel_start,
                req.end,
                fields=sorted(required_fields),
                symbols=raw_symbols,
            )
        except KeyError:
            # 当前数据版本没有 listed_status 时按因子回测的口径跳过该过滤，
            # 其余字段照常加载；其余缺失字段仍然照常报错。
            if "listed_status" not in required_fields:
                raise
            listed_status_available = False
            required_fields.discard("listed_status")
            panels = data_engine.get_task_panels(
                panel_start,
                req.end,
                fields=sorted(required_fields),
                symbols=raw_symbols,
            )
        event_panels = {
            field: panels[field].rename(columns=symbol_mapping)
            for field in required_fields
        }
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
                field: raw_execution[field].rename(columns=symbol_mapping)
                for field in ("open", "close")
            }

        intraday_price = None
        data_dir = getattr(data_engine, "data_dir", None)
        if req.timed_execution and data_dir is not None:
            from app.services.data.intraday import MinuteStore

            store = MinuteStore(data_dir)

            def _intraday_price(date, time, symbol):
                # 分钟数据缺失（例如 2025-01-15 之前）时回退到日线近似。
                try:
                    return store.price_at(date, time, symbol)
                except Exception:  # noqa: BLE001
                    return None

            intraday_price = _intraday_price

        # 聚宽首日也有 previous_date；引擎从预热面板取真实上一交易日。
        first_previous_date = None
        active_start = req.start if warmup else None

        result = EventBacktestEngine(
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
                timed_execution=req.timed_execution,
            ),
            execution_panels=execution_panels,
            intraday_price=intraday_price,
            first_previous_date=first_previous_date,
            active_start=active_start,
        ).run(strategy)
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
        return StrategyBacktestResponse(error=f"{type(exc).__name__}: {exc}")


__all__ = ["run_strategy_backtest", "_run_event_strategy_backtest"]
