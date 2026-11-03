"""事件引擎状态面板（停牌 / ST）与长表构造的回归测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.strategy.compat.runtime import _long_frames
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
