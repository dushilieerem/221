"""事件策略运行时：受限数据视图、调度器与订单服务。"""

from __future__ import annotations

from app.models.event import PortfolioView
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Protocol

import pandas as pd
from app.models.data import DataView

from app.models.event import EventOrder


class EventStrategy(Protocol):
    def initialize(self, context: "StrategyContext") -> None: ...




def normalize_time(value: str) -> str:
    """把聚宽风格的时点统一成 ``HH:MM``；``open`` 视为 09:30。"""

    text = str(value).strip()
    if text.lower() == "open":
        return "09:30"
    if ":" in text:
        hour, minute = text.split(":", 1)
        return f"{int(hour):02d}:{int(minute[:2]):02d}"
    return text


class Scheduler:
    """按交易日 + 时点注册回调。

    ``daily/weekly/monthly`` 的 ``time`` 默认 ``open``（09:30），与旧行为兼容。
    ``due()`` 返回当天全部回调（日频引擎使用）；``due_at()`` 只返回指定时点
    的回调（时点引擎使用）。
    """

    def __init__(self) -> None:
        self._daily: list[tuple[str, Callable[[StrategyContext], None]]] = []
        self._weekly: list[tuple[int, str, Callable[[StrategyContext], None]]] = []
        self._monthly: list[tuple[int, str, Callable[[StrategyContext], None]]] = []

    def daily(
        self,
        callback: Callable[["StrategyContext"], None],
        time: str = "open",
    ) -> None:
        self._daily.append((normalize_time(time), callback))

    def weekly(
        self,
        callback: Callable[["StrategyContext"], None],
        weekday: int = 0,
        time: str = "open",
    ) -> None:
        callback._event_weekday = weekday  # type: ignore[attr-defined]
        self._weekly.append((weekday, normalize_time(time), callback))

    def monthly(
        self,
        callback: Callable[["StrategyContext"], None],
        trading_day: int = 1,
        time: str = "open",
    ) -> None:
        if trading_day < 1:
            raise ValueError("trading_day 必须从 1 开始")
        callback._event_trading_day = trading_day  # type: ignore[attr-defined]
        self._monthly.append((trading_day, normalize_time(time), callback))

    def times(self) -> set[str]:
        """已注册的全部时点。"""

        values = {time for time, _ in self._daily}
        values |= {time for _, time, _ in self._weekly}
        values |= {time for _, time, _ in self._monthly}
        return values

    def due(
        self,
        date: pd.Timestamp,
        trading_days: pd.DatetimeIndex,
    ) -> list[Callable[["StrategyContext"], None]]:
        callbacks = [callback for _, callback in self._daily]
        callbacks.extend(
            callback for weekday, _, callback in self._weekly
            if date.weekday() == weekday
        )
        day_of_month = _trading_day_of_month(date, trading_days)
        callbacks.extend(
            callback for trading_day, _, callback in self._monthly
            if day_of_month == trading_day
        )
        return callbacks

    def due_at(
        self,
        date: pd.Timestamp,
        trading_days: pd.DatetimeIndex,
        time: str,
    ) -> list[Callable[["StrategyContext"], None]]:
        target = normalize_time(time)
        callbacks = [
            callback for item_time, callback in self._daily if item_time == target
        ]
        callbacks.extend(
            callback
            for weekday, item_time, callback in self._weekly
            if weekday == date.weekday() and item_time == target
        )
        day_of_month = _trading_day_of_month(date, trading_days)
        callbacks.extend(
            callback
            for trading_day, item_time, callback in self._monthly
            if trading_day == day_of_month and item_time == target
        )
        return callbacks


def _trading_day_of_month(date: pd.Timestamp, trading_days: pd.DatetimeIndex) -> int:
    month_days = trading_days[trading_days.to_period("M") == date.to_period("M")]
    return int(month_days.get_loc(date)) + 1


class OrderService:
    def __init__(self, prefix: str = "EVT") -> None:
        self._orders: list[EventOrder] = []
        self._sequence = 0
        self._current_dt: datetime | None = None
        self._prefix = prefix or "EVT"

    @property
    def pending(self) -> list[EventOrder]:
        return self._orders

    def set_current_dt(self, current_dt: datetime) -> None:
        self._current_dt = current_dt

    def target_quantity(self, symbol: str, quantity: int) -> EventOrder:
        return self._submit(EventOrder(symbol=symbol, quantity=quantity))

    def target_value(self, symbol: str, value: float) -> EventOrder:
        return self._submit(EventOrder(symbol=symbol, target_value=value))

    def close(self, symbol: str) -> EventOrder:
        return self.target_value(symbol, 0.0)

    def _submit(self, order: EventOrder) -> EventOrder:
        self._sequence += 1
        order.order_id = f"{self._prefix}-{self._sequence:06d}"
        order.submitted_at = self._current_dt
        self._orders.append(order)
        return order




class StrategyContext:
    def __init__(self, data: DataView, orders: OrderService, portfolio: PortfolioView) -> None:
        self.current_dt: datetime | None = None
        self.previous_date = None
        self.state = SimpleNamespace()
        self.data = data
        self.orders = orders
        self.portfolio = portfolio
        self.schedule = Scheduler()
        # 由事件引擎注入的当前时点市场视图（当前价、开盘价、涨跌停价、状态）。
        self.market = None
