"""基于 OHLC 区间的波动率因子确定性验证。"""

import numpy as np
import pandas as pd

from app.models.data import Panel
from factor_test_support import BuiltinFactorLoader as FactorLoader


def _panel() -> Panel:
    dates = pd.bdate_range("2024-01-02", periods=25)
    columns = ["A", "B"]
    close = pd.DataFrame(
        np.arange(10, 60, dtype=float).reshape(25, 2), index=dates, columns=columns
    )
    return Panel({
        "open": close - 0.5,
        "high": close + 2,
        "low": close - 1,
        "close": close,
    })


#: 因子库在 beebeb4 由 432 条裁剪到 331 条，本文件原有的 parkinson_volatility /
#: garman_klass_volatility / rogers_satchell_volatility 已随该次裁剪从库中移除
#: （实现只存在于 SQLite 的 factor_code 列，取回需从 c2e9784 恢复整库）。
#: 这里只保留仍在库中的 chaikin_volatility。
FACTOR_NAMES = {"chaikin_volatility"}


def test_range_volatility_factor_formulae_match_reference():
    loader = FactorLoader()
    loader.load()
    panel = _panel()

    high_low_average = (panel.high - panel.low).ewm(span=10, adjust=False).mean()
    expected_chaikin = (high_low_average / high_low_average.shift(10) - 1) * 100
    pd.testing.assert_frame_equal(loader.factors["chaikin_volatility"].compute(panel), expected_chaikin)


def test_range_volatility_factors_are_discoverable():
    loader = FactorLoader()
    loader.load()

    assert FACTOR_NAMES.issubset(loader.factors)
