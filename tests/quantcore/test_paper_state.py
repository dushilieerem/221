"""模拟盘策略状态编解码测试。"""

from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.services.paper_trading.state_codec import (
    StateCodecError,
    code_hash,
    decode_state,
    encode_state,
    merge_state,
    normalize_code_hash,
    state_hash,
)


def test_state_round_trip_preserves_dates_tuples_and_nan():
    state = SimpleNamespace(
        peak_close={"A": 12.5, "B": np.float64(13.5)},
        entry_dates={"A": date(2026, 10, 20)},
        last_scan_week=(2026, 43),
        entry_time=datetime(2026, 10, 20, 15, 0),
        missing=float("nan"),
        flags=[True, None, 1],
    )

    payload = encode_state(state)
    decoded = decode_state(payload)

    assert decoded.peak_close == {"A": 12.5, "B": 13.5}
    assert decoded.entry_dates["A"] == date(2026, 10, 20)
    assert decoded.last_scan_week == (2026, 43)
    assert decoded.entry_time == datetime(2026, 10, 20, 15, 0)
    assert np.isnan(decoded.missing)
    assert decoded.flags == [True, None, 1]


def test_merge_runs_after_initialize_and_overrides_defaults():
    state = SimpleNamespace(peak_close={}, entry_dates={}, last_scan_week=None)
    payload = encode_state(SimpleNamespace(
        peak_close={"A": 1.2},
        entry_dates={"A": date(2026, 10, 20)},
        last_scan_week=(2026, 43),
    ))

    merge_state(state, payload)

    assert state.peak_close == {"A": 1.2}
    assert state.entry_dates["A"] == date(2026, 10, 20)
    assert state.last_scan_week == (2026, 43)


def test_unsupported_value_is_rejected():
    with pytest.raises(StateCodecError):
        encode_state(SimpleNamespace(series=pd.Series([1, 2, 3])))


def test_state_hash_is_stable_across_key_order():
    left = {"a": 1, "b": {"x": 2, "y": 3}}
    right = {"b": {"y": 3, "x": 2}, "a": 1}
    assert state_hash(left) == state_hash(right)


def test_code_hash_normalization():
    digest = code_hash("abc")
    assert digest.startswith("sha256:")
    assert normalize_code_hash(digest.upper()) == digest
    assert normalize_code_hash("ABC") == "sha256:abc"
    assert normalize_code_hash("") is None
