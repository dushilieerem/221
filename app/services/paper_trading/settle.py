"""ResearchEngine 单日模拟盘结算编排。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.models.event import EventBacktestConfig, EventOrder, _Ledger
from app.models.paper_trading import PaperPositionState, SettlementDayResult
from app.schemas.paper_trading import (
    PaperFillInfo,
    PaperNextAccount,
    PaperOrderInfo,
    PaperPositionOut,
    PaperSettlementRequest,
    PaperSettlementResponse,
)
from app.services.factor.backtest_support import _resolve_strategy_symbols
from app.services.factor.market_data import _market_symbol, _panel_symbol
from app.services.paper_trading.errors import conflict, data_not_ready, invalid
from app.services.paper_trading.price_view import (
    build_vwap_panel,
    load_unadjusted_close,
    release_trading_days,
)
from app.services.paper_trading.state_codec import (
    code_hash,
    normalize_code_hash,
    normalize_state_hash,
    state_hash as compute_state_hash,
)
from app.services.paper_trading.status import paper_data_status
from app.services.paper_trading.trading_calendar import next_trading_day
from app.services.paper_trading.vectorized import (
    build_target_orders,
    generate_target_weights,
    prepare_vectorized_symbols,
)
from app.services.strategy.event.engine import EventBacktestEngine


def _load_strategy_class(strategy_code: str, engine_mode: str):
    namespace: dict[str, Any] = {"__name__": "user_strategy"}
    try:
        exec(compile(strategy_code, "<strategy>", "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 - 策略源码由受信任调用方提供
        raise invalid(
            "STRATEGY_CODE_INVALID", f"策略源码无法编译: {type(exc).__name__}: {exc}"
        ) from exc
    method = "generate_signals" if engine_mode == "vectorized" else "initialize"
    classes = [
        obj
        for obj in namespace.values()
        if isinstance(obj, type)
        and getattr(obj, "__module__", None) == "user_strategy"
        and hasattr(obj, method)
    ]
    if len(classes) != 1:
        raise invalid(
            "STRATEGY_CODE_INVALID",
            f"策略代码必须且只能定义一个包含 {method} 方法的类",
        )
    return classes[0]


def _history_start(
    data_engine,
    signal_date: pd.Timestamp,
    history_days: int,
) -> pd.Timestamp:
    days = release_trading_days(data_engine)
    earlier = days[days <= signal_date]
    if not len(earlier):
        raise data_not_ready("回看区间内没有可用交易日")
    start = earlier[max(0, len(earlier) - history_days)]
    return pd.Timestamp(start)


def _validate_contract(req: PaperSettlementRequest, status) -> str:
    computed = code_hash(req.strategy_code)
    expected = normalize_code_hash(req.expected_code_hash)
    if expected is not None and expected != computed:
        raise conflict(
            "CODE_HASH_MISMATCH",
            "策略代码哈希与调用方期望不一致",
            expected_code_hash=expected,
            code_hash=computed,
        )
    if req.expected_data_release_id:
        if status.data_release_id and req.expected_data_release_id != status.data_release_id:
            raise conflict(
                "DATA_RELEASE_MISMATCH",
                "行情 release 已变化",
                expected_data_release_id=req.expected_data_release_id,
                data_release_id=status.data_release_id,
            )
    if req.strategy_state:
        if not req.strategy_state_code_hash:
            raise conflict(
                "STRATEGY_STATE_VERSION_MISMATCH",
                "存在策略状态但未提供 strategy_state_code_hash",
            )
        state_code_hash = normalize_code_hash(req.strategy_state_code_hash)
        if state_code_hash != computed:
            raise conflict(
                "STRATEGY_STATE_VERSION_MISMATCH",
                "策略状态绑定的代码哈希与本次策略代码不一致",
            )
        if req.expected_state_hash:
            expected_state = normalize_state_hash(req.expected_state_hash)
            actual_state = compute_state_hash(req.strategy_state)
            if expected_state != actual_state:
                raise conflict(
                    "STRATEGY_STATE_HASH_MISMATCH",
                    "策略状态哈希与调用方期望不一致",
                )
    return computed


def _dedupe_symbols(raw_symbols: list[str], mapping: dict[str, str], held_symbols: list[str]) -> list[str]:
    for symbol in held_symbols:
        raw = _panel_symbol(symbol)
        if raw not in mapping:
            raw_symbols.append(raw)
            mapping[raw] = _market_symbol(raw)
    return list(dict.fromkeys(raw_symbols))


def _event_panels(
    data_engine,
    strategy,
    *,
    req: PaperSettlementRequest,
    history_start: pd.Timestamp,
    settlement_date: pd.Timestamp,
    raw_symbols: list[str],
    mapping: dict[str, str],
    warnings: list[str],
) -> dict[str, pd.DataFrame]:
    required = (
        set(getattr(strategy, "required_fields", ()))
        | set(getattr(strategy, "required_market_state", ()))
        | {"open", "high", "low", "close", "volume", "amount"}
    )
    required.add("listed_status")
    required.add("up_and_down_status")
    for optional in ("listed_status", "up_and_down_status"):
        try:
            data_engine.get_task_panels(
                settlement_date, settlement_date, fields=[optional], symbols=[]
            )
        except KeyError:
            required.discard(optional)
            warnings.append(f"DATA_FIELD_MISSING:{optional}")

    panels = data_engine.get_task_panels(
        history_start, settlement_date, fields=sorted(required), symbols=raw_symbols
    )
    event_panels = {
        field: panels[field].rename(columns=mapping) for field in sorted(required)
    }
    vwap = build_vwap_panel({
        "amount": event_panels["amount"],
        "volume": event_panels["volume"],
    })
    event_panels["vwap"] = vwap

    benchmark_symbol = getattr(strategy, "benchmark_symbol", None)
    if benchmark_symbol:
        index = data_engine.get_index(req.benchmark, history_start, settlement_date)
        for field in ("open", "high", "low", "close"):
            if field not in index.columns:
                raise invalid(
                    "DATA_FIELD_MISSING",
                    f"基准 {req.benchmark} 缺少日线字段: {field}",
                )
            frame = event_panels[field].copy()
            frame[benchmark_symbol] = index[field].reindex(frame.index)
            event_panels[field] = frame
    return event_panels


def _order_info(order: EventOrder) -> PaperOrderInfo:
    return PaperOrderInfo(
        order_id=order.order_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        target_value=order.target_value,
        status=order.status,
        reason_code=order.reason_code,
        submitted_at=(
            order.submitted_at.isoformat() if order.submitted_at is not None else None
        ),
    )


def _fill_info(fill) -> PaperFillInfo:
    return PaperFillInfo(
        order_id=fill.order_id,
        symbol=fill.symbol,
        side=fill.side,
        quantity=fill.quantity,
        price=fill.price,
        commission=fill.commission,
        stamp_tax=fill.stamp_tax,
        timestamp=fill.timestamp.isoformat(),
    )


def _response(
    *,
    req: PaperSettlementRequest,
    status,
    computed_hash: str,
    settlement_date: pd.Timestamp,
    signal_date: pd.Timestamp,
    orders: list[EventOrder],
    fills: list,
    positions: list[PaperPositionState],
    cash: float,
    positions_value: float,
    total_equity: float,
    commission: float,
    stamp_tax: float,
    state_payload: dict,
    state_hash: str | None,
    warnings: list[str],
    assumptions: dict[str, str],
    data_engine,
) -> PaperSettlementResponse:
    nxt = next_trading_day(data_engine.data_dir, settlement_date)
    return PaperSettlementResponse(
        settlement_date=settlement_date.date().isoformat(),
        signal_date=signal_date.date().isoformat(),
        next_trading_day=nxt.date().isoformat() if nxt is not None else None,
        engine_mode=req.engine_mode,
        data_release_id=status.data_release_id,
        data_release_signature=status.data_release_signature,
        data_version=status.data_version,
        code_hash=computed_hash,
        state_hash=state_hash,
        orders=[_order_info(order) for order in orders],
        fills=[_fill_info(fill) for fill in fills],
        next_account=PaperNextAccount(
            cash=float(cash),
            positions=[
                PaperPositionOut(
                    symbol=item.symbol,
                    quantity=item.quantity,
                    available_quantity=item.available_quantity,
                    average_cost=item.average_cost,
                    mark_price=item.mark_price,
                    market_value=item.market_value,
                )
                for item in positions
            ],
            positions_value=float(positions_value),
            total_equity=float(total_equity),
        ),
        strategy_state=state_payload or {},
        fees={
            "commission": float(commission),
            "stamp_tax": float(stamp_tax),
            "total": float(commission + stamp_tax),
        },
        warnings=sorted(set(warnings)),
        assumptions=assumptions,
    )


def settle_paper_day(req: PaperSettlementRequest, data_engine) -> PaperSettlementResponse:
    """计算一个结算日；不持久化任何状态。"""

    settlement_date = pd.Timestamp(req.settlement_date).normalize()
    status = paper_data_status(data_engine, settlement_date=settlement_date)
    if status.status == "not_trading_day":
        raise conflict(
            "NOT_A_TRADING_DAY",
            f"{settlement_date.date()} 不是交易日",
            next_trading_day=status.next_trading_day,
        )
    if status.status != "ready" or not status.signal_date:
        raise data_not_ready(
            f"{settlement_date.date()} 行情数据尚未就绪",
            latest_ready_trading_day=status.latest_ready_trading_day,
            next_trading_day=status.next_trading_day,
            missing_fields=status.missing_fields,
        )

    computed_hash = _validate_contract(req, status)
    signal_date = pd.Timestamp(status.signal_date).normalize()
    history_start = _history_start(
        data_engine, signal_date, req.execution.history_days
    )
    strategy_class = _load_strategy_class(req.strategy_code, req.engine_mode)
    held_symbols = [
        item.symbol for item in req.account.positions if item.quantity > 0
    ]
    warnings: list[str] = []
    if status.release_stale:
        warnings.append("RELEASE_SIGNATURE_CHANGED")

    if req.engine_mode == "event":
        strategy = strategy_class()
        raw_symbols, mapping, _declared = _resolve_strategy_symbols(
            data_engine.provider, req.universe, strategy, as_of=signal_date
        )
        # 结算面板会额外并入账户持仓用于卖出/估值；把策略的 universe 固定为
        # 本次解析出的股票池，避免持仓标的被策略误当成新候选。
        if hasattr(strategy, "universe") and not getattr(strategy, "universe", None):
            try:
                strategy.universe = [mapping[raw] for raw in raw_symbols]
            except Exception:  # noqa: BLE001 - 用户策略可能禁止写属性
                pass
        raw_symbols = _dedupe_symbols(raw_symbols, mapping, held_symbols)
        event_panels = _event_panels(
            data_engine,
            strategy,
            req=req,
            history_start=history_start,
            settlement_date=settlement_date,
            raw_symbols=raw_symbols,
            mapping=mapping,
            warnings=warnings,
        )
        vwap = event_panels["vwap"]
        market_symbols = [mapping[raw] for raw in raw_symbols]
        mark = load_unadjusted_close(
            data_engine,
            start=history_start,
            end=settlement_date,
            symbols=market_symbols,
        )
        ledger = _Ledger.from_account(
            req.account.cash, [item.model_dump() for item in req.account.positions]
        )
        engine = EventBacktestEngine(
            event_panels,
            EventBacktestConfig(
                commission_rate=req.execution.commission_rate,
                min_commission=req.execution.min_commission,
                stamp_tax_rate=req.execution.stamp_tax_rate,
                slippage_rate=0.0,
                lot_size=req.execution.lot_size,
                filter_st=req.execution.filter_st,
            ),
        )
        result = engine.settle_day(
            strategy,
            settlement_date=settlement_date,
            ledger=ledger,
            signal_date=signal_date,
            persisted_state=req.strategy_state,
            mark_prices=mark,
            order_prefix=f"PAPER-{settlement_date:%Y%m%d}",
        )
        for order in result.orders:
            if order.reason_code == "NO_VWAP":
                warnings.append(f"NO_VWAP:{order.symbol}")
        return _response(
            req=req,
            status=status,
            computed_hash=computed_hash,
            settlement_date=settlement_date,
            signal_date=signal_date,
            orders=result.orders,
            fills=result.fills,
            positions=result.next_positions,
            cash=result.next_cash,
            positions_value=result.positions_value,
            total_equity=result.total_equity,
            commission=result.commission,
            stamp_tax=result.stamp_tax,
            state_payload=result.strategy_state,
            state_hash=result.state_hash,
            warnings=warnings + result.warnings,
            assumptions={
                **result.assumptions,
                "universe": req.universe,
                "benchmark": req.benchmark,
                "slippage": "not_modeled",
            },
            data_engine=data_engine,
        )

    # 向量化策略：只把 signal_date 之前的数据交给策略，D 日数据仅用于成交/估值。
    raw_symbols, mapping, _declared, universe_symbols = prepare_vectorized_symbols(
        strategy_class,
        data_engine,
        req=req,
        signal_date=signal_date,
        held_symbols=held_symbols,
    )
    weights = generate_target_weights(
        strategy_class,
        data_engine,
        universe=req.universe,
        benchmark=req.benchmark,
        signal_date=signal_date,
        history_start=history_start,
        raw_symbols=universe_symbols,
        mapping=mapping,
        execution=req.execution,
        warnings=warnings,
    )
    execution_fields = ["open", "high", "low", "close", "amount", "volume"]
    for optional in ("up_and_down_status", "listed_status"):
        try:
            data_engine.get_task_panels(
                settlement_date, settlement_date, fields=[optional], symbols=[]
            )
        except KeyError:
            warnings.append(f"DATA_FIELD_MISSING:{optional}")
        else:
            execution_fields.append(optional)
    execution_panels = data_engine.get_task_panels(
        history_start,
        settlement_date,
        symbols=raw_symbols,
        fields=execution_fields,
    )
    event_panels: dict[str, pd.DataFrame] = {}
    for field in execution_fields:
        try:
            event_panels[field] = execution_panels[field].rename(columns=mapping)
        except KeyError as exc:
            raise invalid(
                "DATA_FIELD_MISSING", f"向量化结算缺少行情字段: {field}"
            ) from exc
    vwap = build_vwap_panel({
        "amount": event_panels["amount"],
        "volume": event_panels["volume"],
    })
    event_panels["vwap"] = vwap
    market_symbols = [mapping[raw] for raw in raw_symbols]
    mark = load_unadjusted_close(
        data_engine,
        start=history_start,
        end=settlement_date,
        symbols=market_symbols,
    )
    ledger = _Ledger.from_account(
        req.account.cash, [item.model_dump() for item in req.account.positions]
    )
    orders = build_target_orders(
        weights,
        settlement_date=settlement_date,
        cash=req.account.cash,
        positions=[item.model_dump() for item in req.account.positions],
        vwap=vwap,
        mark=mark,
        lot_size=req.execution.lot_size,
        warnings=warnings,
    )
    for index, order in enumerate(orders, 1):
        order.order_id = f"VEC-{index:06d}"
        order.submitted_at = settlement_date.to_pydatetime()
    engine = EventBacktestEngine(
        event_panels,
        EventBacktestConfig(
            commission_rate=req.execution.commission_rate,
            min_commission=req.execution.min_commission,
            stamp_tax_rate=req.execution.stamp_tax_rate,
            slippage_rate=0.0,
            lot_size=req.execution.lot_size,
            filter_st=req.execution.filter_st,
        ),
    )
    fills, positions, positions_value, total_equity, engine_warnings = engine.execute_orders(
        orders,
        settlement_date=settlement_date,
        ledger=ledger,
        vwap=vwap,
        mark_prices=mark,
    )
    for order in orders:
        if order.reason_code == "NO_VWAP":
            warnings.append(f"NO_VWAP:{order.symbol}")
    commission = sum(fill.commission for fill in fills)
    stamp_tax = sum(fill.stamp_tax for fill in fills)
    return _response(
        req=req,
        status=status,
        computed_hash=computed_hash,
        settlement_date=settlement_date,
        signal_date=signal_date,
        orders=orders,
        fills=fills,
        positions=positions,
        cash=ledger.cash,
        positions_value=positions_value,
        total_equity=total_equity,
        commission=commission,
        stamp_tax=stamp_tax,
        state_payload={},
        state_hash=None,
        warnings=warnings + engine_warnings,
        assumptions={
            "frequency": "1d",
            "execution": "daily_vwap_full_fill",
            "signal_visibility": "through_previous_trading_day",
            "fill_price_source": "daily_amount_volume_vwap",
            "mark_price_source": "unadjusted_close",
            "target_weight_source": "last_valid_ffill",
            "lot_size": str(req.execution.lot_size),
            "slippage": "not_modeled",
            "company_actions": "not_modeled",
            "universe": req.universe,
            "benchmark": req.benchmark,
        },
        data_engine=data_engine,
    )


__all__ = ["settle_paper_day"]
