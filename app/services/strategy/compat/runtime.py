"""聚宽 API 兼容层运行时。

把策略源码里的 ``g``、``run_daily``、``get_price``、``get_current_data``、
下单函数等聚宽全局名，绑定到 SVQuant 的事件引擎上下文。

已实现：调度、下单、当前行情、日线/指数 ``get_price`` / ``history`` /
``attribute_history``、证券列表、交易日、证券信息、财务、jqfactor 描述因子、
指数成分、行业。
未实现：分钟 ``get_price``、扩展数据、撤单与委托查询——调用时抛出明确的
``NotImplementedError``，避免静默返回错误结果。
"""

from __future__ import annotations

import datetime as _datetime
import logging
from types import SimpleNamespace
from typing import Any

import pandas as pd

from app.services.strategy.compat.factors import FactorStore
from app.services.strategy.compat.finance import FinanceNamespace
from app.services.strategy.compat.fundamentals import FundamentalStore
from app.services.strategy.compat.index import IndexMembership
from app.services.strategy.compat.industry import IndustryStore
from app.services.strategy.compat.master import SecurityMaster
from app.services.strategy.compat.query import (
    balance,
    cash_flow,
    income,
    indicator,
    query,
    valuation,
)

_SUPPORTED_FREQUENCIES = {"daily", "1d", "day"}


class GlobalState(SimpleNamespace):
    """聚宽 ``g``：每次回测开始时清空。"""

    def reset(self) -> None:
        self.__dict__.clear()


class _Log:
    def __init__(self) -> None:
        self._logger = logging.getLogger("joinquant_compat")

    def set_level(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        """聚宽日志级别设置；兼容层统一交给 Python logging。"""

    def debug(self, message: object, *args: Any) -> None:
        self._logger.debug(str(message), *args)

    def info(self, message: object, *args: Any) -> None:
        self._logger.info(str(message), *args)

    def warn(self, message: object, *args: Any) -> None:
        self._logger.warning(str(message), *args)

    warning = warn

    def error(self, message: object, *args: Any) -> None:
        self._logger.error(str(message), *args)


class JoinQuantRuntime:
    """一次聚宽策略运行的全局函数绑定。"""

    def __init__(
        self,
        fundamentals=None,
        factors=None,
        master=None,
        index=None,
        industry=None,
        index_prices=None,
        finance=None,
    ) -> None:
        self.context = None
        self.g = GlobalState()
        self.settings: dict[str, Any] = {}
        self.log = _Log()
        self.fundamentals = fundamentals if fundamentals is not None else _default_store()
        self.factors = factors if factors is not None else _default_factor_store()
        self.master = master if master is not None else _default_master()
        self.index = index
        self.industry = industry
        self.index_prices = index_prices
        self.finance = finance if finance is not None else FinanceNamespace()

    def bind(self, context) -> None:
        self.context = context
        self.g.reset()

    # ------------------------------------------------------------ 调度

    def run_daily(self, func, time: str = "09:30", **kwargs: Any) -> None:
        self.context.schedule.daily(func, time=time)

    def run_weekly(
        self, func, weekday: int = 1, time: str = "09:30", **kwargs: Any
    ) -> None:
        # 聚宽 weekday 是「本周第几个交易日」（1 = 第一个交易日），不是星期几；
        # 周一休市时顺延到周二，回测起始周也按本周第一个交易日触发。
        nth = max(1, int(weekday))

        def scheduled(context) -> None:
            if _is_nth_trading_day(context, nth, period="week"):
                func(context)

        scheduled.__name__ = getattr(func, "__name__", "run_weekly")
        self.context.schedule.daily(scheduled, time=time)

    def run_monthly(
        self, func, monthday: int = 1, time: str = "09:30", **kwargs: Any
    ) -> None:
        self.context.schedule.monthly(func, trading_day=max(1, int(monthday)), time=time)

    # ------------------------------------------------------------ 设置

    def set_option(self, key: str, value: Any) -> None:
        self.settings[f"option.{key}"] = value

    def set_benchmark(self, benchmark: str, **kwargs: Any) -> None:
        self.settings["benchmark"] = benchmark

    def set_order_cost(self, cost, type: str = "stock", **kwargs: Any) -> None:
        self.settings["order_cost"] = cost

    def set_slippage(self, slippage, type: str = "stock", **kwargs: Any) -> None:
        self.settings["slippage"] = slippage

    def set_universe(self, universe) -> None:
        self.settings["universe"] = universe

    # ------------------------------------------------------------ 下单

    def order(self, security: str, amount: float, *args: Any, **kwargs: Any) -> Any:
        current = self.context.portfolio.quantity(security)
        desired = current + int(amount)
        event = self.context.orders.target_quantity(security, desired)
        return self._jq_order(security, event, desired)

    def order_value(self, security: str, value: float, *args: Any, **kwargs: Any) -> Any:
        price = self.context.market.current_price(security)
        current_value = self.context.portfolio.quantity(security) * (price or 0.0)
        target_value = current_value + float(value)
        desired = self._desired_quantity(target_value, price)
        event = self.context.orders.target_value(security, target_value)
        return self._jq_order(security, event, desired)

    def order_target(self, security: str, amount: float, *args: Any, **kwargs: Any) -> Any:
        desired = int(amount)
        event = self.context.orders.target_quantity(security, desired)
        return self._jq_order(security, event, desired)

    def order_target_value(self, security: str, value: float, *args: Any, **kwargs: Any) -> Any:
        price = self.context.market.current_price(security)
        desired = self._desired_quantity(float(value), price)
        event = self.context.orders.target_value(security, float(value))
        return self._jq_order(security, event, desired)

    def order_target_percent(self, security: str, percent: float, *args: Any, **kwargs: Any) -> Any:
        value = self._total_value() * float(percent)
        price = self.context.market.current_price(security)
        desired = self._desired_quantity(value, price)
        event = self.context.orders.target_value(security, value)
        return self._jq_order(security, event, desired)

    @staticmethod
    def _desired_quantity(value: float, price: float | None) -> int:
        if not price or price <= 0:
            return 0
        return int(float(value) / float(price) / 100.0) * 100

    def _jq_order(self, security: str, event, desired: int) -> JQOrder:
        current = int(self.context.portfolio.quantity(security))
        delta = int(desired) - current
        return JQOrder(
            security=security,
            order_id=event.order_id,
            amount=delta,
            filled=delta,
            price=self.context.market.current_price(security),
            status=OrderStatus.held,
        )

    def _total_value(self) -> float:
        wrapped = getattr(self.context.portfolio, "total_value", None)
        if wrapped is not None:
            return float(wrapped)
        total = float(self.context.portfolio.cash)
        for symbol, quantity in self.context.portfolio.positions.items():
            price = self.context.market.current_price(symbol)
            if price:
                total += int(quantity) * float(price)
        return total

    # ------------------------------------------------------------ 行情

    def get_current_data(self) -> "_CurrentData":
        return _CurrentData(self.context.market, self.master)

    def get_price(
        self,
        security,
        start_date=None,
        end_date=None,
        frequency: str = "daily",
        fields=None,
        skip_paused: bool = False,
        fq: str = "pre",
        count: int | None = None,
        panel: bool = False,
        **kwargs: Any,
    ):
        if frequency not in _SUPPORTED_FREQUENCIES:
            raise NotImplementedError(
                f"兼容层暂不支持 get_price(frequency={frequency!r})；"
                "当前仅支持日线，分钟数据请走 timed_execution"
            )
        requested = (
            list(fields)
            if fields
            else ["open", "close", "high", "low", "volume", "amount"]
        )
        single = isinstance(security, str)
        symbols = [security] if single else list(security)
        end = end_date if end_date is not None else self._current_date()

        if (
            single
            and self.index_prices is not None
            and self.index_prices.has(symbols[0])
        ):
            frame = self.index_prices.history(
                symbols[0], requested, count=count, start=start_date, end=end
            )
            if panel:
                return frame[[field for field in requested if field in frame.columns]]
            time_name = frame.index.name or "index"
            frame = frame.reset_index().rename(columns={time_name: "time"})
            frame["code"] = symbols[0]
            ordered = ["time", "code", *[field for field in requested if field in frame.columns]]
            return frame[ordered]

        aliases = {"money": "amount"}
        source_fields = [
            aliases.get(field, field)
            for field in requested
            if field not in {"high_limit", "low_limit"}
        ]
        raw = self.context.market.history(
            symbols,
            source_fields,
            count=count,
            start=start_date,
            end=end,
            fq=fq,
        )
        frames: dict[str, pd.DataFrame] = {}
        for field in requested:
            if field in {"high_limit", "low_limit"}:
                frames[field] = self.context.market.limit_panel(
                    field, symbols, count=count, start=start_date, end=end
                )
                continue
            source = aliases.get(field, field)
            if source in raw:
                frames[field] = raw[source]

        if not panel:
            return _long_frames(
                frames, symbols, requested, skip_paused=skip_paused
            )

        if single:
            return _frame_for(frames, symbols[0], requested)
        return {symbol: _frame_for(frames, symbol, requested) for symbol in symbols}

    def attribute_history(
        self,
        security,
        count: int,
        unit: str = "1d",
        fields=None,
        skip_paused: bool = True,
        df: bool = True,
        fq: str = "pre",
        **kwargs: Any,
    ):
        return self.get_price(
            security,
            count=count,
            fields=fields,
            fq=fq,
            frequency=unit,
            panel=True,
        )

    def history(
        self,
        count: int,
        unit: str = "1d",
        field: str | list[str] = "avg",
        security_list=None,
        df: bool = True,
        skip_paused: bool = False,
        fq: str = "pre",
        **kwargs: Any,
    ):
        fields = [field] if isinstance(field, str) else list(field)
        symbols = (
            list(security_list)
            if security_list is not None
            else list(self.context.market.symbols)
        )
        if str(unit).lower() in {"1m", "minute"}:
            # 日频回测的 1m 请求：用当前时点价格构造一行，供涨跌停过滤使用。
            # 无有效价格时写 NaN，聚宽里的比较结果是 False 而不是异常。
            prices = {
                symbol: (
                    price
                    if (price := self.context.market.current_price(symbol)) is not None
                    else float("nan")
                )
                for symbol in symbols
            }
            frames = {name: pd.DataFrame([prices], index=[-1]) for name in fields}
        else:
            frames = {
                name: frame.reset_index(drop=True)
                for name, frame in self.context.market.history(
                    symbols, fields, count=count, end=self._current_date()
                ).items()
            }
        if count == 1:
            # 聚宽策略常用 series[-1]；pandas 3 对 RangeIndex 的 -1 按标签查找，
            # 这里用 -1 作为唯一索引标签，保持旧行为。
            frames = {
                name: frame.set_axis([-1] * len(frame), axis=0)
                for name, frame in frames.items()
            }
        if isinstance(field, str):
            return frames[field]
        return {name: frames[name] for name in frames}

    # ------------------------------------------------------------ 元数据

    def get_all_securities(self, types=None, date=None) -> pd.DataFrame:
        symbols = list(self.context.market.symbols)
        as_of = pd.Timestamp(date) if date is not None else None
        rows = []
        index = []
        for symbol in symbols:
            start, end = self.context.market.listing_dates(symbol)
            master_start = self.master.start_date(symbol) if self.master else None
            start_value = master_start or start
            if isinstance(start_value, pd.Timestamp):
                start_value = start_value.date()
            if as_of is not None:
                if start_value is not None and pd.Timestamp(start_value) > as_of:
                    continue
                if end is not None and pd.Timestamp(end) < as_of:
                    continue
            name = (
                self.master.name(symbol)
                if self.master is not None
                else self.context.market.name(symbol)
            )
            rows.append({
                "display_name": name,
                "name": name,
                "start_date": start_value,
                "end_date": end,
                "type": "stock",
            })
            index.append(symbol)
        return pd.DataFrame(rows, index=pd.Index(index))

    def get_all_trade_days(self):
        return self.get_trade_days()

    def get_trade_days(self, start_date=None, end_date=None, count=None):
        days = self.context.market.trade_days(start_date, end_date)
        if count is not None:
            days = days[-int(count):]
        return [day.date() for day in days]

    def get_security_info(self, security: str):
        start, end = self.context.market.listing_dates(security)
        master_start = self.master.start_date(security) if self.master else None
        name = self.master.name(security) if self.master else self.context.market.name(security)
        start_value = master_start or start
        if isinstance(start_value, pd.Timestamp):
            start_value = start_value.date()
        if start_value is None:
            # 主数据缺失时按“老股”处理，避免 new_stock 过滤直接崩溃。
            start_value = _datetime.date(1990, 1, 1)
        end_value = end.date() if isinstance(end, pd.Timestamp) else end
        if end_value is None:
            end_value = _datetime.date(2200, 1, 1)
        return SimpleNamespace(
            code=security,
            display_name=name,
            name=name,
            start_date=start_value,
            end_date=end_value,
            type="stock",
        )

    # ------------------------------------------------------------ 未实现

    def get_fundamentals(self, q, date=None, statDate=None, **kwargs: Any):
        if statDate is not None:
            raise NotImplementedError("get_fundamentals(statDate=...) 暂未支持")
        if self.fundamentals is None:
            raise NotImplementedError(
                "财务数据目录未配置；请设置 MODELSCOPE_ROOT 或传入 FundamentalStore"
            )
        if date is not None:
            as_of = date
        elif self.settings.get("option.avoid_future_data") and getattr(
            self.context, "previous_date", None
        ) is not None:
            # 聚宽 avoid_future_data：当日 09:30 不可见当日收盘估值。
            as_of = self.context.previous_date
        else:
            as_of = self._current_date()
        as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d")
        return self.fundamentals.query(q, as_of=as_of)

    def get_factor_values(
        self,
        securities,
        factors,
        end_date=None,
        count: int = 1,
        **kwargs: Any,
    ):
        if self.factors is None:
            raise NotImplementedError("jqfactor 数据目录未配置；请设置 MODELSCOPE_ROOT")
        if count != 1:
            raise NotImplementedError("get_factor_values 暂只支持 count=1")
        symbols = [securities] if isinstance(securities, str) else list(securities)
        names = [factors] if isinstance(factors, str) else list(factors)
        as_of = end_date if end_date is not None else self._current_date()
        timestamp = pd.Timestamp(as_of).normalize()
        result = {}
        for name in names:
            values = self.factors.compute(name, symbols, timestamp.strftime("%Y-%m-%d"))
            result[name] = pd.DataFrame(
                [values.reindex(symbols).to_numpy()],
                index=[timestamp],
                columns=symbols,
            )
        return result

    def calc_factors(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("calc_factors 需要 jqfactor 因子兼容层")

    def get_index_stocks(self, index_symbol, date=None, **kwargs: Any):
        if self.index is None:
            raise NotImplementedError(
                "指数成分数据目录未配置；请传入 IndexMembership"
            )
        as_of = date if date is not None else self._current_date()
        return self.index.members(index_symbol, as_of)

    def get_industry(self, security, date=None, **kwargs: Any):
        if self.industry is None:
            raise NotImplementedError("行业数据目录未配置；请传入 IndustryStore")
        symbols = [security] if isinstance(security, str) else list(security)
        as_of = date if date is not None else self._current_date()
        return self.industry.industries(symbols, as_of)

    def get_extras(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("get_extras 暂未实现")

    def _engine_orders(self) -> list[Any]:
        engine = getattr(getattr(self.context, "market", None), "_engine", None)
        return list(getattr(engine, "_all_orders", ())) if engine else []

    def _order_amount(self, order: Any) -> int:
        """聚宽订单数量：正为买、负为卖；目标价委托按当前价折算。"""

        if order.quantity is not None:
            current = int(self.context.portfolio.quantity(order.symbol))
            return int(order.quantity) - current
        price = self.context.market.current_price(order.symbol)
        if not price or price <= 0:
            return 0
        current_value = int(self.context.portfolio.quantity(order.symbol)) * float(price)
        value = float(order.target_value) - current_value
        return int(value / float(price) / 100.0) * 100

    def _jq_order_from_event(self, order: Any, *, pending: bool) -> "JQOrder":
        engine = getattr(getattr(self.context, "market", None), "_engine", None)
        filled = int(getattr(engine, "_fill_quantities", {}).get(order.order_id, 0))
        if pending:
            amount = self._order_amount(order)
            status = OrderStatus.open
            filled = 0
        elif order.status == "rejected":
            amount = self._order_amount(order)
            status = OrderStatus.rejected
            filled = 0
        else:
            amount = filled if order.side == "buy" else -filled
            status = OrderStatus.held
        price = self.context.market.current_price(order.symbol)
        return JQOrder(
            security=order.symbol,
            order_id=order.order_id,
            amount=amount,
            filled=filled,
            price=price,
            status=status,
        )

    def get_orders(self, *args: Any, **kwargs: Any) -> dict[str, "JQOrder"]:
        """当日已提交订单（含挂单）；按 order_id 索引。"""

        pending = list(getattr(self.context.orders, "pending", ()))
        pending_ids = {order.order_id for order in pending}
        today = pd.Timestamp(self.context.current_dt).normalize()
        # 订单按提交顺序追加；从尾部回看，遇到更早日期即可停止。
        today_orders: list[Any] = []
        for order in reversed(self._engine_orders()):
            submitted = getattr(order, "submitted_at", None)
            if submitted is None:
                continue
            if pd.Timestamp(submitted).normalize() != today:
                break
            today_orders.append(order)
        result: dict[str, JQOrder] = {}
        for order in list(reversed(today_orders)) + pending:
            if order.order_id in result:
                continue
            result[order.order_id] = self._jq_order_from_event(
                order, pending=order.order_id in pending_ids
            )
        return result

    def get_open_orders(self, *args: Any, **kwargs: Any) -> dict[str, "JQOrder"]:
        """未完成订单（尚未撮合或跨日挂单）。"""

        pending = list(getattr(self.context.orders, "pending", ()))
        return {
            order.order_id: self._jq_order_from_event(order, pending=True)
            for order in pending
        }

    def cancel_order(self, order: Any, **kwargs: Any) -> "JQOrder | None":
        order_id = getattr(order, "order_id", None)
        if not order_id:
            return None
        if not self.context.orders.cancel(str(order_id)):
            return None
        return JQOrder(
            security=getattr(order, "security", getattr(order, "symbol", "")),
            order_id=str(order_id),
            amount=int(getattr(order, "amount", 0) or 0),
            filled=0,
            price=getattr(order, "price", None),
            status=OrderStatus.canceled,
        )

    def inout_cash(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("兼容层不支持资金划转")

    def get_trades(self, *args: Any, **kwargs: Any):
        """聚宽成交查询；当前仅用于日志打印，返回空映射。"""

        return {}

    # ------------------------------------------------------------ 导出

    def globals(self) -> dict[str, Any]:
        return {
            "g": self.g,
            "log": self.log,
            "datetime": _datetime,
            "date": _datetime.date,
            "time": _datetime.time,
            "timedelta": _datetime.timedelta,
            "run_daily": self.run_daily,
            "run_weekly": self.run_weekly,
            "run_monthly": self.run_monthly,
            "set_option": self.set_option,
            "set_benchmark": self.set_benchmark,
            "set_order_cost": self.set_order_cost,
            "set_slippage": self.set_slippage,
            "OrderCost": OrderCost,
            "MarketOrderStyle": MarketOrderStyle,
            "LimitOrderStyle": LimitOrderStyle,
            "OrderStatus": OrderStatus,
            "FixedSlippage": FixedSlippage,
            "PriceRelatedSlippage": PriceRelatedSlippage,
            "set_universe": self.set_universe,
            "order": self.order,
            "order_value": self.order_value,
            "order_target": self.order_target,
            "order_target_value": self.order_target_value,
            "order_target_percent": self.order_target_percent,
            "get_current_data": self.get_current_data,
            "get_price": self.get_price,
            "attribute_history": self.attribute_history,
            "history": self.history,
            "get_all_securities": self.get_all_securities,
            "get_trade_days": self.get_trade_days,
            "get_all_trade_days": self.get_all_trade_days,
            "get_security_info": self.get_security_info,
            "get_fundamentals": self.get_fundamentals,
            "get_factor_values": self.get_factor_values,
            "calc_factors": self.calc_factors,
            "get_index_stocks": self.get_index_stocks,
            "get_industry": self.get_industry,
            "get_extras": self.get_extras,
            "cancel_order": self.cancel_order,
            "get_orders": self.get_orders,
            "get_open_orders": self.get_open_orders,
            "get_trades": self.get_trades,
            "inout_cash": self.inout_cash,
            "finance": self.finance,
            "query": query,
            "valuation": valuation,
            "indicator": indicator,
            "balance": balance,
            "income": income,
            "cash_flow": cash_flow,
            "Factor": _Unsupported("Factor"),
        }

    def _current_date(self):
        engine = getattr(self.context.market, "_engine", None)
        date = getattr(engine, "_current_date", None)
        if date is not None:
            return date
        timestamp = getattr(self.context, "current_dt", None)
        return pd.Timestamp(timestamp).normalize() if timestamp is not None else None


def _default_master():
    try:
        master = SecurityMaster()
        return master if master._names else None
    except Exception:  # noqa: BLE001
        return None


def _default_factor_store():
    try:
        store = FactorStore()
        return store if store.fin.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


def _default_store():
    try:
        store = FundamentalStore()
        return store if store.fin.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


class _CurrentData:
    def __init__(self, market, master=None) -> None:
        self._market = market
        self._master = master

    def __getitem__(self, symbol: str) -> "_SecurityData":
        return _SecurityData(self._market, symbol, self._master)


class _SecurityData:
    def __init__(self, market, symbol: str, master=None) -> None:
        self._market = market
        self._symbol = symbol
        self._master = master

    @property
    def last_price(self) -> float:
        value = self._market.current_price(self._symbol)
        return float(value) if value is not None else float("nan")

    @property
    def day_open(self) -> float:
        value = self._market.day_open(self._symbol)
        return float(value) if value is not None else float("nan")

    @property
    def high_limit(self) -> float:
        high, _ = self._market.limit_prices(self._symbol)
        return float(high) if high is not None else float("nan")

    @property
    def low_limit(self) -> float:
        _, low = self._market.limit_prices(self._symbol)
        return float(low) if low is not None else float("nan")

    @property
    def paused(self) -> bool:
        status = self._market.status_text(self._symbol) or ""
        return "停牌" in status

    @property
    def is_st(self) -> bool:
        return (self._market.listed_status(self._symbol) or "") in {"ST", "*ST"}

    @property
    def name(self) -> str:
        if self._master is not None:
            return self._master.name(self._symbol)
        return self._market.name(self._symbol)


class OrderStatus:
    """聚宽 OrderStatus 常量占位。"""

    new = "new"
    open = "open"
    filled = "filled"
    canceled = "canceled"
    rejected = "rejected"
    held = "held"


class JQOrder:
    """聚宽 Order 对象的最小实现。

    SVQuant 引擎在回调结束后才撮合，这里按当前价预估目标股数并假定成交，
    让策略里的 ``order.filled`` / ``order.amount`` / ``order.status`` 可用。
    """

    def __init__(
        self,
        *,
        security: str,
        order_id: str,
        amount: int,
        filled: int,
        price: float | None,
        status: str,
    ) -> None:
        self.security = security
        self.order_id = order_id
        self.amount = int(amount)
        self.filled = int(filled)
        self.price = float(price) if price is not None else float("nan")
        self.avg_cost = self.price
        self.status = status
        self.side = "buy" if amount > 0 else ("sell" if amount < 0 else None)
        self.is_buy = amount > 0


class MarketOrderStyle:
    """聚宽市价单占位；兼容层按当前时点价格撮合。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.params = dict(kwargs)


class LimitOrderStyle:
    """聚宽限价单占位；保留限价供策略读取，成交仍按引擎时点价。"""

    def __init__(self, limit_price: float | None = None, *args: Any, **kwargs: Any) -> None:
        self.limit_price = limit_price


class OrderCost:
    """聚宽 OrderCost 占位；实际费率由回测请求提供。"""

    def __init__(self, **kwargs: Any) -> None:
        self.params = dict(kwargs)


class FixedSlippage:
    """聚宽固定滑点占位；实际滑点由回测请求提供。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = float(value)


class PriceRelatedSlippage(FixedSlippage):
    pass


class _Unsupported:
    """未实现 API 的占位符；属性访问返回自身，便于 ``valuation.pe_ratio``。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str) -> "_Unsupported":
        return self

    def __getitem__(self, item: object) -> "_Unsupported":
        return self

    def __call__(self, *args: Any, **kwargs: Any):
        raise NotImplementedError(f"{self._name} 在聚宽兼容层中暂未实现")


def _is_nth_trading_day(context, nth: int, *, period: str = "week") -> bool:
    """``date`` 是否为本周/本月第 ``nth`` 个交易日（聚宽 run_* 语义）。"""

    market = getattr(context, "market", None)
    if market is None:
        return False
    day = pd.Timestamp(context.current_dt).normalize()
    if period == "week":
        start = day - pd.Timedelta(days=int(day.weekday()))
        end = start + pd.Timedelta(days=6)
    else:
        start = day.replace(day=1)
        end = start + pd.offsets.MonthEnd(1)
    days = market.trade_days(start, end)
    return len(days) >= nth and pd.Timestamp(days[nth - 1]).normalize() == day


def _long_frames(
    frames: dict[str, pd.DataFrame],
    symbols: list[str],
    fields: list[str],
    *,
    skip_paused: bool = False,
) -> pd.DataFrame:
    """多标的 ``panel=False`` 长表：一次 melt/merge，避免逐标的构造。"""

    available = [field for field in fields if field in frames]
    if not available:
        return pd.DataFrame(columns=["time", "code", *fields])
    symbol_index = pd.Index(symbols)
    merged: pd.DataFrame | None = None
    for field in available:
        frame = frames[field].reindex(columns=symbol_index)
        if frame.empty:
            continue
        long = (
            frame.rename_axis("time")
            .reset_index()
            .melt(id_vars="time", var_name="code", value_name=field)
        )
        if merged is None:
            merged = long
        else:
            merged = merged.merge(
                long, on=["time", "code"], how="outer", sort=False
            )
    if merged is None:
        return pd.DataFrame(columns=["time", "code", *fields])
    if skip_paused:
        merged = merged.dropna(subset=available, how="all")
    return merged[["time", "code", *available]].reset_index(drop=True)


def _long_frame(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    fields: list[str],
    *,
    skip_paused: bool = False,
) -> pd.DataFrame:
    """聚宽 ``panel=False`` 的长表：time / code / 字段。"""

    columns = {}
    for field in fields:
        frame = frames.get(field)
        if frame is None or symbol not in frame.columns:
            continue
        columns[field] = frame[symbol]
    if not columns:
        return pd.DataFrame(columns=["time", "code", *fields])
    frame = pd.DataFrame(columns)
    if skip_paused:
        frame = frame.dropna(how="all")
    time_name = frame.index.name or "index"
    frame = frame.reset_index().rename(columns={time_name: "time"})
    frame["code"] = symbol
    ordered = ["time", "code", *[field for field in fields if field in frame.columns]]
    return frame[ordered]


def _frame_for(frames: dict[str, pd.DataFrame], symbol: str, fields: list[str]) -> pd.DataFrame:
    columns = {}
    for field in fields:
        frame = frames.get(field)
        if frame is None or symbol not in frame.columns:
            continue
        columns[field] = frame[symbol]
    return pd.DataFrame(columns)
