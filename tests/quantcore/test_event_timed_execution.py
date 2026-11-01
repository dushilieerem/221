"""时点引擎：盘中成交、15:10 挂单跨日、1m 缺失回退。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.models.event import EventBacktestConfig
from app.services.strategy.event.engine import EventBacktestEngine
from app.services.strategy.event.strategy import normalize_time

DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
SYMBOL = "000001.SZ"


class TimedStrategy:
    def initialize(self, context):
        context.state.step = 0
        context.schedule.daily(self.at_0931, time="09:31")
        context.schedule.daily(self.at_1450, time="14:50")
        context.schedule.daily(self.at_1510, time="15:10")

    def at_0931(self, context):
        if context.state.step == 0:
            context.state.step = 1
            context.orders.target_quantity(SYMBOL, 100)

    def at_1450(self, context):
        if context.state.step == 1:
            context.state.step = 2
            context.orders.target_quantity(SYMBOL, 200)

    def at_1510(self, context):
        if context.state.step == 2:
            context.state.step = 3
            context.orders.target_quantity(SYMBOL, 300)


def _panels():
    adjusted = {
        field: pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0]}, index=DATES)
        for field in ("open", "high", "low", "close")
    }
    adjusted["close"] = pd.DataFrame({SYMBOL: [10.5, 10.5, 10.5]}, index=DATES)
    unadjusted = {
        "open": pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0]}, index=DATES),
        "close": pd.DataFrame({SYMBOL: [10.5, 10.5, 10.5]}, index=DATES),
    }
    return adjusted, unadjusted


def _config():
    return EventBacktestConfig(
        initial_capital=100_000.0,
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=0.0,
        slippage_rate=0.0,
        execution_timing="next_open",
        execution_price_source="unadjusted",
        timed_execution=True,
    )


def _intraday(date, time, symbol):
    return {"09:31": 10.1, "14:50": 10.4}.get(time)


def test_timed_fills_and_after_close_carry_to_next_open():
    adjusted, unadjusted = _panels()
    engine = EventBacktestEngine(
        adjusted, _config(), execution_panels=unadjusted, intraday_price=_intraday
    )
    result = engine.run(TimedStrategy())

    fills = [(fill.timestamp.strftime("%H:%M"), fill.quantity, fill.price) for fill in result.fills]
    assert fills == [
        ("09:31", 100, pytest.approx(10.1)),
        ("14:50", 100, pytest.approx(10.4)),
        ("09:30", 100, pytest.approx(10.0)),
    ]
    assert all(fill.timestamp.hour != 15 for fill in result.fills)
    assert result.position_snapshots[0].positions[SYMBOL] == 200
    assert result.position_snapshots[1].positions[SYMBOL] == 300
    assert result.assumptions["execution"] == "timed"
    assert result.assumptions["intraday_price"] == "1m"


def test_timed_falls_back_to_daily_open_close_without_minute_data():
    adjusted, unadjusted = _panels()
    engine = EventBacktestEngine(
        adjusted, _config(), execution_panels=unadjusted, intraday_price=None
    )
    result = engine.run(TimedStrategy())

    fills = [(fill.timestamp.strftime("%H:%M"), fill.price) for fill in result.fills]
    assert fills == [
        ("09:31", pytest.approx(10.0)),  # 回退开盘价
        ("14:50", pytest.approx(10.5)),  # 回退收盘价
        ("09:30", pytest.approx(10.0)),
    ]
    assert result.assumptions["intraday_price"] == "daily_open_close_fallback"


class OpenPriceStrategy:
    def initialize(self, context):
        context.schedule.daily(self.buy, time="09:30")

    def buy(self, context):
        if context.previous_date is None:
            return
        context.orders.target_quantity(SYMBOL, 100)


def test_0930_fill_uses_open_not_first_minute_close():
    adjusted, unadjusted = _panels()

    def intraday(date, time, symbol):
        # 首根分钟线收盘价与开盘价不同；09:30 必须按开盘价成交。
        return 10.9 if time == "09:30" else None

    engine = EventBacktestEngine(
        adjusted,
        _config(),
        execution_panels=unadjusted,
        intraday_price=intraday,
        first_previous_date="2024-01-01",
    )
    result = engine.run(OpenPriceStrategy())

    assert [(fill.timestamp.strftime("%H:%M"), fill.price) for fill in result.fills] == [
        ("09:30", pytest.approx(10.0))
    ]


def test_first_previous_date_is_exposed_when_configured():
    class Probe:
        def __init__(self):
            self.seen = []

        def initialize(self, context):
            context.schedule.daily(self.mark, time="09:31")

        def mark(self, context):
            self.seen.append(context.previous_date)

    adjusted, unadjusted = _panels()
    strategy = Probe()
    EventBacktestEngine(
        adjusted,
        _config(),
        execution_panels=unadjusted,
        first_previous_date="2024-01-01",
    ).run(strategy)

    assert [str(item) for item in strategy.seen] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
    ]


def test_active_start_keeps_warmup_history_but_starts_equity_later():
    dates = pd.to_datetime(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    )
    adjusted = {
        field: pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0, 10.0]}, index=dates)
        for field in ("open", "high", "low", "close")
    }
    unadjusted = {
        "open": pd.DataFrame({SYMBOL: [10.0] * 4}, index=dates),
        "close": pd.DataFrame({SYMBOL: [10.5] * 4}, index=dates),
    }

    class Probe:
        def __init__(self):
            self.seen = []

        def initialize(self, context):
            context.schedule.daily(self.mark, time="09:31")

        def mark(self, context):
            self.seen.append((context.current_dt.date(), context.previous_date))

    strategy = Probe()
    result = EventBacktestEngine(
        adjusted,
        _config(),
        execution_panels=unadjusted,
        active_start="2024-01-03",
    ).run(strategy)

    assert list(result.equity_curve.index) == list(dates[1:])
    assert strategy.seen[0][0] == dates[1].date()
    assert strategy.seen[0][1] == dates[0].date()


def test_normalize_time_accepts_joinquant_style():
    assert normalize_time("9:30") == "09:30"
    assert normalize_time("open") == "09:30"
    assert normalize_time("14:50:00") == "14:50"


def test_timed_execution_is_event_mode_only():
    from pydantic import ValidationError

    from app.schemas.backtest import StrategyBacktestRequest

    with pytest.raises(ValidationError, match="timed_execution"):
        StrategyBacktestRequest(
            engine_mode="vectorized",
            strategy_code="class X: pass",
            timed_execution=True,
        )
