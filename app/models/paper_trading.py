"""模拟盘结算的领域结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.models.event import EventFill, EventOrder


@dataclass(frozen=True)
class PaperPositionState:
    """结算后的持仓状态；``available_quantity`` 是下一结算日可卖数量。"""

    symbol: str
    quantity: int
    available_quantity: int
    average_cost: float
    mark_price: float = 0.0
    market_value: float = 0.0


@dataclass
class SettlementDayResult:
    """单日结算的计算结果（尚未持久化）。"""

    settlement_date: date
    signal_date: date
    orders: list[EventOrder] = field(default_factory=list)
    fills: list[EventFill] = field(default_factory=list)
    next_cash: float = 0.0
    next_positions: list[PaperPositionState] = field(default_factory=list)
    commission: float = 0.0
    stamp_tax: float = 0.0
    strategy_state: dict[str, Any] = field(default_factory=dict)
    state_hash: str | None = None
    positions_value: float = 0.0
    total_equity: float = 0.0
    warnings: list[str] = field(default_factory=list)
    assumptions: dict[str, str] = field(default_factory=dict)


__all__ = ["PaperPositionState", "SettlementDayResult"]
