"""事件回测的订单、成交与审计结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass
class EventOrder:
    symbol: str
    quantity: Optional[int] = None
    target_value: Optional[float] = None
    order_id: str = ""
    submitted_at: Optional[datetime] = None
    side: Optional[str] = None
    status: str = "submitted"
    reason_code: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.quantity is None) == (self.target_value is None):
            raise ValueError("订单必须且只能指定 quantity 或 target_value")


@dataclass(frozen=True)
class EventFill:
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    commission: float
    stamp_tax: float


@dataclass(frozen=True)
class PositionSnapshot:
    timestamp: datetime
    cash: float
    positions: dict[str, int]
    sellable_quantities: dict[str, int]
    total_value: float


@dataclass
class EventBacktestResult:
    equity_curve: pd.Series
    orders: list[EventOrder] = field(default_factory=list)
    fills: list[EventFill] = field(default_factory=list)
    position_snapshots: list[PositionSnapshot] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    assumptions: dict[str, str] = field(default_factory=dict)

    @property
    def returns(self) -> pd.Series:
        return self.equity_curve.pct_change().dropna()


@dataclass(frozen=True)
class EventBacktestConfig:
    initial_capital: float = 1_000_000.0
    commission_rate: float = 0.0003
    #: 每笔最低手续费；显式设为 0 即取消最低收费。
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    #: 滑点率：买入向上、卖出向下调整成交价，模拟更差的执行价格。
    slippage_rate: float = 0.0
    #: 成交时点。策略回调始终只看到截至前一交易日收盘的数据，因此信号恒在
    #: T 日收盘产生：next_close = T+1（即当日）收盘价成交，next_open = 当日
    #: 开盘价成交。退市强平不受此影响，恒按转换日收盘价处置。
    execution_timing: str = "next_close"
    lot_size: int = 100
    #: 是否拒绝买入 ST/*ST 标的。停牌与涨跌停由撮合层无条件拦截，不受配置影响。
    filter_st: bool = False


@dataclass
class _Ledger:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    average_cost: dict[str, float] = field(default_factory=dict)
    sellable: dict[str, int] = field(default_factory=dict)
    bought_today: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_account(cls, cash: float, positions: list[dict]) -> "_Ledger":
        """用模拟盘账户的持久化持仓恢复账本。

        ``available_quantity`` 是 D 日开盘时可卖数量，直接作为 ``sellable``；
        ``bought_today`` 为空，避免结算开始时把上一交易日的买入重复解禁。
        """

        quantities: dict[str, int] = {}
        average_cost: dict[str, float] = {}
        sellable: dict[str, int] = {}
        for item in positions:
            symbol = str(item.get("symbol") or "").strip()
            quantity = int(item.get("quantity") or 0)
            if not symbol or quantity <= 0:
                continue
            quantities[symbol] = quantity
            average_cost[symbol] = float(item.get("average_cost") or 0.0)
            available = int(item.get("available_quantity") or 0)
            sellable[symbol] = max(0, min(quantity, available))
        return cls(
            cash=float(cash),
            positions=quantities,
            average_cost=average_cost,
            sellable=sellable,
        )

    def export_positions(self) -> list[dict[str, float | int | str]]:
        """导出下一结算日的持仓状态。

        当日买入在 T+1 后才能卖出，因此下一日可卖数量是
        ``sellable + bought_today``；不要在这里调用 ``begin_session``。
        """

        result: list[dict[str, float | int | str]] = []
        for symbol in sorted(self.positions):
            quantity = int(self.positions.get(symbol, 0))
            if quantity <= 0:
                continue
            available = int(self.sellable.get(symbol, 0)) + int(
                self.bought_today.get(symbol, 0)
            )
            result.append(
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "available_quantity": max(0, min(quantity, available)),
                    "average_cost": float(self.average_cost.get(symbol, 0.0)),
                }
            )
        return result

    def begin_session(self) -> None:
        for symbol, quantity in self.bought_today.items():
            self.sellable[symbol] = self.sellable.get(symbol, 0) + quantity
        self.bought_today = {}


@dataclass
class PortfolioView:
    _ledger: object

    @property
    def cash(self) -> float:
        return self._ledger.cash

    @property
    def positions(self) -> dict[str, int]:
        return dict(self._ledger.positions)

    def quantity(self, symbol: str) -> int:
        return self._ledger.positions.get(symbol, 0)

    def average_cost(self, symbol: str) -> float | None:
        """返回当前持仓的成交均价；无持仓时返回 ``None``。"""
        return self._ledger.average_cost.get(symbol)
