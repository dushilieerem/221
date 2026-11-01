"""jqfactor 本地计算：价格类因子与未实现因子的契约。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.strategy.compat.factors import FactorStore

SYMBOL = "000001.SZ"


class _FakeDataEngine:
    provider = object()

    def __init__(self, close: pd.Series, turnover: pd.Series) -> None:
        self.close = close
        self.turnover = turnover

    def get_task_panels(self, start, end, fields=None, symbols=None):
        return {
            "close": pd.DataFrame({SYMBOL: self.close}),
            "turnover": pd.DataFrame({SYMBOL: self.turnover}),
        }


def _series(values: np.ndarray, start: str = "2023-01-02") -> pd.Series:
    index = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=index)


def test_sharpe_ratio_60_matches_manual_formula():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0005, 0.01, 200)
    close = _series(10.0 * np.cumprod(1.0 + returns))
    turnover = _series(np.full(200, 0.01))
    store = FactorStore(data_engine=_FakeDataEngine(close, turnover))

    actual = store.compute("sharpe_ratio_60", [SYMBOL], close.index[-1].strftime("%Y-%m-%d")).iloc[0]

    window = close.iloc[-61:]
    daily = window.pct_change().iloc[1:]
    # 聚宽口径用几何年化收益，而非 mean×252（见聚宽风格因子清单「已知差异」）。
    annual_return = (1.0 + daily).prod() ** (252.0 / len(daily)) - 1.0
    expected = (annual_return - 0.04) / (daily.std() * np.sqrt(252.0))
    assert actual == pytest.approx(expected)


def test_davol20_matches_manual_formula():
    turnover = np.concatenate([np.full(100, 0.01), np.full(20, 0.02)])
    close = _series(np.linspace(10.0, 12.0, len(turnover)))
    store = FactorStore(
        data_engine=_FakeDataEngine(close, _series(turnover))
    )

    actual = store.compute("DAVOL20", [SYMBOL], close.index[-1].strftime("%Y-%m-%d")).iloc[0]

    expected = turnover[-20:].mean() / turnover[-120:].mean()
    assert actual == pytest.approx(expected)


def test_unknown_factor_raises():
    store = FactorStore(data_engine=_FakeDataEngine(_series(np.arange(200.0)), _series(np.arange(200.0))))
    with pytest.raises(NotImplementedError, match="暂未实现"):
        store.compute("not_a_factor", [SYMBOL], "2024-12-31")
