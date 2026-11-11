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
from app.services.strategy.event.strategy import OrderService, StrategyContext, normalize_time
from app.models.event import PortfolioView






def _row_slice(
    frame: pd.DataFrame,
    *,
    start: object | None = None,
    end: object | None = None,
) -> pd.DataFrame:
    """按日期区间切行；用 searchsorted + iloc，避免布尔 loc 的全表 take。

    面板是「全历史 × 5000 列」，``loc[bool]`` 每次会构造新块并复制整段，
    实测 2428 行 × 5000 列约 22 毫秒；iloc 位置切片约 0.1 毫秒。
    """

    index = frame.index
    if not index.is_monotonic_increasing:
        subset = frame
        if start is not None:
            subset = subset.loc[subset.index >= pd.Timestamp(start)]
        if end is not None:
            subset = subset.loc[subset.index <= pd.Timestamp(end)]
        return subset
    lower = (
        int(index.searchsorted(pd.Timestamp(start), side="left"))
        if start is not None
        else 0
    )
    upper = (
        int(index.searchsorted(pd.Timestamp(end), side="right"))
        if end is not None
        else len(index)
    )
    if lower == 0 and upper >= len(index):
        return frame
    return frame.iloc[lower:upper]


def _limit_ratios(columns: pd.Index) -> np.ndarray:
    """按板块生成涨跌停比例向量（主板 10%、创业板/科创板 20%、北交所 30%）。"""

    ratios = np.full(len(columns), 0.10, dtype=float)
    for position, column in enumerate(columns):
        code = str(column).split(".")[0]
        if code.startswith(("300", "301", "688")):
            ratios[position] = 0.20
        elif code.startswith(("4", "8", "920")):
            ratios[position] = 0.30
    return ratios


class EventBacktestEngine:
    """策略在盘前基于截至前一收盘的数据决策，当日开盘按卖后买成交。"""

    required_fields = frozenset({"open", "high", "low", "close"})

    def __init__(
        self,
        panels: dict[str, pd.DataFrame],
        config: EventBacktestConfig | None = None,
        *,
        execution_panels: dict[str, pd.DataFrame] | None = None,
        intraday_price=None,
        first_previous_date: object | None = None,
        active_start: object | None = None,
        etf_symbols: set[str] | None = None,
        symbol_aliases: dict[str, str] | None = None,
    ) -> None:
        missing = self.required_fields - panels.keys()
        if missing:
            raise ValueError(f"事件回测缺少行情字段: {sorted(missing)}")
        self.panels = {name: frame.sort_index() for name, frame in panels.items()}
        self.config = config or EventBacktestConfig()
        # panels 可以带预热区间：回测只从 active_start 开始，但首日之前的
        # 行情仍可被 get_price/history/涨跌停读取。
        self._full_dates = self.panels["close"].index
        if active_start is not None:
            self.dates = self._full_dates[
                self._full_dates >= pd.Timestamp(active_start)
            ]
        else:
            self.dates = self._full_dates
        self.symbols = list(self.panels["close"].columns)
        # 额外并入面板的 ETF：它们参与撮合与估值，但不算「股票」，
        # get_all_securities(['stock']) 需要把它们排除。
        self.etf_symbols = frozenset(etf_symbols or ())
        # 策略里写死的代码（如 511880.XSHG）与引擎代码（511880.SH）之间的
        # 别名；持仓与订单一律按策略侧的写法回报，保证字符串比较成立。
        self.symbol_aliases = dict(symbol_aliases or {})
        if self.dates.empty:
            raise ValueError("事件回测没有可用交易日")
        # 首次/末次有效报价：聚宽兼容层的 get_all_securities 会逐标的查询，
        # 逐列 dropna 在 5000+ 标的上是 O(标的×日期)；这里一次性向量化。
        self._listing_bounds: (
            dict[str, tuple[pd.Timestamp | None, pd.Timestamp | None]] | None
        ) = None
        # 逐日行缓存：宽表 ``frame.at[date, symbol]`` 的标量访问在策略高频
        # 查询（current_data / 涨跌停状态）下会退化成 O(列) 的 pandas 定位，
        # 每个交易日只把当天需要的行转成 dict，后续都是 O(1)。
        self._row_cache: dict[tuple[int, pd.Timestamp], pd.Series] = {}
        # 盘中价缓存：同一时点、同一标的会被 portfolio.positions 反复取价，
        # 分钟面板命中后不再重复走时间点逻辑；逐日清空。
        self._price_cache: dict[tuple[pd.Timestamp, str, str], float | None] = {}
        # 当日停牌/ST 标的集合：get_current_data 会逐标的检查，按天缓存成
        # 集合后是 O(1) 成员判断，避免每次走文本比较。
        self._day_code_cache: dict[tuple[str, object], frozenset[str]] = {}
        # 状态面板（ST / 停牌）的布尔矩阵缓存：宽表字符串比较按列很慢，
        # 首次使用时整表向量化一次，之后每天只做 numpy 切片。
        self._status_matrix_cache: dict[
            tuple[str, str | tuple[str, ...]],
            tuple[pd.Index, pd.Index, np.ndarray],
        ] = {}
        # 聚宽 get_orders/get_open_orders 需要回看已处理订单与成交量。
        self._all_orders: list[EventOrder] = []
        self._all_fills: list[EventFill] = []
        self._fill_quantities: dict[str, int] = {}

        # 成交/估值价格来源。adjusted 保持原有行为；unadjusted 用未复权面板
        # 成交与估值，信号和上下文历史仍读 self.panels（复权）。
        self._execution_price_source = str(self.config.execution_price_source).lower()
        if self._execution_price_source not in {"adjusted", "unadjusted"}:
            raise ValueError(
                f"不支持的 execution_price_source: {self.config.execution_price_source!r}"
            )
        self._price_panels = self.panels
        self._mark_close = self.panels["close"]
        self._adjustment_ratio: pd.DataFrame | None = None
        if self._execution_price_source == "unadjusted":
            if not execution_panels or "open" not in execution_panels or "close" not in execution_panels:
                raise ValueError(
                    "execution_price_source='unadjusted' 需要 execution_panels "
                    "同时提供 open 与 close"
                )
            self._price_panels = {
                name: frame.reindex(index=self._full_dates, columns=self.symbols)
                for name, frame in execution_panels.items()
            }
            self._mark_close = self._price_panels["close"].ffill()
            adjusted_close = self.panels["close"].reindex(
                index=self._full_dates, columns=self.symbols
            )
            factor = (adjusted_close / self._price_panels["close"]).replace(
                [np.inf, -np.inf], np.nan
            )
            # 停牌期间因子缺失时沿用最后一个有效值，复牌日一次性补记公司行为。
            factor = factor.ffill()
            self._adjustment_ratio = factor / factor.shift(1)
        # 模拟盘单日结算模式：成交价改读 VWAP，估值改读未复权 close。
        # 默认关闭，现有区间回测行为不变。
        # 时点引擎使用的盘中价格回调：``(date, "HH:MM", symbol) -> price``。
        # 仅 unadjusted 模式启用；缺失时回退到日线 open/close。
        self._intraday_price = intraday_price
        self._current_time: str | None = None
        self._current_date: pd.Timestamp | None = None
        # 聚宽兼容层在回测首日也有 previous_date；原生事件策略保持 None。
        if first_previous_date is not None:
            self._first_previous_date = pd.Timestamp(first_previous_date)
        elif len(self.dates) < len(self._full_dates):
            location = int(self._full_dates.get_loc(self.dates[0]))
            self._first_previous_date = (
                pd.Timestamp(self._full_dates[location - 1])
                if location > 0
                else None
            )
        else:
            self._first_previous_date = None
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

    def _run_params(self):
        """聚宽 ``context.run_params`` 的最小实现（日频区间回测）。"""

        from types import SimpleNamespace

        dates = getattr(self, "dates", None)
        start = dates[0].date() if dates is not None and len(dates) else None
        end = dates[-1].date() if dates is not None and len(dates) else None
        return SimpleNamespace(
            type="simple_backtest",
            frequency="day",
            start_date=start,
            end_date=end,
        )

    def run(self, strategy) -> EventBacktestResult:
        """唯一的日循环：日频与时点执行只在调度/撮合细节上分支。

        * 日频（``timed_execution=False``）：一天一次回调，回调结束后统一撮合；
        * 时点（``timed_execution=True``）：按注册时点逐点回调与撮合，
          09:30–14:59 之外产生的订单挂到下一个可成交时点。
        """

        timed = bool(self.config.timed_execution)
        ledger = _Ledger(cash=self.config.initial_capital)
        data = DataView(self.panels)
        orders = OrderService()
        context = StrategyContext(data, orders, PortfolioView(ledger))
        context.market = _MarketView(self)
        context.run_params = self._run_params()
        strategy.initialize(context)

        times: list[str] = []
        if timed:
            registered = {
                normalize_time(item) for item in context.schedule.times()
            }
            unsupported = sorted(
                item for item in registered if not re.fullmatch(r"\d{2}:\d{2}", item)
            )
            if unsupported:
                raise ValueError(f"时点引擎不支持的调度时点: {unsupported}")
            times = sorted({"09:30"} | registered)

        self._reset_order_history()
        all_orders = self._all_orders
        fills = self._all_fills
        events: list[dict[str, object]] = []
        snapshots: list[PositionSnapshot] = []
        equity: list[float] = []

        for index, date in enumerate(self.dates):
            timestamp = pd.Timestamp(date)
            self._current_date = timestamp
            self._row_cache.clear()
            self._price_cache.clear()
            self._day_code_cache.clear()
            previous = self._previous_date(index)
            context.previous_date = previous.date() if previous is not None else None
            ledger.begin_session()
            events.extend(self._apply_corporate_adjustments(ledger, index, date))

            if timed:
                # 聚宽定时回测的可见数据固定为上一交易日。
                data.set_as_of(previous)
                events.append({
                    "type": "SESSION_OPEN",
                    "timestamp": self._at(timestamp, "09:00"),
                })
                for time in times:
                    self._current_time = time
                    context.current_dt = self._at(timestamp, time)
                    orders.set_current_dt(context.current_dt)
                    for callback in context.schedule.due_at(timestamp, self.dates, time):
                        callback(context)
                        events.append({
                            "type": "TIMED_CALLBACK",
                            "timestamp": context.current_dt,
                            "time": time,
                            "callback": callback.__name__,
                        })
                    if not ("09:30" <= time < "15:00"):
                        # 开盘前与收盘后的订单都挂起，等下一个可成交时点。
                        continue
                    batch = list(orders.pending)
                    orders.pending.clear()
                    all_orders.extend(batch)
                    day_fills, day_events = self._execute_pending(batch, ledger, timestamp)
                    fills.extend(day_fills)
                    events.extend(day_events)
                    self._current_time = None
                close_dt = self._at(
                    timestamp, max("15:00", times[-1]) if times else "15:00"
                )
            else:
                offset = self._visibility_offset()
                visible = index - offset
                as_of = (
                    self.dates[visible]
                    if visible >= 0
                    else self._first_previous_date
                )
                data.set_as_of(as_of)
                context.current_dt = timestamp.to_pydatetime()
                orders.set_current_dt(context.current_dt)
                events.append({
                    "type": "SESSION_OPEN",
                    "timestamp": context.current_dt,
                })
                for callback in context.schedule.due(timestamp, self.dates):
                    callback(context)
                    events.append({
                        "type": "SCHEDULED_CALLBACK",
                        "timestamp": context.current_dt,
                        "callback": callback.__name__,
                    })
                batch = list(orders.pending)
                orders.pending.clear()
                all_orders.extend(batch)
                day_fills, day_events = self._execute_pending(batch, ledger, timestamp)
                fills.extend(day_fills)
                events.extend(day_events)
                close_dt = timestamp.to_pydatetime()

            for order, fill in self._liquidate_delisted(ledger, timestamp):
                all_orders.append(order)
                fills.append(fill)
                events.append({
                    "type": "FORCED_LIQUIDATION",
                    "timestamp": self._at(timestamp, "15:00"),
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "reason_code": "DELISTED",
                })

            context.current_dt = close_dt
            total_value = self._portfolio_value(ledger, timestamp)
            equity.append(total_value)
            snapshots.append(PositionSnapshot(
                context.current_dt,
                ledger.cash,
                dict(ledger.positions),
                dict(ledger.sellable),
                total_value,
            ))
            events.append({
                "type": "SESSION_CLOSE",
                "timestamp": context.current_dt,
            })

        assumptions = {
            "frequency": "1d",
            "delisted_handling": "point_in_time_exclude;force_liquidate_at_transition_close",
            "limit_price_source": (
                "derived_limit_prices"
                if "upper_limit" in self.panels
                else "computed_prev_close_ratio"
            ),
            "price_source": self._execution_price_source,
            "corporate_action_adjustment": self._corporate_action_adjustment(),
        }
        if timed:
            assumptions.update({
                "execution": "timed",
                "time_points": ",".join(times),
                "data_visibility": "previous_close",
                "intraday_price": (
                    "1m"
                    if self._intraday_price is not None
                    and self._execution_price_source == "unadjusted"
                    else "daily_open_close_fallback"
                ),
            })
        else:
            assumptions.update({
                "execution": self.config.execution_timing,
                "data_visibility": (
                    "same_close"
                    if self.config.execution_timing == "same_close"
                    else "previous_close"
                ),
            })

        return EventBacktestResult(
            equity_curve=pd.Series(equity, index=self.dates, name="equity"),
            orders=all_orders,
            fills=fills,
            position_snapshots=snapshots,
            events=events,
            assumptions=assumptions,
        )

    @staticmethod
    def _at(date: pd.Timestamp, time: str) -> datetime:
        normalized = normalize_time(time)
        return pd.Timestamp(
            f"{pd.Timestamp(date).strftime('%Y-%m-%d')} {normalized}:00"
        ).to_pydatetime()

    def _event_value(self, panel_name: str, symbol: str, date: pd.Timestamp) -> float:
        """读取分红/送转面板的单值；缺失或非有限值按 0 处理。"""

        frame = self.panels.get(panel_name)
        if frame is None or symbol not in frame.columns or date not in frame.index:
            return 0.0
        value = frame.at[date, symbol]
        if pd.isna(value) or not np.isfinite(value):
            return 0.0
        return float(value)

    def _apply_cash_dividends(
        self, ledger: _Ledger, date: pd.Timestamp
    ) -> list[dict[str, object]]:
        """聚宽现金分红口径：派息入现金、送股增股，不自动再投资。

        与再投资口径的区别只在事件日之后的暴露：现金留在账户，等策略自己
        决定何时买入。现金分红按 ``dividend_tax_rate`` 预扣红利税后入账
        （聚宽真实价格模式统一 20%）；送股不足 1 股向下取整。
        """

        events: list[dict[str, object]] = []
        tax_rate = float(self.config.dividend_tax_rate)
        for symbol in list(ledger.positions):
            shares = int(ledger.positions.get(symbol, 0))
            if shares <= 0:
                continue
            cash_per_share = self._event_value("cash_dividend", symbol, date)
            bonus_per_share = self._event_value("stock_bonus", symbol, date)
            if cash_per_share <= 0 and bonus_per_share <= 0:
                continue
            gross_cash = shares * cash_per_share
            tax = gross_cash * tax_rate if cash_per_share > 0 else 0.0
            cash = gross_cash - tax
            bonus_shares = (
                int(np.floor(shares * bonus_per_share))
                if bonus_per_share > 0 else 0
            )
            new_shares = shares + bonus_shares
            if cash:
                ledger.cash += cash
            if bonus_shares:
                ledger.positions[symbol] = new_shares
                ledger.sellable[symbol] = min(
                    new_shares,
                    int(ledger.sellable.get(symbol, 0)) + bonus_shares,
                )
            if symbol in ledger.average_cost:
                total_cost = ledger.average_cost[symbol] * shares - cash
                ledger.average_cost[symbol] = (
                    max(total_cost, 0.0) / new_shares if new_shares else 0.0
                )
            events.append({
                "type": "CORPORATE_ACTION_ADJUSTMENT",
                "timestamp": date.to_pydatetime(),
                "symbol": symbol,
                "model": "cash",
                "cash_dividend": float(cash),
                "cash_dividend_gross": float(gross_cash),
                "dividend_tax": float(tax),
                "stock_bonus_shares": int(bonus_shares),
                "previous_quantity": shares,
                "quantity": new_shares,
            })
        return events

    def _corporate_action_adjustment(self) -> str:
        """assumptions 里的公司行为口径标签。"""

        if self._execution_price_source != "unadjusted":
            return "embedded_in_adjusted_prices"
        if self.config.company_action_model != "cash":
            return "share_scaling_by_adjusted_over_unadjusted_ratio"
        if self.config.dividend_tax_rate > 0:
            percent = round(self.config.dividend_tax_rate * 100, 2)
            text = f"{percent:g}".replace(".", "p")
            return f"cash_dividend_net_{text}pct_tax"
        return "cash_dividend_gross_no_tax"

    def _apply_corporate_adjustments(
        self, ledger: _Ledger, index: int, date: pd.Timestamp
    ) -> list[dict[str, object]]:
        """按复权比例缩放持仓，等价于分红送转再投资。

        未复权模式下，持仓以真实股数记账；为让收益与后复权序列一致，除权日
        开盘前把股数乘以 ``(hfq/unadj)_t / (hfq/unadj)_{t-1}``。取整余数按当日
        未复权开盘价折算成现金，避免凭空多出或少掉价值。
        """

        if (
            self._execution_price_source != "unadjusted"
            or index == 0
        ):
            return []
        if self.config.company_action_model == "cash":
            return self._apply_cash_dividends(ledger, date)
        if self._adjustment_ratio is None:
            return []
        ratios = self._adjustment_ratio.loc[date]
        events: list[dict[str, object]] = []
        for symbol in list(ledger.positions):
            quantity = int(ledger.positions.get(symbol, 0))
            if quantity <= 0:
                continue
            ratio_value = ratios.get(symbol)
            if ratio_value is None or not np.isfinite(ratio_value) or ratio_value <= 0:
                continue
            ratio = float(ratio_value)
            if abs(ratio - 1.0) < 1e-9:
                continue

            raw_quantity = quantity * ratio
            adjusted_quantity = int(np.floor(raw_quantity))
            residual = raw_quantity - adjusted_quantity
            sellable = int(ledger.sellable.get(symbol, 0))
            ledger.positions[symbol] = adjusted_quantity
            ledger.sellable[symbol] = min(
                adjusted_quantity, int(np.floor(sellable * ratio))
            )
            if symbol in ledger.average_cost:
                ledger.average_cost[symbol] = ledger.average_cost[symbol] / ratio

            cash_in_lieu = 0.0
            if residual > 0:
                price = self._adjustment_price(symbol, date)
                if price is not None:
                    cash_in_lieu = residual * price
                    ledger.cash += cash_in_lieu

            if adjusted_quantity <= 0:
                ledger.positions.pop(symbol, None)
                ledger.sellable.pop(symbol, None)
                ledger.average_cost.pop(symbol, None)

            events.append({
                "type": "CORPORATE_ACTION_ADJUSTMENT",
                "timestamp": date.to_pydatetime(),
                "symbol": symbol,
                "ratio": ratio,
                "previous_quantity": quantity,
                "quantity": adjusted_quantity,
                "cash_in_lieu": float(cash_in_lieu),
            })
        return events

    def _adjustment_price(self, symbol: str, date: pd.Timestamp) -> float | None:
        """取整余数的折算价：优先当日未复权开盘价，其次最后有效收盘价。"""

        for frame in (self._price_panels.get("open"), self._mark_close):
            if frame is None or symbol not in frame.columns:
                continue
            value = frame[symbol].get(date)
            if value is not None and pd.notna(value) and np.isfinite(value) and value > 0:
                return float(value)
        return None

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
                self._fill_quantities[order.order_id] = (
                    self._fill_quantities.get(order.order_id, 0) + int(fill.quantity)
                )
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
        context.market = _MarketView(self)
        context.run_params = self._run_params()
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
        # ETF（基金份额）卖出不收印花税；聚宽 set_order_cost(type='fund') 同样不设。
        tax_exempt = order.symbol in self.etf_symbols
        stamp_tax = (
            value * self.config.stamp_tax_rate
            if side == "sell" and not tax_exempt
            else 0.0
        )
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
        fill_timestamp = date.to_pydatetime()
        if self.config.timed_execution and self._current_time is not None:
            fill_timestamp = self._at(date, self._current_time)
        return EventFill(
            order.order_id,
            order.symbol,
            side,
            quantity,
            price,
            fill_timestamp,
            commission,
            stamp_tax,
        )

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

    def _row_values(
        self, frame: pd.DataFrame | None, date: pd.Timestamp | None
    ) -> pd.Series | None:
        """缓存某一日整行；日期切换时清空。

        返回 Series 而不是 dict：宽表有 5000+ 列，``to_dict()`` 会把整行
        转成 Python 对象，ARROW 字符串列下每次约 60 毫秒；调用方只用
        ``.get(symbol)``，Series 直接按标签取即可。
        """

        if frame is None or date is None:
            return None
        key = (id(frame), date)
        values = self._row_cache.get(key)
        if values is None:
            if date not in frame.index:
                return None
            values = frame.loc[date]
            if len(self._row_cache) > 64:
                self._row_cache.clear()
            self._row_cache[key] = values
        return values

    def _portfolio_value(self, ledger, date: pd.Timestamp) -> float:
        """按收盘价估值：只遍历持仓，避免每天对全市场做 Series 标量定位。"""

        row = self._row_values(self._mark_close, date)
        total = float(ledger.cash)
        if row is None:
            return total
        for symbol, quantity in ledger.positions.items():
            if not quantity:
                continue
            price = row.get(symbol)
            if price is not None and pd.notna(price):
                total += float(quantity) * float(price)
        return total

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
        if self.config.timed_execution and self._current_time is not None:
            return self._price_at_time(symbol, date, self._current_time)
        return self._price_from_panel(symbol, date, self._fill_field())

    def _price_from_panel(
        self, symbol: str, date: pd.Timestamp, field: str
    ) -> float | None:
        frame = self._price_panels.get(field)
        if frame is None or symbol not in frame.columns:
            return None
        values = self._row_values(frame, date)
        if values is None:
            return None
        value = values.get(symbol)
        return float(value) if pd.notna(value) and value > 0 else None

    def _price_at_time(
        self, symbol: str, date: pd.Timestamp, time: str
    ) -> float | None:
        """时点成交价：优先 1m 数据，缺失时用日线 open/close 近似。"""

        if not ("09:30" <= time < "15:00"):
            return None
        key = (pd.Timestamp(date), time, symbol)
        if key in self._price_cache:
            return self._price_cache[key]
        value = self._compute_price_at_time(symbol, date, time)
        if len(self._price_cache) > 8192:
            self._price_cache.clear()
        self._price_cache[key] = value
        return value

    def _compute_price_at_time(
        self, symbol: str, date: pd.Timestamp, time: str
    ) -> float | None:
        if time == "09:30":
            # 聚宽 09:30 看到的 last_price 与成交价都是当日开盘价，不是首根
            # 分钟线的收盘价。
            return self._price_from_panel(symbol, date, "open")
        if self._intraday_price is not None and self._execution_price_source == "unadjusted":
            try:
                value = self._intraday_price(pd.Timestamp(date), time, symbol)
            except Exception:  # noqa: BLE001 - 分钟数据缺失时回退日线
                value = None
            if value is not None and np.isfinite(value) and value > 0:
                return float(value)
        # 无分钟数据时的近似：上午时点用开盘价，下午时点用收盘价。
        field = "open" if time < "12:00" else "close"
        return self._price_from_panel(symbol, date, field)

    def _limit_from_panels(
        self, symbol: str, date: pd.Timestamp
    ) -> tuple[float | None, float | None]:
        """读取数据侧涨跌停价；缺失字段或日期时返回 (None, None)。"""

        high = low = None
        for field, target in (
            ("upper_limit", "high"), ("lower_limit", "low"),
        ):
            frame = self.panels.get(field)
            if frame is None or symbol not in frame.columns or date not in frame.index:
                continue
            value = frame.at[date, symbol]
            if pd.notna(value) and np.isfinite(value) and value > 0:
                if target == "high":
                    high = float(value)
                else:
                    low = float(value)
        return high, low

    def _reject_reason(self, symbol: str, side: str, quantity: int, price: float, ledger: _Ledger, date: pd.Timestamp) -> str | None:
        # 停牌/ST/退市优先按状态字段判断；涨跌停优先按数据侧的
        # ``upper_limit`` / ``lower_limit``，缺失时回退到状态文本。
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
        high, low = self._limit_from_panels(symbol, date)
        if high is not None or low is not None:
            # 与聚宽日频撮合一致：买入成交价达到涨停价、卖出达到跌停价时
            # 视为无法成交；仅触及但未封死的情况由日线数据无法区分。
            if side == "buy" and high is not None and price >= high - 1e-9:
                return "LIMIT_UP"
            if side == "sell" and low is not None and price <= low + 1e-9:
                return "LIMIT_DOWN"
        else:
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
        close = self._mark_close
        if date not in close.index:
            return None
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

    def _reset_order_history(self) -> None:
        self._all_orders.clear()
        self._all_fills.clear()
        self._fill_quantities.clear()

    def _listing_bounds_for(
        self, symbol: str
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        """标的首次/末次有效报价日期；结果按引擎生命周期缓存。"""

        if self._listing_bounds is None:
            frame = self._mark_close
            valid = frame.notna()
            has_value = valid.any()
            first = valid.idxmax()
            last = valid.iloc[::-1].idxmax()
            self._listing_bounds = {
                str(item): (
                    pd.Timestamp(first[item]) if bool(has_value[item]) else None,
                    pd.Timestamp(last[item]) if bool(has_value[item]) else None,
                )
                for item in frame.columns
            }
        return self._listing_bounds.get(str(symbol), (None, None))

    def _previous_date(self, index: int) -> pd.Timestamp | None:
        """当前交易日的上一交易日；首日按 ``first_previous_date`` 回退。"""

        if index > 0:
            return pd.Timestamp(self.dates[index - 1])
        return self._first_previous_date

    def _previous_close(self, symbol: str) -> float | None:
        """当前交易日的上一交易日收盘价，用于涨跌停价计算。"""

        date = self._current_date
        if date is None or date not in self._full_dates:
            return None
        location = int(self._full_dates.get_loc(date))
        if location <= 0:
            return None
        previous = self._full_dates[location - 1]
        frame = self._price_panels.get("close")
        if frame is None or symbol not in frame.columns:
            return None
        values = self._row_values(frame, previous)
        if values is None:
            return None
        value = values.get(symbol)
        return float(value) if pd.notna(value) and value > 0 else None

    def _panel_text(self, panel: str, symbol: str) -> str | None:
        frame = self.panels.get(panel)
        if frame is None or symbol not in frame.columns:
            return None
        values = self._row_values(frame, self._current_date)
        if values is None:
            return None
        value = values.get(symbol)
        return None if pd.isna(value) else str(value).strip()

    def _status_matches(self, date: pd.Timestamp, symbol: str, pattern: str) -> bool:
        frame = self.panels.get("up_and_down_status")
        if frame is None or symbol not in frame.columns:
            return False
        values = self._row_values(frame, date)
        if values is None:
            return False
        value = values.get(symbol)
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


class _MarketView:
    """注入策略上下文的当前时点市场视图。

    供聚宽兼容层实现 ``get_current_data()``；所有取值都基于引擎当前日期与
    时点，不读取未来数据。
    """

    def __init__(self, engine: EventBacktestEngine) -> None:
        self._engine = engine

    def _status_matrix(
        self, name: str, values: str | tuple[str, ...]
    ) -> tuple[pd.Index, pd.Index, np.ndarray] | None:
        """把状态面板（字符串）一次性转成布尔矩阵并缓存。

        状态面板是 Arrow 字符串宽表（2600×5900），任何按列的字符串比较
        都要几秒；回测里每天都会按窗口取一次，必须预先向量化成 numpy。
        """

        engine = self._engine
        key = (name, values)
        cached = engine._status_matrix_cache.get(key)
        if cached is not None:
            return cached
        panel = engine.panels.get(name)
        if panel is None:
            return None
        mask = (
            panel.isin(list(values))
            if isinstance(values, tuple)
            else panel.eq(values)
        )
        cached = (mask.index, mask.columns, mask.to_numpy(dtype=bool))
        engine._status_matrix_cache[key] = cached
        return cached

    def _status_frame(
        self,
        cached: tuple[pd.Index, pd.Index, np.ndarray],
        symbols: list[str],
        *,
        count: int | None = None,
        start: object | None = None,
        end: object | None = None,
    ) -> pd.DataFrame:
        """按行窗口与标的投影布尔矩阵；缺失标的按 False 处理。"""

        index, columns, matrix = cached
        positions = index
        if start is not None:
            positions = positions[positions >= pd.Timestamp(start)]
        if end is not None:
            positions = positions[positions <= pd.Timestamp(end)]
        rows = index.get_indexer(positions)
        if count is not None:
            rows = rows[-int(count):]
            positions = positions[-int(count):]
        width = columns.get_indexer(pd.Index(symbols))
        block = np.zeros((len(rows), len(width)), dtype=bool)
        present = width >= 0
        if rows.size and present.any():
            block[:, present] = matrix[np.ix_(rows, width[present])]
        return pd.DataFrame(block, index=positions, columns=symbols)

    def paused_codes(self) -> frozenset[str]:
        return self._day_codes("up_and_down_status", "停牌")

    def st_codes(self) -> frozenset[str]:
        return self._day_codes("listed_status", ("ST", "*ST"))

    def _day_codes(self, panel: str, values) -> frozenset[str]:
        engine = self._engine
        date = engine._current_date
        key = (panel, values)
        cached = engine._day_code_cache.get(key)
        if cached is not None:
            return cached
        matrix = self._status_matrix(panel, values)
        if matrix is None or date is None:
            result: frozenset[str] = frozenset()
        else:
            index, columns, block = matrix
            if date not in index:
                result = frozenset()
            else:
                result = frozenset(columns[block[index.get_loc(date)]])
        engine._day_code_cache[key] = result
        return result

    def current_price(self, symbol: str) -> float | None:
        engine = self._engine
        date = engine._current_date
        if date is None:
            return None
        time = engine._current_time
        if time is None:
            return engine._price(symbol, date)
        if time < "09:30":
            return engine._previous_close(symbol)
        if time >= "15:00":
            frame = engine._mark_close
            if date not in frame.index or symbol not in frame.columns:
                return None
            value = frame.at[date, symbol]
            return float(value) if pd.notna(value) and value > 0 else None
        return engine._price_at_time(symbol, date, time)

    def day_open(self, symbol: str) -> float | None:
        engine = self._engine
        if engine._current_date is None:
            return None
        return engine._price_from_panel(symbol, engine._current_date, "open")

    def previous_close(self, symbol: str) -> float | None:
        return self._engine._previous_close(symbol)

    def paused_panel(
        self,
        symbols: list[str],
        *,
        count: int | None = None,
        start: object | None = None,
        end: object | None = None,
    ) -> pd.DataFrame | None:
        """历史停牌标记（1=停牌），供聚宽 ``get_price(fields=['paused'])`` 使用。"""

        cached = self._status_matrix("up_and_down_status", "停牌")
        if cached is None:
            return None
        block = self._status_frame(
            cached, symbols, count=count, start=start, end=end
        )
        return block.astype(np.int64)

    def status_text(self, symbol: str) -> str | None:
        return self._engine._panel_text("up_and_down_status", symbol)

    def listed_status(self, symbol: str) -> str | None:
        return self._engine._panel_text("listed_status", symbol)

    def name(self, symbol: str) -> str:
        return str(symbol)

    @property
    def symbols(self) -> list[str]:
        return list(self._engine.symbols)

    @property
    def etf_symbols(self) -> frozenset[str]:
        return self._engine.etf_symbols

    def display_symbol(self, symbol: str) -> str:
        """引擎代码 -> 策略侧写法（只有 ETF 别名会被改写）。"""

        return self._engine.symbol_aliases.get(symbol, symbol)

    def trade_days(
        self,
        start: object | None = None,
        end: object | None = None,
    ) -> list[pd.Timestamp]:
        # 用带预热区间（_full_dates）的完整日历：聚宽的 get_trade_days 返回
        # 市场交易日历，不受回测起始日裁剪。若用 dates（从 active_start 起），
        # 回测首日会被策略误判成“当月第一个交易日”，从而提前调仓。
        dates = self._engine._full_dates
        if start is not None:
            dates = dates[dates >= pd.Timestamp(start)]
        if end is not None:
            dates = dates[dates <= pd.Timestamp(end)]
        elif self._engine._current_date is not None:
            # 未指定 end 时不能返回回测结束后的日历，避免未来函数。
            dates = dates[dates <= self._engine._current_date]
        return [pd.Timestamp(item) for item in dates]

    def listing_dates(
        self, symbol: str
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        return self._engine._listing_bounds_for(symbol)

    def history(
        self,
        symbols: list[str],
        fields: list[str],
        *,
        count: int | None = None,
        start: object | None = None,
        end: object | None = None,
        fq: object = "pre",
    ) -> dict[str, pd.DataFrame]:
        """读取面板切片，供 ``get_price`` / ``history`` 兼容层使用。

        ``fq=None`` 且引擎使用未复权成交时读取未复权面板；其余口径沿用
        研究面板（后复权），与事件引擎的信号口径一致。
        """

        panels = self._engine.panels
        if fq is None or str(fq).lower() in {"none", ""}:
            if self._engine._execution_price_source == "unadjusted":
                panels = self._engine._price_panels
        result: dict[str, pd.DataFrame] = {}
        for field in fields:
            frame = panels.get(field)
            if frame is None and panels is not self._engine.panels:
                # 未复权文件只有 OHLC；成交额/成交量等仍用研究面板。
                frame = self._engine.panels.get(field)
            if frame is None:
                continue
            # 先按行切片再投影列：面板是「全历史 × 全市场」，双轴同时 reindex
            # 每次都要搬运上千万个单元格；行切片后只剩几十行。
            subset = _row_slice(frame, start=start, end=end)
            if count is not None:
                subset = subset.iloc[-int(count):]
            result[field] = subset.reindex(columns=pd.Index(symbols))
        return result

    def limit_panel(
        self,
        field: str,
        symbols: list[str],
        *,
        count: int | None = None,
        start: object | None = None,
        end: object | None = None,
    ) -> pd.DataFrame:
        """按昨收与板块规则生成涨跌停价面板（未复权口径）。"""

        close = _row_slice(self._engine._mark_close, start=start, end=end)
        if count is not None:
            # 多取一行用于昨收；count=1 时不必构造整段历史面板。
            close = close.iloc[-(int(count) + 1):]
        block = close.reindex(columns=pd.Index(symbols))
        prices = block.to_numpy(dtype=float)
        previous = np.empty_like(prices)
        if len(previous):
            previous[0] = np.nan
            previous[1:] = prices[:-1]
        ratios = _limit_ratios(block.columns)
        st_mask = self._st_mask(block.index, block.columns)
        upper = field == "high_limit"
        from app.services.strategy.event import kernels

        if kernels.kernels_enabled():
            values = kernels.limit_price_values(previous, ratios, st_mask, upper)
        else:
            factor = np.where(st_mask, 0.05, ratios)
            values = np.round(
                previous * (1.0 + factor if upper else 1.0 - factor), 2
            )
        values_frame = pd.DataFrame(
            values, index=block.index, columns=block.columns
        )
        if count is not None:
            values_frame = values_frame.iloc[-int(count):]
        return values_frame

    def _st_mask(
        self, index: pd.Index, columns: pd.Index
    ) -> np.ndarray:
        """对齐 ``listed_status`` 布尔矩阵到给定日期/标的窗口。"""

        mask = np.zeros((len(index), len(columns)), dtype=bool)
        cached = self._status_matrix("listed_status", ("ST", "*ST"))
        if cached is None:
            return mask
        dates, symbols, matrix = cached
        rows = dates.get_indexer(index)
        cols = symbols.get_indexer(columns)
        rows_ok = rows >= 0
        cols_ok = cols >= 0
        if rows_ok.any() and cols_ok.any():
            mask[np.ix_(rows_ok, cols_ok)] = matrix[
                np.ix_(rows[rows_ok], cols[cols_ok])
            ]
        return mask

    def limit_prices(self, symbol: str) -> tuple[float | None, float | None]:
        """当前交易日的涨跌停价。

        优先读数据侧 ``derived/limit_prices``（含板块/ST/北交所与规则切换），
        数据集缺失或当日无记录时回退到「昨收 × 板块比例」的本地推算。
        """

        engine = self._engine
        date = engine._current_date
        if date is not None:
            high, low = engine._limit_from_panels(symbol, date)
            if high is not None or low is not None:
                return high, low
        previous = self.previous_close(symbol)
        if previous is None:
            return None, None
        status = self.listed_status(symbol)
        ratio = 0.05 if status in {"ST", "*ST"} else _limit_ratio(symbol)
        return (
            round(previous * (1.0 + ratio) + 1e-9, 2),
            round(previous * (1.0 - ratio) + 1e-9, 2),
        )


def _limit_ratio(symbol: str) -> float:
    code = str(symbol).split(".")[0]
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10
