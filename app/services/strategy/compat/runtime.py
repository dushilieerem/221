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

import numpy as np
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

#: 聚宽交易所后缀 -> 本仓库后缀。策略里写死的 ``511880.XSHG`` 这类代码必须
#: 在进入引擎前转换，否则面板取不到列、会静默返回 NaN。
_JQ_SUFFIX = {"XSHG": "SH", "XSHE": "SZ", "XBSE": "BJ"}


def normalize_security(code: Any) -> Any:
    """把聚宽代码换成引擎使用的交易所代码；其它输入原样返回。"""

    if not isinstance(code, str):
        return code
    base, separator, suffix = code.rpartition(".")
    if not separator:
        return code
    mapped = _JQ_SUFFIX.get(suffix.upper())
    return f"{base}.{mapped}" if mapped else code


class GlobalState(SimpleNamespace):
    """聚宽 ``g``：每次回测开始时清空。"""

    def reset(self) -> None:
        self.__dict__.clear()


class _Log:
    def __init__(self) -> None:
        self._logger = logging.getLogger("joinquant_compat")

    def set_level(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        """聚宽日志级别设置；兼容层统一交给 Python logging。"""

    @staticmethod
    def _render(message: object, args: tuple[Any, ...]) -> str:
        """按聚宽习惯格式化；没有 ``%s`` 占位符时把参数追加到末尾。"""

        text = str(message)
        if not args:
            return text
        try:
            return text % args
        except (TypeError, ValueError):
            return " ".join([text, *(str(arg) for arg in args)])

    def _emit(self, level: int, message: object, args: tuple[Any, ...]) -> None:
        self._logger.log(level, self._render(message, args))

    def debug(self, message: object, *args: Any) -> None:
        self._emit(logging.DEBUG, message, args)

    def info(self, message: object, *args: Any) -> None:
        self._emit(logging.INFO, message, args)

    def warn(self, message: object, *args: Any) -> None:
        self._emit(logging.WARNING, message, args)

    warning = warn

    def error(self, message: object, *args: Any) -> None:
        self._emit(logging.ERROR, message, args)


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
        # get_all_securities 每天都会被策略调用；逐标的查主数据再拼 5000 行
        # DataFrame 要做几万次调用，这里只建一次基础表，日期过滤走布尔掩码。
        self._all_securities: pd.DataFrame | None = None

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
        monthday = int(monthday)
        if monthday < 0:
            # 聚宽负数表示月末倒数第 N 个交易日（-1 = 最后一个交易日）。
            nth = abs(monthday)

            def scheduled(context) -> None:
                if _is_nth_last_trading_day(context, nth, period="month"):
                    func(context)

            scheduled.__name__ = getattr(func, "__name__", "run_monthly")
            self.context.schedule.daily(scheduled, time=time)
            return
        self.context.schedule.monthly(
            func, trading_day=max(1, monthday), time=time
        )

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
        security = normalize_security(security)
        current = self.context.portfolio.quantity(security)
        desired = current + int(amount)
        event = self.context.orders.target_quantity(security, desired)
        return self._jq_order(security, event, desired)

    def order_value(self, security: str, value: float, *args: Any, **kwargs: Any) -> Any:
        security = normalize_security(security)
        price = self.context.market.current_price(security)
        current_value = self.context.portfolio.quantity(security) * (price or 0.0)
        target_value = current_value + float(value)
        desired = self._desired_quantity(target_value, price)
        event = self.context.orders.target_value(security, target_value)
        return self._jq_order(security, event, desired)

    def order_target(self, security: str, amount: float, *args: Any, **kwargs: Any) -> Any:
        security = normalize_security(security)
        desired = int(amount)
        event = self.context.orders.target_quantity(security, desired)
        return self._jq_order(security, event, desired)

    def order_target_value(self, security: str, value: float, *args: Any, **kwargs: Any) -> Any:
        security = normalize_security(security)
        price = self.context.market.current_price(security)
        desired = self._desired_quantity(float(value), price)
        event = self.context.orders.target_value(security, float(value))
        return self._jq_order(security, event, desired)

    def order_target_percent(self, security: str, percent: float, *args: Any, **kwargs: Any) -> Any:
        security = normalize_security(security)
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
            security=self._display_symbol(security),
            order_id=event.order_id,
            amount=delta,
            filled=delta,
            price=self.context.market.current_price(security),
            status=OrderStatus.held,
        )

    def _display_symbol(self, symbol: str) -> str:
        """按策略侧写法回报代码（ETF 保留 511880.XSHG 这类别名）。"""

        market = getattr(self.context, "market", None)
        display = getattr(market, "display_symbol", None)
        return display(symbol) if callable(display) else symbol

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
        symbols = [
            normalize_security(item)
            for item in ([security] if single else list(security))
        ]
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
            if field not in {"high_limit", "low_limit", "paused"}
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
            if field == "paused":
                # 聚宽返回 1/0；状态面板缺失时按 0 处理（不误判停牌）。
                paused = self.context.market.paused_panel(
                    symbols, count=count, start=start_date, end=end
                )
                if paused is not None:
                    frames[field] = paused
                continue
            source = aliases.get(field, field)
            if source in raw:
                frames[field] = raw[source]

        if not skip_paused:
            # 聚宽 get_price 默认 skip_paused=False + fill_paused=True：停牌日
            # 保留在时间轴上，价格用前收（源数据已填），volume/money 填 0。
            # 不填的话，策略里 count>=N 的流动性/成交额窗口会把停牌日算丢。
            paused = self.context.market.paused_panel(
                symbols, count=count, start=start_date, end=end
            )
            mask = paused.reindex(columns=pd.Index(symbols)).eq(1) if paused is not None else None
            if mask is None:
                close_frame = frames.get("close")
                if close_frame is not None:
                    # 没有状态面板时，用「有收盘价但成交额缺失」近似停牌日。
                    mask = close_frame.reindex(columns=pd.Index(symbols)).notna()
            if mask is not None:
                for field in ("volume", "amount", "money", "turnover"):
                    if field in frames:
                        frame = frames[field]
                        aligned = frame.reindex(index=mask.index, columns=mask.columns)
                        frames[field] = aligned.mask(mask & aligned.isna(), 0.0)

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
        """聚宽 ``attribute_history``：默认 ``skip_paused=True``，剔除停牌日。

        停牌日会占用 count 窗口，因此先多取一段，剔除停牌后再取最后
        ``count`` 行；否则窗口会被停牌日挤占。
        """

        symbols = [security] if isinstance(security, str) else list(security)
        request = int(count) * 3 + 20 if skip_paused else int(count)
        frame = self.get_price(
            security,
            count=request,
            fields=fields,
            fq=fq,
            frequency=unit,
            panel=True,
        )
        if not skip_paused:
            return frame
        paused = self.context.market.paused_panel(
            symbols, count=request, end=self._current_date()
        )
        if paused is None:
            return frame

        def _drop_paused(value, symbol):
            if symbol not in paused.columns:
                return value.tail(count)
            mask = paused[symbol].reindex(value.index).fillna(False).astype(bool)
            return value.loc[~mask].tail(count)

        if isinstance(frame, dict):
            return {
                symbol: _drop_paused(value, symbol)
                for symbol, value in frame.items()
            }
        return _drop_paused(frame, symbols[0])

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
            [normalize_security(item) for item in security_list]
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
        frame = self._all_securities
        if frame is None:
            symbols = list(self.context.market.symbols)
            etf_symbols = set(getattr(self.context.market, "etf_symbols", ()) or ())
            rows = []
            index = []
            for symbol in symbols:
                # 聚宽 stock 类型不含 B 股（200xxx.SZ / 900xxx.SH）；主数据
                # 里带这些代码，不剔除会进入选股候选。
                if symbol.startswith(("200", "900")):
                    continue
                start, end = self.context.market.listing_dates(symbol)
                master_start = self.master.start_date(symbol) if self.master else None
                start_value = master_start or start
                if isinstance(start_value, pd.Timestamp):
                    start_value = start_value.date()
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
                    "type": "fund" if symbol in etf_symbols else "stock",
                })
                index.append(symbol)
            frame = pd.DataFrame(rows, index=pd.Index(index))
            self._all_securities = frame
        keep = pd.Series(True, index=frame.index)
        wanted = (
            {str(item) for item in types}
            if types is not None and not isinstance(types, str)
            else ({types} if types is not None else None)
        )
        if wanted and "all" not in wanted:
            keep &= frame["type"].isin(wanted)
        if date is not None:
            as_of = pd.Timestamp(date)
            starts = pd.to_datetime(frame["start_date"], errors="coerce")
            ends = pd.to_datetime(frame["end_date"], errors="coerce")
            keep &= (starts.isna() | (starts <= as_of)) & (
                ends.isna() | (ends >= as_of)
            )
        return frame[keep]

    def get_all_trade_days(self):
        return self.get_trade_days()

    def get_trade_days(self, start_date=None, end_date=None, count=None):
        days = self.context.market.trade_days(start_date, end_date)
        if count is not None:
            days = days[-int(count):]
        return [day.date() for day in days]

    def get_security_info(self, security: str):
        security = normalize_security(security)
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

    def get_history_fundamentals(
        self,
        security=None,
        fields=None,
        watch_date=None,
        count: int = 1,
        interval: str = "1q",
        stat_by_year: bool = False,
        **kwargs: Any,
    ):
        """聚宽 ``get_history_fundamentals``：按观察日取最近 N 期财务数据。"""

        if self.fundamentals is None:
            raise NotImplementedError("财务数据目录未配置")
        if stat_by_year:
            raise NotImplementedError("get_history_fundamentals(stat_by_year=True) 暂未支持")
        symbols = [security] if isinstance(security, str) else list(security or [])
        if not symbols:
            return pd.DataFrame(columns=["code", "statDate"])
        if watch_date is None:
            watch_date = self.context.previous_date or self._current_date()
        return self.fundamentals.history(
            list(fields or []), symbols, watch_date=watch_date, count=int(count)
        )

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

    def calc_factors(
        self,
        securities,
        factors,
        start_date=None,
        end_date=None,
        **kwargs: Any,
    ):
        """执行聚宽自定义 ``Factor`` 子类。

        策略通常只用单日（start=end）；兼容层按 ``end_date`` 取 PIT 依赖，
        返回 ``{factor.name: DataFrame[date × code]}``。
        """

        if self.factors is None:
            raise NotImplementedError("jqfactor 数据目录未配置")
        symbols = [securities] if isinstance(securities, str) else list(securities)
        as_of = end_date if end_date is not None else start_date
        as_of = as_of if as_of is not None else self._current_date()
        timestamp = pd.Timestamp(as_of).normalize()
        as_of_text = timestamp.strftime("%Y-%m-%d")
        result: dict[str, pd.DataFrame] = {}
        for factor in factors:
            dependencies = list(getattr(factor, "dependencies", ()) or ())
            series_map = self.factors.dependency_series_many(
                dependencies, symbols, as_of_text
            )
            data: dict[str, pd.DataFrame] = {}
            for dependency in dependencies:
                series = series_map[dependency].reindex(symbols)
                data[dependency] = pd.DataFrame(
                    [series.to_numpy()],
                    index=[timestamp],
                    columns=symbols,
                )
            values = factor.calc(data)
            if isinstance(values, pd.Series):
                frame = values.reindex(symbols).to_frame().T
                frame.index = [timestamp]
                frame.columns = symbols
            else:
                frame = pd.DataFrame(values)
                frame = frame.reindex(columns=symbols)
            result[str(getattr(factor, "name", "factor"))] = frame
        return result

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
        symbols = [
            normalize_security(item)
            for item in ([security] if isinstance(security, str) else list(security))
        ]
        as_of = date if date is not None else self._current_date()
        return self.industry.industries(symbols, as_of)

    def get_industry_stocks(self, industry_code, date=None, **kwargs: Any):
        if self.industry is None:
            raise NotImplementedError("行业数据目录未配置；请传入 IndustryStore")
        as_of = date if date is not None else self._current_date()
        return self.industry.stocks(industry_code, as_of)

    def get_extras(
        self,
        field: str,
        security_list,
        start_date=None,
        end_date=None,
        count: int | None = None,
        df: bool = True,
        **kwargs: Any,
    ):
        """聚宽 ``get_extras``；目前只实现 ``is_st``（ST / *ST）。"""
        if str(field) != "is_st":
            raise NotImplementedError(f"get_extras 暂只支持 is_st: {field}")
        market = getattr(self.context, "market", None)
        engine = getattr(market, "_engine", None)
        panel = getattr(engine, "panels", {}).get("listed_status") if engine else None
        if panel is None:
            raise NotImplementedError("当前数据版本没有 listed_status，无法取 is_st")
        symbols = [
            normalize_security(item)
            for item in (
                [security_list] if isinstance(security_list, str) else list(security_list)
            )
        ]
        frame = panel
        if start_date is not None:
            frame = frame.loc[frame.index >= pd.Timestamp(start_date)]
        if end_date is not None:
            frame = frame.loc[frame.index <= pd.Timestamp(end_date)]
        if count is not None:
            frame = frame.iloc[-int(count):]
        frame = frame.reindex(columns=symbols)
        status = frame.astype("object")
        result = status.apply(lambda column: column.map(
            lambda value: False if pd.isna(value) else str(value).strip() in {"ST", "*ST"}
        ))
        return result if df else result.to_dict()

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
            security=self._display_symbol(order.symbol),
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
            "get_industry_stocks": self.get_industry_stocks,
            "get_extras": self.get_extras,
            "get_history_fundamentals": self.get_history_fundamentals,
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
            "Factor": Factor,
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
        return _SecurityData(self._market, normalize_security(symbol), self._master)


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


class Factor:
    """聚宽 jqfactor ``Factor`` 基类的最小实现。"""

    name: str = ""
    max_window: int = 1
    dependencies: tuple[str, ...] = ()

    def calc(self, data: dict[str, pd.DataFrame]):
        raise NotImplementedError("Factor 子类必须实现 calc(data)")


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


def _is_nth_last_trading_day(
    context, nth: int, *, period: str = "month"
) -> bool:
    """``date`` 是否为本月倒数第 ``nth`` 个交易日。"""

    market = getattr(context, "market", None)
    if market is None:
        return False
    day = pd.Timestamp(context.current_dt).normalize()
    if period == "month":
        start = day.replace(day=1)
        end = start + pd.offsets.MonthEnd(1)
    else:
        start = day - pd.Timedelta(days=int(day.weekday()))
        end = start + pd.Timedelta(days=6)
    days = market.trade_days(start, end)
    return len(days) >= nth and pd.Timestamp(days[-nth]).normalize() == day


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
    """多标的 ``panel=False`` 长表：一次构造，避免逐字段 melt + merge。

    聚宽策略经常按日对全市场取长表（如涨停黑名单），原先的
    ``melt`` + ``merge`` 在 26×5000 的窗口上要 3 秒；改为列优先拉平后
    直接拼装，同一规模约 20 毫秒。
    """

    available = [field for field in fields if field in frames]
    sliced: list[tuple[str, pd.DataFrame]] = []
    index: pd.Index | None = None
    for field in available:
        frame = frames[field]
        if frame.empty:
            continue
        if index is None:
            index = frame.index
        elif not frame.index.equals(index):
            # 行索引不一致时对齐到场内共同的行序，保持与 merge 一致的外连接语义。
            index = index.union(frame.index)
        sliced.append((field, frame))
    available = [field for field, _ in sliced]
    if not sliced or index is None:
        return pd.DataFrame(columns=["time", "code", *fields])
    symbol_index = pd.Index(symbols)
    matrices: dict[str, np.ndarray] = {}
    numeric = True
    for field, frame in sliced:
        if not frame.index.equals(index):
            frame = frame.reindex(index)
        frame = frame.reindex(columns=symbol_index)
        if not all(pd.api.types.is_numeric_dtype(dtype) for dtype in frame.dtypes):
            numeric = False
            break
        matrices[field] = frame.to_numpy(dtype=np.float64, copy=False)
    if numeric:
        length = len(index)
        data: dict[str, Any] = {
            "time": np.repeat(index.to_numpy(), len(symbol_index)),
            "code": np.tile(symbol_index.to_numpy(), length),
        }
        for field, matrix in matrices.items():
            data[field] = matrix.ravel()
        merged = pd.DataFrame(data)
    else:
        pieces = []
        for field, frame in sliced:
            if not frame.index.equals(index):
                frame = frame.reindex(index)
            series = frame.reindex(columns=symbol_index).stack()
            series.name = field
            pieces.append(series)
        merged = pd.concat(pieces, axis=1).reset_index()
        merged.columns = ["time", "code", *available]
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
