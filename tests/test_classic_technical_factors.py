"""经典技术与量价因子的确定性验证。"""

import numpy as np
import pandas as pd

from app.models.data import Panel
from factor_test_support import BuiltinFactorLoader as FactorLoader
from factor_test_support import builtin_factor_ids


#: 因子库在 beebeb4 由 432 条裁剪到 331 条，本文件原有的
#: chaikin_money_flow / chande_momentum_oscillator 以及方向指标
#: （+DI/-DI/ADX）、累积派发线、终极振荡器、费舍尔变换、简易波动、
#: 相对活力指数、动量振荡器、波动率通道等 17 个因子已随该次裁剪从库中移除
#: （实现只存在于 SQLite 的 factor_code 列，取回需从 c2e9784 恢复整库）。
#: 这里只保留仍在库中的 6 个。
FACTOR_NAMES = {"cci", "williams_r", "obv", "aroon_up", "aroon_down", "dpo"}


def _panel() -> Panel:
    dates = pd.bdate_range("2024-01-02", periods=60)
    columns = ["A", "B"]
    close = pd.DataFrame(
        np.arange(10, 130, dtype=float).reshape(60, 2), index=dates, columns=columns
    )
    return Panel(
        {
            "open": close - 0.5,
            "high": close + 2,
            "low": close - 1,
            "close": close,
            "volume": close * 100,
        }
    )


def test_classic_technical_factor_formulae_match_reference():
    loader = FactorLoader()
    panel = _panel()
    loader.load()

    typical_price = (panel.high + panel.low + panel.close) / 3
    mean_deviation = typical_price.rolling(14).apply(
        lambda values: np.mean(np.abs(values - values.mean())), raw=True
    )
    expected_cci = (typical_price - typical_price.rolling(14).mean()) / (0.015 * mean_deviation)
    pd.testing.assert_frame_equal(loader.factors["cci"].compute(panel), expected_cci)

    expected_williams_r = -100 * (panel.high.rolling(14).max() - panel.close) / (
        panel.high.rolling(14).max() - panel.low.rolling(14).min() + 1e-10
    )
    pd.testing.assert_frame_equal(loader.factors["williams_r"].compute(panel), expected_williams_r)

    direction = panel.close.diff().gt(0).astype(float) - panel.close.diff().lt(0).astype(float)
    pd.testing.assert_frame_equal(loader.factors["obv"].compute(panel), (direction * panel.volume).cumsum())


def test_trend_and_momentum_factor_formulae_match_reference():
    loader = FactorLoader()
    panel = _panel()
    loader.load()

    expected_aroon_up = 100 * (
        25 - panel.high.rolling(26).apply(lambda values: np.argmax(values[::-1]), raw=True)
    ) / 25
    pd.testing.assert_frame_equal(loader.factors["aroon_up"].compute(panel), expected_aroon_up)

    expected_aroon_down = 100 * (
        25 - panel.low.rolling(26).apply(lambda values: np.argmin(values[::-1]), raw=True)
    ) / 25
    pd.testing.assert_frame_equal(loader.factors["aroon_down"].compute(panel), expected_aroon_down)

    expected_dpo = panel.close - panel.close.rolling(11).mean()
    pd.testing.assert_frame_equal(loader.factors["dpo"].compute(panel), expected_dpo)





def test_classic_technical_factors_are_discoverable():
    loader = FactorLoader()
    loader.load()

    assert FACTOR_NAMES.issubset(loader.factors)

    from app.api.routes import _builtin_factors

    names = {factor.name for factor in _builtin_factors()}
    ids = builtin_factor_ids()
    assert {ids[name] for name in FACTOR_NAMES}.issubset(names)
