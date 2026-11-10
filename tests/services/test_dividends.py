# -*- coding: utf-8 -*-
"""分红送转事件面板加载。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.data.dividends import load_dividend_panels


def _write_dataset(root):
    directory = root / "free" / "dividend_actions"
    directory.mkdir(parents=True)
    frame = pd.DataFrame({
        "time": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-06-01"]),
        "type": [1, 1, 1],
        "interest": [5.0, 0.0, 10.0],
        "allotPrice": [0.0, 0.0, 0.0],
        "stockBonus": [1.0, 0.0, 0.0],
        "allotment": [0.0, 0.0, 0.0],
        "Symbol": ["000001.SZ", "000001.SZ", "000001.SZ"],
    })
    frame.to_parquet(directory / "000001.SZ.parquet", index=False)


def test_load_dividend_panels_converts_per_ten_shares(tmp_path):
    data_dir = tmp_path / "materialized" / "ifind"
    data_dir.mkdir(parents=True)
    _write_dataset(tmp_path / "materialized")

    panels = load_dividend_panels(
        data_dir, ["000001.SZ"], start="2024-01-01", end="2024-01-31"
    )

    assert panels["cash_dividend"].loc["2024-01-02", "000001.SZ"] == pytest.approx(0.5)
    assert panels["stock_bonus"].loc["2024-01-02", "000001.SZ"] == pytest.approx(0.1)
    assert panels["cash_dividend"].loc["2024-01-03", "000001.SZ"] == 0.0
    assert "2024-06-01" not in panels["cash_dividend"].index


def test_missing_dataset_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_dividend_panels(tmp_path / "materialized" / "ifind", ["000001.SZ"])


def test_load_dividend_panels_accepts_mixed_date_strings(tmp_path):
    """新回填的分红可能混用日期与日期时间字符串，不能被首值格式卡住。"""

    data_dir = tmp_path / "materialized" / "ifind"
    data_dir.mkdir(parents=True)
    directory = tmp_path / "materialized" / "free" / "dividend_actions"
    directory.mkdir(parents=True)
    pd.DataFrame({
        "time": ["2024-01-02 00:00:00", "2024-01-03", "2024-06-01"],
        "type": [1, 1, 1],
        "interest": [5.0, 0.0, 10.0],
        "allotPrice": [0.0, 0.0, 0.0],
        "stockBonus": [1.0, 0.0, 0.0],
        "allotment": [0.0, 0.0, 0.0],
        "Symbol": ["000001.SZ", "000001.SZ", "000001.SZ"],
    }).to_parquet(directory / "000001.SZ.parquet", index=False)

    panels = load_dividend_panels(
        data_dir, ["000001.SZ"], start="2024-01-01", end="2024-01-31"
    )

    assert panels["cash_dividend"].loc["2024-01-02", "000001.SZ"] == pytest.approx(0.5)
    assert panels["cash_dividend"].loc["2024-01-03", "000001.SZ"] == 0.0
