"""分钟行情读取测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.services.data.intraday import IntradayDataError, MinuteStore, minute_file


def _write_day(data_dir: Path, date: str = "2025-07-01") -> Path:
    day = pd.Timestamp(date)
    columns = pd.MultiIndex.from_product(
        [["000001.SZ", "600000.SH"], ["open", "high", "low", "close", "volume", "amount"]]
    )
    index = [f"{date} 09:30:00", f"{date} 09:31:00", f"{date} 14:50:00"]
    rows = [
        [12.0, 12.1, 11.9, 12.05, 1000, 12050, 6.5, 6.6, 6.4, 6.55, 2000, 13100],
        [12.05, 12.2, 12.0, 12.1, 1100, 13310, 6.55, 6.7, 6.5, 6.6, 2100, 13860],
        [12.3, 12.4, 12.2, 12.35, 1500, 18525, 6.8, 6.9, 6.7, 6.85, 2500, 17125],
    ]
    frame = pd.DataFrame(rows, index=index, columns=columns)
    path = minute_file(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)
    return path


def test_price_at_and_closes(tmp_path: Path):
    _write_day(tmp_path)
    store = MinuteStore(tmp_path)

    assert store.price_at("2025-07-01", "09:30", "000001.SZ") == pytest.approx(12.05)
    assert store.price_at("2025-07-01", "14:50:00", "600000.SH") == pytest.approx(6.85)
    assert store.price_at("2025-07-01", "09:30", "999999.SZ") is None

    closes = store.closes("2025-07-01", "09:31", ["000001.SZ", "600000.SH"])
    assert closes["000001.SZ"] == pytest.approx(12.1)
    assert closes["600000.SH"] == pytest.approx(6.6)


def test_missing_day_raises(tmp_path: Path):
    store = MinuteStore(tmp_path)
    with pytest.raises(IntradayDataError, match="缺少分钟数据"):
        store.panel("2025-07-02")


def test_cache_reuses_panel(tmp_path: Path):
    _write_day(tmp_path)
    store = MinuteStore(tmp_path, max_days=1)
    assert store.panel("2025-07-01") is store.panel("2025-07-01")
