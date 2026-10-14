"""迁入经典指标库的确定性公式测试。"""

import pandas as pd
import numpy as np

from app.models.data import Panel
from app.services.factor.base import library_factors
from app.assets.factors import tool
from factor_test_support import BuiltinFactorLoader as FactorLoader
from factor_test_support import builtin_factor_ids


def _panel():
    dates = pd.bdate_range("2024-01-02", periods=80)
    close = pd.DataFrame(np.arange(160, dtype=float).reshape(80, 2) + 10, index=dates, columns=["A", "B"])
    return Panel({"open": close - .5, "high": close + 2, "low": close - 1, "close": close, "volume": close * 100})


def test_classic_indicator_library_is_discoverable():
    library = FactorLoader().load()
    factors = library_factors(library)
    assert {
        "bbi",
        "macd",
        "kdj",
        "rsi",
        "atr",
        "vroc",
        "wvad",
    }.issubset(factors)


def test_factor_api_lists_classic_indicators():
    from app.api.routes import _builtin_factors

    names = {factor.name for factor in _builtin_factors()}
    ids = builtin_factor_ids()
    assert {
        ids["bbi"],
        ids["macd"],
        ids["kdj"],
        ids["atr"],
    }.issubset(names)


def test_classic_indicator_formulae_match_reference():
    loader, panel = FactorLoader(), _panel()
    library = loader.load()
    expected_bbi = sum(panel.close.rolling(n).mean() for n in (3, 6, 12, 24)) / 4
    pd.testing.assert_frame_equal(loader.factors["bbi"].compute(panel), expected_bbi)
    diff = panel.close.ewm(span=12).mean() - panel.close.ewm(span=26).mean()
    pd.testing.assert_frame_equal(
        loader.factors["macd"].compute(panel),
        diff - diff.ewm(span=9).mean(),
    )
    high, low = panel.high.rolling(9).max(), panel.low.rolling(9).min()
    rsv = (panel.close - low) / (high - low) * 100
    k = tool.sma(rsv, 3)
    d = tool.sma(k, 3)
    pd.testing.assert_frame_equal(loader.factors["kdj"].compute(panel), 3 * k - 2 * d)

    # channel_position / donchian_width / keltner_width 已随 beebeb4 的因子库
    # 裁剪（432→331）从库中移除；实现只存在于 SQLite 的 factor_code 列，
    # 取回需从 c2e9784 恢复整库。这里不再断言它们。


def test_classic_indicators_return_panel_shape():
    loader, panel = FactorLoader(), _panel()
    loader.load()
    for name in ("bbi", "macd", "kdj", "rsi", "atr", "vroc", "wvad"):
        assert loader.factors[name].compute(panel).shape == panel.close.shape
