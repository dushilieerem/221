"""向量化策略的目标权重到 D 日整手订单。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.models.data import Panel
from app.models.event import EventOrder
from app.models.strategy import BacktestSignals
from app.services.factor.backtest_support import (
    _StrategyPanels,
    _resolve_strategy_symbols,
)
from app.services.paper_trading.errors import invalid
from app.services.paper_trading.price_view import mark_price_on
from app.services.strategy.backtest import (
    _instantiate_vectorized_strategy,
    _strategy_filter_info,
)
from app.services.strategy.vectorized.factor_strategy import FactorPortfolioStrategy


class _SlicedPanels:
    """按需把字段切到 ``<= end``，避免向量化信号准备时加载全部字段。"""

    def __init__(self, panels, end: pd.Timestamp) -> None:
        self._panels = panels
        self._end = pd.Timestamp(end).normalize()

    def __contains__(self, name: str) -> bool:
        return name in self._panels

    def __iter__(self):
        return iter(self._panels)

    def keys(self):
        return list(self._panels.keys())

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self._panels[name].loc[: self._end]


def _filter_request(execution) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        filter_suspended=True,
        filter_st=bool(getattr(execution, "filter_st", False)),
        filter_limit_up=True,
        filter_limit_down=True,
    )


def generate_target_weights(
    strategy_class,
    data_engine,
    *,
    universe: str,
    benchmark: str,
    signal_date: pd.Timestamp,
    history_start: pd.Timestamp,
    raw_symbols: list[str],
    mapping: dict[str, str],
    execution,
    warnings: list[str],
) -> pd.DataFrame:
    """复用现有向量化回测的信号生成路径，只保留到 signal_date 的数据。"""

    raw_panels = data_engine.get_task_panels(
        history_start, signal_date, symbols=raw_symbols
    )
    panels = Panel(
        _StrategyPanels(_SlicedPanels(raw_panels, signal_date), raw_symbols, mapping),
        index_loader=lambda name: data_engine.get_index(name, history_start, signal_date),
    )
    close = panels.close
    filter_info = _strategy_filter_info(
        data_engine, panels, _filter_request(execution)
    )
    allow = filter_info["mask"]
    structured = issubclass(strategy_class, FactorPortfolioStrategy)

    if structured:
        strategy = strategy_class()
        if allow is not None:
            strategy._eligibility_mask = allow
        signals = strategy.generate_signals(panels)
    else:
        strategy = _instantiate_vectorized_strategy(strategy_class, panels)
        signals = strategy.generate_signals()

    if isinstance(signals, BacktestSignals):
        bt_signals = signals
    elif isinstance(signals, pd.DataFrame):
        bt_signals = BacktestSignals(target_weights=signals, close=close)
    else:
        raise invalid(
            "STRATEGY_CODE_INVALID",
            "generate_signals 必须返回 pandas.DataFrame 或 BacktestSignals",
        )

    if allow is not None and not structured:
        aligned = allow.reindex(
            index=bt_signals.target_weights.index,
            columns=bt_signals.target_weights.columns,
        ).fillna(False)
        bt_signals.target_weights = bt_signals.target_weights.mask(~aligned, other=0.0)

    weights = bt_signals.target_weights
    if weights is None or weights.empty or len(weights.index) == 0:
        raise invalid("NO_SIGNAL_DATE", "策略没有产生任何目标权重")
    return weights


def build_target_orders(
    weights: pd.DataFrame,
    *,
    settlement_date: pd.Timestamp,
    cash: float,
    positions: list[dict],
    vwap: pd.DataFrame,
    mark: pd.DataFrame,
    lot_size: int,
    warnings: list[str],
) -> list[EventOrder]:
    """把最后一行有效权重转换成整手目标数量订单。"""

    last_row = weights.sort_index().ffill().iloc[-1]
    targets: dict[str, float] = {}
    for symbol, value in last_row.items():
        if pd.isna(value):
            targets[str(symbol)] = 0.0
            continue
        number = float(value)
        if not np.isfinite(number):
            warnings.append(f"TARGET_WEIGHT_NON_FINITE:{symbol}")
            number = 0.0
        targets[str(symbol)] = number

    negative = {symbol: value for symbol, value in targets.items() if value < -1e-12}
    if negative:
        raise invalid(
            "UNSUPPORTED_PORTFOLIO",
            "模拟盘仅支持现金多头，目标权重不能为负",
            symbols=sorted(negative),
        )
    total_weight = float(sum(targets.values()))
    if total_weight > 1.0 + 1e-6:
        raise invalid(
            "TARGET_WEIGHT_EXCEEDS_ONE",
            f"目标权重合计 {total_weight:.6f} 超过 1",
        )

    held = {
        str(item["symbol"]): int(item.get("quantity") or 0)
        for item in positions
        if int(item.get("quantity") or 0) > 0
    }
    for symbol in held:
        targets.setdefault(symbol, 0.0)

    def price_basis(symbol: str) -> tuple[float | None, bool]:
        if symbol in vwap.columns and settlement_date in vwap.index:
            value = vwap.at[settlement_date, symbol]
            if pd.notna(value) and np.isfinite(value) and value > 0:
                return float(value), False
        value, fallback = mark_price_on(mark, day=settlement_date, symbol=symbol)
        if value is not None:
            return value, True
        return None, True

    # 执行前总权益：现金 + 持仓按 D 日 VWAP 估值；缺价时逐级回退。
    pre_equity = float(cash)
    for symbol, quantity in held.items():
        price, used_fallback = price_basis(symbol)
        if price is None:
            for item in positions:
                if str(item.get("symbol")) == symbol:
                    price = float(item.get("average_cost") or 0.0)
                    break
            warnings.append(f"MARK_PRICE_FALLBACK:{symbol}")
        elif used_fallback:
            warnings.append(f"MARK_PRICE_FALLBACK:{symbol}")
        pre_equity += quantity * float(price or 0.0)

    orders: list[EventOrder] = []
    for symbol in sorted(targets):
        weight = targets[symbol]
        current = held.get(symbol, 0)
        price, used_fallback = price_basis(symbol)
        if price is None:
            if weight > 0:
                # 没有有效 VWAP 时不新增买入；继续持有已有仓位。
                warnings.append(f"NO_VWAP_SKIPPED:{symbol}")
                continue
            if current > 0:
                # 生成卖出订单，让执行器以 NO_VWAP 留痕拒单。
                orders.append(EventOrder(
                    symbol=symbol,
                    quantity=0,
                    order_id="",
                    submitted_at=settlement_date.to_pydatetime(),
                ))
            continue
        if used_fallback:
            warnings.append(f"MARK_PRICE_FALLBACK:{symbol}")
        target_quantity = int(weight * pre_equity / price / lot_size) * lot_size
        if target_quantity == current:
            continue
        orders.append(EventOrder(
            symbol=symbol,
            quantity=target_quantity,
            order_id="",
            submitted_at=settlement_date.to_pydatetime(),
        ))
    return orders


def prepare_vectorized_symbols(
    strategy_class,
    data_engine,
    *,
    req,
    signal_date: pd.Timestamp,
    held_symbols: list[str],
):
    """解析股票池，并把账户持仓并入加载范围。"""

    from app.services.factor.market_data import _market_symbol, _panel_symbol

    raw_symbols, mapping, declared = _resolve_strategy_symbols(
        data_engine.provider, req.universe, strategy_class, as_of=signal_date
    )
    universe_symbols = list(dict.fromkeys(raw_symbols))
    for symbol in held_symbols:
        raw = _panel_symbol(symbol)
        if raw not in mapping:
            raw_symbols.append(raw)
            mapping[raw] = _market_symbol(raw)
    raw_symbols = list(dict.fromkeys(raw_symbols))
    return raw_symbols, mapping, declared, universe_symbols


__all__ = [
    "build_target_orders",
    "generate_target_weights",
    "prepare_vectorized_symbols",
]
