"""事件回测编译内核的数值一致性。"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.strategy.event import kernels


def test_limit_price_values_match_numpy():
    rng = np.random.default_rng(0)
    previous = rng.uniform(5.0, 50.0, (6, 4))
    previous[0] = np.nan
    ratios = np.array([0.1, 0.2, 0.3, 0.1])
    st_mask = np.zeros((6, 4), dtype=bool)
    st_mask[3, 1] = True

    for upper in (True, False):
        actual = kernels.limit_price_values(previous, ratios, st_mask, upper)
        factor = np.where(st_mask, 0.05, ratios)
        expected = np.round(
            previous * (1.0 + factor if upper else 1.0 - factor), 2
        )
        np.testing.assert_allclose(actual, expected, equal_nan=True)


@pytest.mark.parametrize("value", ["0", "1"])
def test_kernels_flag_reads_environment(monkeypatch, value):
    monkeypatch.setenv("RE_NUMBA_KERNELS", value)
    assert kernels.kernels_enabled() is (value == "1" and kernels._HAS_NUMBA)
