"""A 股现金多头的日频 EventEngine MVP。"""

from __future__ import annotations

import re

from app.models.event import EventBacktestConfig, _Ledger
from datetime import datetime

import numpy as np
import pandas as pd

from app.models.event import EventBacktestResult, EventFill, EventOrder, PositionSnapshot
from app.models.paper_trading import PaperPositionState, SettlementDayResult
from app.models.market_data import is_delisted_status
from app.models.data import DataView
from app.services.strategy.event.strategy import OrderService, StrategyContext
from app.models.event import PortfolioView






class EventBacktestEngine:
    """策略在盘前基于截至前一收盘的数据决策，当日开盘按卖后买成交。"""

    required_fields = frozenset({"open", "high", "low", "close"})

    def __init__(self, panels: dict[str, pd.DataFrame], config: EventBacktestConfig | None = None) -> None:
        missing = self.required_fields - panels.keys()
        if missing:
            raise ValueError(f"事件回测缺少行情字段: {sorted(missing)}")
        self.panels = {name: frame.sort_index() for name, frame in panels.items()}
        self.config = config or EventBacktestConfig()
        self.dates = self.panels["close"].index
        self.symbols = list(self.panels["close"].columns)
        if self.dates.empty:
            raise ValueError("事件回测没有可用交易日")
        # 模拟盘单日结算模式：成交价改读 VWAP，估值改读未复权 close。
        # 默认关闭，现有区间回测行为不变。
        self._settlement_mode = False
        self._settlement_vwap: pd.DataFrame | None = None
        self._settlement_mark: pd.DataFrame | None = None
        # 状态面板真正覆盖到的标的；整列为空说明只是股票池列投影的产物。
        _listed = self.panels.get("listed_status")
        self._status_covered = (
            frozenset(_listed.columns[_listed.notna().any()])
            if _listed is not None
            else frozenset()
        )

    def run(self, strategy) -> EventBacktestResult:
        ledger = _Ledger(cash=self.config.initial_capital)
        data = DataView(self.panels)
        orders = OrderService()
        context = StrategyContext(data, orders, PortfolioView(ledger))
        strategy.initialize(context)
        all_orders: list[EventOrder] = []
        fills: list[EventFill] = []
        events: list[dict[str, object]] = []
        snapshots: list[PositionSnapshot] = []
        equity: list[float] = []

        for index, date in enumerate(self.dates):
            timestamp = pd.Timestamp(date)
            ledger.begin_session()
            offset = self._visibility_offset()
            visible = index - offset
            as_of = self.dates[visible] if visible >= 0 else None
            data.set_as_of(as_of)
            context.current_dt = timestamp.to_pydatetime()
            orders.set_current_dt(context.current_dt)
            events.append({"type": "SESSION_OPEN", "timestamp": context.current_dt})

            for callback in context.schedule.due(timestamp, self.dates):
                callback(context)
                events.append({"type": "SCHEDULED_CALLBACK", "timestamp": context.current_dt, "callback": callback.__name__})

            batch = list(orders.pending)
            orders.pending.clear()
            all_orders.extend(batch)
            day_fills, day_events = self._execute_pending(batch, ledger, timestamp)
            fills.extend(day_fills)
            events.extend(day_events)

            # 日终强平：放在策略回调与撮合之后，策略当日开盘的卖单先成交，
            # 这里只处置残余持仓；放在权益计算之前，使当日净值即反映退出。
            for order, fill in self._liquidate_delisted(ledger, timestamp):
                all_orders.append(order)
                fills.append(fill)
                events.append({
                    "type": "FORCED_LIQUIDATION",
                    "timestamp": context.current_dt,
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "reason_code": "DELISTED",
                })

            close_prices = self.panels["close"].loc[date]
            total_value = ledger.cash + sum(ledger.positions.get(symbol, 0) * float(close_prices[symbol]) for symbol in self.symbols if pd.notna(close_prices[symbol]))
            equity.append(total_value)
            snapshots.append(PositionSnapshot(context.current_dt, ledger.cash, dict(ledger.positions), dict(ledger.sellable), total_value))
            events.append({"type": "SESSION_CLOSE", "timestamp": context.current_dt})

        return EventBacktestResult(
            equity_curve=pd.Series(equity, index=self.dates, name="equity"),
            orders=all_orders,
            fills=fills,
            position_snapshots=snapshots,
            events=events,
            assumptions={
                "frequency": "1d",
                "execution": self.config.execution_timing,
                "data_visibility": (
                    "same_close"
                    if self.config.execution_timing == "same_close"
                    else "previous_close"
                ),
                "delisted_handling": "point_in_time_exclude;force_liquidate_at_transition_close",
            },
        )

    def _execute_pending(
        self,
        orders: list[EventOrder],
        ledger: _Ledger,
        date: pd.Timestamp,
    ) -> tuple[list[EventFill], list[dict[str, object]]]:
        """按卖先买顺序执行一批订单；回测与单日结算共用。"""

        fills: list[EventFill] = []
        events: list[dict[str, object]] = []
        for order in self._sort_orders(orders, ledger, date):
            fill = self._execute(order, ledger, date)
            if fill is None:
                events.append({
                    "type": "ORDER_REJECTED",
                    "timestamp": date.to_pydatetime(),
                    "order_id": order.order_id,
                    "reason_code": order.reason_code,
                })
            else:
                fills.append(fill)
                events.append({
                    "type": "FILL",
                    "timestamp": date.to_pydatetime(),
                    "order_id": order.order_id,
                })
        return fills, events

    def settle_day(
        self,
        strategy,
        *,
        settlement_date: pd.Timestamp,
        ledger: _Ledger,
        signal_date: pd.Timestamp | None = None,
        persisted_state: dict | None = None,
        mark_prices: pd.DataFrame | None = None,
        order_prefix: str = "PAPER",
    ) -> SettlementDayResult:
        """结算一个交易日：D-1 可见数据出信号，D 日 VWAP 全量成交。

        与区间回测的差异：
        - 不调用 ``begin_session``，可卖数量直接来自账户输入；
        - 不做退市强平，持仓只能由策略主动卖出；
        - 成交价统一取 ``vwap`` 面板，缺失时记 ``NO_VWAP``；
        - 权益用未复权 close 估值。
        """

        timestamp = pd.Timestamp(settlement_date).normalize()
        if timestamp not in self.dates:
            raise ValueError(f"结算日 {timestamp.date()} 不在行情面板中")
        loc = int(self.dates.get_loc(timestamp))
        if signal_date is None:
            if loc <= 0:
                raise ValueError("缺少前一交易日，无法生成信号")
            signal_timestamp = pd.Timestamp(self.dates[loc - 1])
        else:
            signal_timestamp = pd.Timestamp(signal_date).normalize()
        vwap = self.panels.get("vwap")
        if vwap is None:
            raise ValueError("单日结算缺少 vwap 面板")

        from app.services.paper_trading.state_codec import export_state, merge_state

        data = DataView(self.panels)
        orders = OrderService(prefix=order_prefix)
        context = StrategyContext(data, orders, PortfolioView(ledger))
        # 与区间回测一致：initialize 时尚未开放任何行情。
        strategy.initialize(context)
        merge_state(context.state, persisted_state)

        data.set_as_of(signal_timestamp)
        context.current_dt = timestamp.to_pydatetime()
        orders.set_current_dt(context.current_dt)
        for callback in context.schedule.due(timestamp, self.dates):
            callback(context)

        batch = list(orders.pending)
        orders.pending.clear()

        self._settlement_mode = True
        self._settlement_vwap = vwap
        self._settlement_mark = mark_prices
        try:
            fills, _events = self._execute_pending(batch, ledger, timestamp)
            positions, positions_value, total_equity, warnings = self._settlement_snapshot(
                ledger, timestamp, mark_prices
            )
        finally:
            self._settlement_mode = False
            self._settlement_vwap = None
            self._settlement_mark = None

        for order in batch:
            if order.reason_code == "NO_VWAP":
                warnings.append(f"NO_VWAP:{order.symbol}")
        state_payload, state_hash = export_state(context.state)
        commission = sum(fill.commission for fill in fills)
        stamp_tax = sum(fill.stamp_tax for fill in fills)
        return SettlementDayResult(
            settlement_date=timestamp.date(),
            signal_date=signal_timestamp.date(),
            orders=batch,
            fills=fills,
            next_cash=float(ledger.cash),
            next_positions=positions,
            commission=float(commission),
            stamp_tax=float(stamp_tax),
            strategy_state=state_payload,
            state_hash=state_hash,
            positions_value=float(positions_value),
            total_equity=float(total_equity),
            warnings=sorted(set(warnings)),
            assumptions={
                "frequency": "1d",
                "execution": "daily_vwap_full_fill",
                "signal_visibility": "through_previous_trading_day",
                "fill_price_source": "daily_amount_volume_vwap",
                "mark_price_source": "unadjusted_close",
                "lot_size": str(self.config.lot_size),
                "slippage": "not_modeled",
                "delisted_handling": "no_forced_liquidation",
                "company_actions": "not_modeled",
            },
        )

    def execute_orders(
        self,
        orders: list[EventOrder],
        *,
        settlement_date: pd.Timestamp,
        ledger: _Ledger,
        vwap: pd.DataFrame,
        mark_prices: pd.DataFrame | None = None,
    ) -> tuple[list[EventFill], list[PaperPositionState], float, float, list[str]]:
        """按结算模式执行一批已生成的订单（向量化策略复用）。"""

        timestamp = pd.Timestamp(settlement_date).normalize()
        self._settlement_mode = True
        self._settlement_vwap = vwap
        self._settlement_mark = mark_prices
        try:
            fills, _events = self._execute_pending(orders, ledger, timestamp)
            positions, positions_value, total_equity, warnings = self._settlement_snapshot(
                ledger, timestamp, mark_prices
            )
        finally:
            self._settlement_mode = False
            self._settlement_vwap = None
            self._settlement_mark = None
        return fills, positions, positions_value, total_equity, warnings

    def _settlement_snapshot(
        self,
        ledger: _Ledger,
        date: pd.Timestamp,
        mark_prices: pd.DataFrame | None,
    ) -> tuple[list[PaperPositionState], float, float, list[str]]:
        positions: list[PaperPositionState] = []
        warnings: list[str] = []
        positions_value = 0.0
        for item in ledger.export_positions():
            symbol = str(item["symbol"])
            quantity = int(item["quantity"])
            mark, used_fallback = self._mark_price(symbol, date)
            if mark is None:
                mark = float(item["average_cost"])
                warnings.append(f"MARK_PRICE_FALLBACK:{symbol}")
            elif used_fallback:
                warnings.append(f"MARK_PRICE_FALLBACK:{symbol}")
            positions_value += quantity * mark
            positions.append(PaperPositionState(
                symbol=symbol,
                quantity=quantity,
                available_quantity=int(item["available_quantity"]),
                average_cost=float(item["average_cost"]),
                mark_price=float(mark),
                market_value=float(quantity * mark),
            ))
        return positions, positions_value, float(ledger.cash + positions_value), warnings

    def _mark_price(self, symbol: str, date: pd.Timestamp) -> tuple[float | None, bool]:
        """D 日未复权 close；缺失时回退到 ``<=D`` 最后有效值。"""

        frame = self._settlement_mark
        if frame is not None and symbol in frame.columns:
            if date in frame.index:
                value = frame.at[date, symbol]
                if pd.notna(value) and np.isfinite(value) and value > 0:
                    return float(value), False
            earlier = frame[symbol].loc[:date].dropna()
            for candidate in reversed(earlier.tolist()):
                if np.isfinite(candidate) and candidate > 0:
                    return float(candidate), True
        # 未复权文件缺失时退回研究 close，只作为防御性回退并记录警告。
        close = self.panels.get("close")
        if close is not None and symbol in close.columns:
            if date in close.index:
                value = close.at[date, symbol]
                if pd.notna(value) and np.isfinite(value) and value > 0:
                    return float(value), True
        return None, True

    def _sort_orders(self, orders: list[EventOrder], ledger: _Ledger, date: pd.Timestamp) -> list[EventOrder]:
        return sorted(orders, key=lambda order: 0 if self._resolved_quantity(order, ledger, date) < 0 else 1)

    def _execute(self, order: EventOrder, ledger: _Ledger, date: pd.Timestamp) -> EventFill | None:
        price = self._price(order.symbol, date)
        if self._settlement_mode and price is None:
            # 模拟盘不提供 close/典型价回退；没有有效 VWAP 直接拒单。
            order.status, order.reason_code = "rejected", "NO_VWAP"
            return None
        quantity = self._resolved_quantity(order, ledger, date)
        if quantity == 0:
            order.status, order.reason_code = "rejected", "NO_POSITION"
            return None
        side = "buy" if quantity > 0 else "sell"
        quantity = abs(quantity)
        if price is None:
            order.status, order.reason_code = "rejected", "NO_PRICE"
            return None
        reason = self._reject_reason(order.symbol, side, quantity, price, ledger, date)
        if reason is not None:
            order.status, order.reason_code = "rejected", reason
            return None
        order.status, order.side = "filled", side
        return self._settle(
            order, side=side, quantity=quantity, price=price, ledger=ledger, date=date
        )

    def _execution_price(self, price: float, side: str) -> float:
        """按滑点把参考价调整为成交价：买入向上、卖出向下。

        滑点建模为「成交价比参考价更差」，因此它会体现在 ``EventFill.price``
        上（审计可见），并参与成交额、佣金与印花税的计算。费率为 0 时原样返回。
        """

        if self._settlement_mode:
            # FEAT-032 首期按 VWAP 全量成交，不模拟滑点。
            return price
        rate = self.config.slippage_rate
        if not rate:
            return price
        adjusted = price * (1.0 + rate) if side == "buy" else price * (1.0 - rate)
        # 卖出滑点不得把价格压到 0 或负数。
        return adjusted if adjusted > 0 else price

    def _settle(
        self,
        order: EventOrder,
        *,
        side: str,
        quantity: int,
        price: float,
        ledger: _Ledger,
        date: pd.Timestamp,
        force: bool = False,
    ) -> EventFill:
        """按买卖方向记账并返回成交回报。

        与策略订单共用同一条记账路径，保证退市强平的手续费、印花税与持仓
        口径和普通成交逐位一致。``force=True`` 用于引擎主动强平：强平绕过
        撮合层的 T+1 检查，卖出量可能超过当日已解禁数量，因此把 ``sellable``
        夹到 0 而不是让它变成负数，并清掉 ``bought_today``，避免次日
        ``begin_session`` 为已清仓的标的复活出幽灵可卖数量。
        """

        price = self._execution_price(price, side)
        value = quantity * price
        commission = max(value * self.config.commission_rate, self.config.min_commission)
        stamp_tax = value * self.config.stamp_tax_rate if side == "sell" else 0.0
        if side == "buy":
            current_quantity = ledger.positions.get(order.symbol, 0)
            current_cost = ledger.average_cost.get(order.symbol, 0.0)
            ledger.cash -= value + commission
            ledger.positions[order.symbol] = current_quantity + quantity
            ledger.average_cost[order.symbol] = (
                (current_quantity * current_cost + quantity * price)
                / ledger.positions[order.symbol]
            )
            ledger.bought_today[order.symbol] = ledger.bought_today.get(order.symbol, 0) + quantity
        else:
            ledger.cash += value - commission - stamp_tax
            ledger.positions[order.symbol] = ledger.positions.get(order.symbol, 0) - quantity
            ledger.sellable[order.symbol] = max(
                0, ledger.sellable.get(order.symbol, 0) - quantity
            )
            if force:
                ledger.bought_today.pop(order.symbol, None)
            if ledger.positions[order.symbol] <= 0:
                ledger.positions[order.symbol] = 0
                ledger.sellable[order.symbol] = 0
                ledger.average_cost.pop(order.symbol, None)
        return EventFill(order.order_id, order.symbol, side, quantity, price, date.to_pydatetime(), commission, stamp_tax)

    def _resolved_quantity(self, order: EventOrder, ledger: _Ledger, date: pd.Timestamp) -> int:
        current = ledger.positions.get(order.symbol, 0)
        if order.quantity is not None:
            return order.quantity - current
        price = self._price(order.symbol, date)
        if price is None:
            return 0
        desired = int(order.target_value / price / self.config.lot_size) * self.config.lot_size
        return desired - current

    def _fill_field(self) -> str:
        """按成交时点返回成交价字段：开盘价或收盘价。

        same_close / next_close 按收盘价撮合，next_open 按开盘价撮合。
        """

        return "open" if self.config.execution_timing == "next_open" else "close"

    def _visibility_offset(self) -> int:
        """策略回调可见数据的截止偏移。

        next_close / next_open 下信号在前一交易日收盘产生，回调只看得到
        ``index-1``；same_close 是「当日收盘出信号、当日收盘成交」的乐观
        口径（与因子/向量化路径的旧口径一致），回调因此能看到当日收盘。
        """

        return 0 if self.config.execution_timing == "same_close" else 1

    def _price(self, symbol: str, date: pd.Timestamp) -> float | None:
        """当日成交参考价。

        同时用于定量（``_resolved_quantity``）与成交价，两者取同一价格以保持
        自洽；退市强平不走这里，恒按转换日收盘价处置。
        """

        if symbol not in self.symbols:
            return None
        if self._settlement_mode:
            frame = self._settlement_vwap
            if frame is None or symbol not in frame.columns or date not in frame.index:
                return None
            value = frame.at[date, symbol]
            return (
                float(value)
                if pd.notna(value) and np.isfinite(value) and value > 0
                else None
            )
        value = self.panels[self._fill_field()].at[date, symbol]
        return float(value) if pd.notna(value) and value > 0 else None

    def _reject_reason(self, symbol: str, side: str, quantity: int, price: float, ledger: _Ledger, date: pd.Timestamp) -> str | None:
        # 当前 iFinD release 只提供 ``up_and_down_status``；状态字段缺失时
        # 按放行处理，原始文本按停牌及涨跌停状态解析。
        if self._status_matches(date, symbol, r"停牌"):
            return "SUSPENDED"
        if (
            side == "buy"
            and self.config.filter_st
            and self._listed_status_matches(date, symbol)
        ):
            # 只拦买入：已持仓标的转 ST 后仍要允许卖出，否则会被套死。
            return "ST"
        if side == "buy" and not self._investable(date, symbol):
            # 退市剔除恒开启。只拦买入：残余持仓由日终 ``_liquidate_delisted``
            # 按转换日收盘价强平，不需要在撮合层再拦一次卖出。
            return "DELISTED"
        if side == "buy" and self._status_matches(
            date, symbol, r"涨停|limit.?up|up.?limit"
        ):
            return "LIMIT_UP"
        if side == "sell" and self._status_matches(
            date, symbol, r"跌停|limit.?down|down.?limit"
        ):
            return "LIMIT_DOWN"
        if side == "buy":
            if quantity % self.config.lot_size:
                return "LOT_SIZE"
            # 现金校验必须用含滑点的成交价，否则买入成交后现金可能为负。
            value = quantity * self._execution_price(price, side)
            cost = value + max(value * self.config.commission_rate, self.config.min_commission)
            return "INSUFFICIENT_CASH" if cost > ledger.cash + 1e-9 else None
        return "T_PLUS_ONE" if quantity > ledger.sellable.get(symbol, 0) else None

    def _investable(self, date: pd.Timestamp, symbol: str) -> bool:
        """标的在该日是否处于可投资的上市状态。

        口径由 :func:`app.models.market_data.is_delisted_status` 唯一定义，
        与因子/向量化回测的 ``is_delisted`` 掩码共用同一份白名单。两种情况按
        可投资处理（数据降级，与四个可选过滤一致）：

        * 数据版本缺少 ``listed_status`` 字段；
        * 该标的未被状态面板覆盖（整列为空）—— 股票池按列投影时会为池内但
          无状态记录的标的造出一整列空值，这不等于退市。
        """

        frame = self.panels.get("listed_status")
        if frame is None or symbol not in frame.columns or date not in frame.index:
            return True
        if symbol not in self._status_covered:
            return True
        return not is_delisted_status(frame.at[date, symbol])

    def _liquidation_price(self, symbol: str, date: pd.Timestamp) -> float | None:
        """退市强平的成交价：转换日收盘价。

        实测依据：``close`` 在退市后被前值填充，因此转换日 ``close`` 在当日
        无成交时恒等于此前的最后成交价，当日有成交时就是真实成交价 —— 一条
        规则覆盖两种情形，无需再回溯查找「最后一个有成交量的交易日」。

        不能改用 ``_price``（读 ``open``）：转换日 ``open`` 在一半以上的退市
        样本里是缺失值，会直接触发 ``NO_PRICE`` 拒单导致强平失败。
        """

        if symbol not in self.symbols:
            return None
        close = self.panels["close"]
        value = close.at[date, symbol]
        if pd.notna(value) and value > 0:
            return float(value)
        # 防御性回退：转换日无收盘价时沿用此前最后一个有效收盘价。
        series = close[symbol].loc[:date].dropna()
        if series.empty:
            return None
        last = float(series.iloc[-1])
        return last if last > 0 else None

    def _liquidate_delisted(
        self, ledger: _Ledger, date: pd.Timestamp
    ) -> list[tuple[EventOrder, EventFill]]:
        """日终按转换日收盘价出清已不可投资的持仓。

        这是引擎的主动行为，不受策略订单的撮合限制（T+1、涨跌停、停牌、
        整手）约束 —— 退市标的本来就不该让仓位被冻结价格一直吊着。持仓在
        转换日收盘后变为 0，当日权益曲线即反映这笔损失。
        """

        results: list[tuple[EventOrder, EventFill]] = []
        for symbol in list(ledger.positions):
            quantity = ledger.positions.get(symbol, 0)
            if quantity <= 0 or self._investable(date, symbol):
                continue
            price = self._liquidation_price(symbol, date)
            if price is None:
                continue
            order = EventOrder(
                symbol=symbol,
                quantity=-quantity,
                order_id=f"engine-delist-{pd.Timestamp(date):%Y%m%d}-{symbol}",
                submitted_at=pd.Timestamp(date).to_pydatetime(),
                side="sell",
                status="filled",
                reason_code="DELISTED",
            )
            results.append(
                (
                    order,
                    self._settle(
                        order,
                        side="sell",
                        quantity=quantity,
                        price=price,
                        ledger=ledger,
                        date=date,
                        force=True,
                    ),
                )
            )
        return results

    def _listed_status_matches(self, date: pd.Timestamp, symbol: str) -> bool:
        """判断标的在该日的上市状态是否为 ST；字段缺失按放行处理。

        取值与 ``DataEngine.build_status_masks`` 的 ``is_st`` 掩码保持一致，
        使事件与向量化/因子回测的 ST 口径统一。
        """

        frame = self.panels.get("listed_status")
        if frame is None or symbol not in frame.columns or date not in frame.index:
            return False
        value = frame.at[date, symbol]
        if pd.isna(value):
            return False
        return str(value).strip() in {"ST", "*ST"}

    def _status_matches(self, date: pd.Timestamp, symbol: str, pattern: str) -> bool:
        frame = self.panels.get("up_and_down_status")
        if frame is None or symbol not in frame.columns or date not in frame.index:
            return False
        value = frame.at[date, symbol]
        if pd.isna(value):
            return False
        text = str(value).strip()
        if re.search(
            r"(?:非|未|not\s*)(?:涨跌停|涨停|跌停|limit.?up|up.?limit|limit.?down|down.?limit)",
            text,
            flags=re.IGNORECASE,
        ):
            return False
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
