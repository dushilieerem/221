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

    def set_benchmark(self, benchmark: str) -> None:
        self.settings["benchmark"] = benchmark

    def set_order_cost(self, cost, type: str = "stock") -> None:
        self.settings["order_cost"] = cost

    def set_slippage(self, slippage) -> None:
        self.settings["slippage"] = slippage

    def set_universe(self, universe) -> None:
        self.settings["universe"] = universe

    # ------------------------------------------------------------ 下单

    def order(self, security: str, amount: float, **kwargs: Any) -> Any:
        current = self.context.portfolio.quantity(security)
        desired = current + int(amount)
        event = self.context.orders.target_quantity(security, desired)
        return self._jq_order(security, event, desired)

    def order_value(self, security: str, value: float, **kwargs: Any) -> Any:
        price = self.context.market.current_price(security)
        current_value = self.context.portfolio.quantity(security) * (price or 0.0)
        target_value = current_value + float(value)
        desired = self._desired_quantity(target_value, price)
        event = self.context.orders.target_value(security, target_value)
        return self._jq_order(security, event, desired)

    def order_target(self, security: str, amount: float, **kwargs: Any) -> Any:
        desired = int(amount)
        event = self.context.orders.target_quantity(security, desired)
        return self._jq_order(security, event, desired)

    def order_target_value(self, security: str, value: float, **kwargs: Any) -> Any:
        price = self.context.market.current_price(security)
        desired = self._desired_quantity(float(value), price)
        event = self.context.orders.target_value(security, float(value))
        return self._jq_order(security, event, desired)

    def order_target_percent(self, security: str, percent: float, **kwargs: Any) -> Any:
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
        fields = list(fields) if fields else ["open", "close", "high", "low", "volume", "amount"]
        single = isinstance(security, str)
        symbols = [security] if single else list(security)
        end = end_date if end_date is not None else self._current_date()
        if (
            single
            and self.index_prices is not None
            and self.index_prices.has(symbols[0])
        ):
            return self.index_prices.history(
                symbols[0], fields, count=count, start=start_date, end=end
            )
        frames = self.context.market.history(
            symbols, fields, count=count, start=start_date, end=end
        )
        if single:
            return _frame_for(frames, symbols[0], fields)
        return {
            symbol: _frame_for(frames, symbol, fields) for symbol in symbols
        }

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
            security, count=count, fields=fields, fq=fq, frequency=unit
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
            prices = {
                symbol: self.context.market.current_price(symbol) for symbol in symbols
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
        rows = []
        for symbol in self.context.market.symbols:
            start, end = self.context.market.listing_dates(symbol)
            rows.append({
                "display_name": self.context.market.name(symbol),
                "name": self.context.market.name(symbol),
                "start_date": start,
                "end_date": end,
                "type": "stock",
            })
        return pd.DataFrame(rows, index=pd.Index(self.context.market.symbols))

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
        as_of = date if date is not None else self._current_date()
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

    def cancel_order(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("兼容层不支持撤单")

    def get_open_orders(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("兼容层不支持查询未完成订单")

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
            "get_open_orders": self.get_open_orders,
            "get_trades": self.get_trades,
            "inout_cash": self.inout_cash,
            "finance": SimpleNamespace(
                run_query=self.get_fundamentals,
                STK_CAPITAL_CHANGE=None,
            ),
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


def _frame_for(frames: dict[str, pd.DataFrame], symbol: str, fields: list[str]) -> pd.DataFrame:
    columns = {}
    for field in fields:
        frame = frames.get(field)
        if frame is None or symbol not in frame.columns:
            continue
        columns[field] = frame[symbol]
    return pd.DataFrame(columns)
