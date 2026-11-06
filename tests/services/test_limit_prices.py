# -*- coding: utf-8 -*-
"""数据侧涨跌停价面板加载。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.data.limit_prices import load_limit_panels


def _write_dataset(root):
    directory = root / "derived" / "limit_prices" / "year=2024"
    directory.mkdir(parents=True)
    frame = pd.DataFrame({
        "date": ["2024-01-02", "2024-01-02", "2024-01-03"],
        "symbol": ["000001.SZ", "600000.SH", "000001.SZ"],
        "upper_limit": [11.0, 8.8, 12.0],
        "lower_limit": [9.0, 7.2, 10.0],
    })
    frame.to_parquet(directory / "part.parquet", index=False)


def test_load_limit_panels_pivots_wide(tmp_path):
    data_dir = tmp_path / "materialized" / "ifind"
    data_dir.mkdir(parents=True)
    _write_dataset(tmp_path / "materialized")

    panels = load_limit_panels(
        data_dir, ["000001.SZ"], start="2024-01-02", end="2024-01-03"
    )

    assert list(panels) == ["upper_limit", "lower_limit"]
    assert panels["upper_limit"].loc["2024-01-02", "000001.SZ"] == 11.0
    assert panels["lower_limit"].loc["2024-01-03", "000001.SZ"] == 10.0


def test_missing_dataset_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_limit_panels(tmp_path / "materialized" / "ifind", ["000001.SZ"])
