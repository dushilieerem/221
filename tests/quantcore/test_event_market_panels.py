"""事件引擎状态面板（停牌 / ST）与长表构造的回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.models.event import EventBacktestConfig
from app.services.strategy.compat.portfolio import _PositionsView
from app.services.strategy.compat.runtime import JoinQuantRuntime, _long_frames
from app.services.strategy.event.engine import EventBacktestEngine, _MarketView


def _panels(periods: int = 6) -> tuple[list[str], pd.DatetimeIndex, dict]:
    dates = pd.bdate_range("2024-01-02", periods=periods)
    symbols = ["000001.SZ", "300001.SZ", "600001.SH"]
    close = pd.DataFrame(
        {symbol: [10.0 + index for index in range(periods)] for symbol in symbols},
        index=dates,
    )
    panels = {field: close.copy() for field in ("open", "high", "low", "close")}
    status = pd.DataFrame("正常交易", index=dates, columns=symbols, dtype="string")
    status.iloc[2, 0] = "停牌"
    status.iloc[4, 1] = "ST"
    panels["up_and_down_status"] = status
    panels["listed_status"] = status
    return symbols, dates, panels


def test_trade_days_include_warmup_before_active_start():
    """聚宽 get_trade_days 看完整市场日历，不能被回测起始日裁剪。"""

    _, dates, panels = _panels()
    engine = EventBacktestEngine(panels, active_start=dates[3])
    view = _MarketView(engine)

    days = view.trade_days(dates[0], dates[-1])

    assert list(engine.dates) == list(dates[3:])
    assert list(days) == list(dates)
    # 回测从月中开始时，月初到起始日之间的日历也要返回，首日才不会被
    # 策略误判成“当月第一个交易日”。
    month_to_start = view.trade_days(dates[0].replace(day=1), dates[3])
    assert list(month_to_start) == list(dates[:4])
    assert month_to_start[0] != dates[3]


def test_paused_panel_respects_count_window():
    symbols, dates, panels = _panels()
    view = _MarketView(EventBacktestEngine(panels))

    block = view.paused_panel(symbols, count=2)

    assert list(block.index) == list(dates[-2:])
    assert list(block.columns) == symbols
    assert block.to_numpy().sum() == 0
    # 停牌发生在 dates[2]，回看到整段时能看到。
    full = view.paused_panel(symbols, count=len(dates))
    assert int(full.loc[dates[2], "000001.SZ"]) == 1
    assert int(full.loc[dates[2], "300001.SZ"]) == 0


def test_limit_panel_uses_st_ratio_with_count_window():
    symbols, dates, panels = _panels()
    view = _MarketView(EventBacktestEngine(panels))

    block = view.limit_panel("high_limit", symbols, count=2)

    assert list(block.index) == list(dates[-2:])
    # 主板：昨收 × 1.1；创业板（300 开头）：昨收 × 1.2；ST：昨收 × 1.05。
    assert block.loc[dates[4], "000001.SZ"] == 14.3
    assert block.loc[dates[5], "000001.SZ"] == 15.4
    assert block.loc[dates[4], "300001.SZ"] == round(13.0 * 1.05, 2)
    assert block.loc[dates[5], "300001.SZ"] == round(14.0 * 1.2, 2)
    low = view.limit_panel("low_limit", symbols, count=1)
    assert low.loc[dates[-1], "000001.SZ"] == round(14.0 * 0.9, 2)


def test_long_frames_builds_time_major_long_table():
    dates = pd.bdate_range("2024-01-02", periods=3)
    symbols = ["000001.SZ", "600001.SH"]
    frames = {
        "close": pd.DataFrame([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]], index=dates, columns=symbols),
        "paused": pd.DataFrame(0.0, index=dates, columns=symbols),
    }

    long = _long_frames(frames, symbols, ["close", "paused"])

    assert list(long.columns) == ["time", "code", "close", "paused"]
    assert len(long) == 6
    assert list(long["code"]) == symbols * 3
    assert list(long["time"]) == [dates[0]] * 2 + [dates[1]] * 2 + [dates[2]] * 2
    assert np.allclose(long["close"], [1, 2, 3, 4, 5, 6])


def test_long_frames_aligns_mismatched_indexes():
    dates = pd.bdate_range("2024-01-02", periods=3)
    symbols = ["000001.SZ"]
    close = pd.DataFrame({"000001.SZ": [1.0, 2.0, 3.0]}, index=dates)
    paused = pd.DataFrame({"000001.SZ": [0.0, 1.0]}, index=dates[:2])

    long = _long_frames(
        {"close": close, "paused": paused}, symbols, ["close", "paused"]
    )

    assert len(long) == 3
    assert pd.isna(long.loc[long["time"] == dates[2], "paused"]).all()


def test_joinquant_suffix_resolves_to_panel_symbols():
    symbols, dates, panels = _panels()
    engine = EventBacktestEngine(panels)
    engine._current_date = dates[-1]
    runtime = JoinQuantRuntime()
    runtime.context = SimpleNamespace(market=_MarketView(engine))

    # 策略里写死的 ``000001.XSHE`` 必须取到 ``000001.SZ`` 的行情，
    # 而不是静默返回 NaN。
    frame = runtime.get_price("000001.XSHE", count=1, fields=["close"], panel=False)
    assert frame["close"].iloc[-1] == 15.0

    missing = runtime.get_price("600001.XSHG", count=1, fields=["close"], panel=False)
    assert missing["close"].iloc[-1] == 15.0


def test_positions_view_accepts_joinquant_suffix():
    class _View:
        positions = {"511880.SH": 300}

        def average_cost(self, symbol):
            return 100.0

    class _Market:
        @staticmethod
        def current_price(symbol):
            return 100.0

    view = _PositionsView(_View(), _Market())
    assert "511880.XSHG" in view
    assert view["511880.XSHG"].total_amount == 300
    assert view["600000.XSHG"].total_amount == 0


def test_etf_alias_is_reported_in_positions():
    class _View:
        positions = {"511880.SH": 300}

        def average_cost(self, symbol):
            return 100.0

    class _Market:
        aliases = {"511880.SH": "511880.XSHG"}

        @staticmethod
        def current_price(symbol):
            return 100.0

        def display_symbol(self, symbol):
            return self.aliases.get(symbol, symbol)

    view = _PositionsView(_View(), _Market())

    assert "511880.XSHG" in view
    assert "511880.SH" in view
    assert view["511880.XSHG"].security == "511880.XSHG"
    assert sorted(view) == ["511880.XSHG"]


def test_etf_sell_is_exempt_from_stamp_tax():
    dates = pd.bdate_range("2024-01-02", periods=4)
    close = pd.DataFrame(
        {"000001.SZ": [10.0] * 4, "511880.SH": [100.0] * 4}, index=dates
    )
    panels = {field: close.copy() for field in ("open", "high", "low", "close")}

    class RoundTrip:
        def initialize(self, context):
            context.schedule.daily(self.trade, time="09:31")

        def trade(self, context):
            step = getattr(context.state, "step", 0)
            if step == 0:
                context.orders.target_quantity("000001.SZ", 200)
                context.orders.target_quantity("511880.SH", 200)
                context.state.step = 1
            elif step == 1:
                context.orders.target_quantity("000001.SZ", 0)
                context.orders.target_quantity("511880.SH", 0)
                context.state.step = 2

    engine = EventBacktestEngine(
        panels,
        EventBacktestConfig(
            initial_capital=100_000.0,
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.001,
            slippage_rate=0.0,
            execution_timing="next_open",
        ),
        etf_symbols={"511880.SH"},
    )
    result = engine.run(RoundTrip())

    sells = {fill.symbol: fill for fill in result.fills if fill.side == "sell"}
    assert sells["000001.SZ"].stamp_tax > 0
    assert sells["511880.SH"].stamp_tax == 0.0


def test_limit_from_panels_scales_adjusted_price_to_unadjusted_limit():
    """复权撮合时，未复权涨跌停价要按当日复权/未复权收盘比例换算。"""

    symbols, dates, _ = _panels()
    adjusted = pd.DataFrame(
        {symbol: [20.0] * len(dates) for symbol in symbols}, index=dates
    )
    panels = {field: adjusted.copy() for field in ("open", "high", "low", "close")}
    panels["upper_limit"] = pd.DataFrame(
        {symbol: [11.0] * len(dates) for symbol in symbols}, index=dates
    )
    panels["lower_limit"] = pd.DataFrame(
        {symbol: [9.0] * len(dates) for symbol in symbols}, index=dates
    )
    reference = pd.DataFrame(
        {symbol: [10.0] * len(dates) for symbol in symbols}, index=dates
    )
    config = EventBacktestConfig(execution_price_source="adjusted")

    scaled = EventBacktestEngine(
        panels, config, limit_reference_close=reference
    )
    # 20 / 10 = 2：未复权 11 / 9 换算成复权口径 22 / 18。
    assert scaled._limit_from_panels("000001.SZ", dates[0]) == (22.0, 18.0)

    raw = EventBacktestEngine(panels, config)
    assert raw._limit_from_panels("000001.SZ", dates[0]) == (11.0, 9.0)
