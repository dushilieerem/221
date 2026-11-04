"""ETF 日线面板加载的单元测试。"""

from __future__ import annotations

import pandas as pd

from app.services.data.etf import etf_daily_dir, load_etf_panels


def _write_etf_dataset(root):
    data_dir = root / "materialized" / "ifind"
    data_dir.mkdir(parents=True)
    dataset = root / "materialized" / "free" / "etf_daily" / "year=2024"
    dataset.mkdir(parents=True)
    frame = pd.DataFrame({
        "symbol": ["511880.SH", "511880.SH", "159001.SZ"],
        "time": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02"]),
        "open": [100.0, 100.1, 1.0],
        "high": [100.2, 100.3, 1.1],
        "low": [99.9, 100.0, 0.9],
        "close": [100.1, 100.2, 1.05],
        "volume": [1000, 2000, 3000],
        "amount": [10.0, 20.0, 30.0],
    })
    frame.to_parquet(dataset / "date=2024-01-02.parquet", index=False)
    return data_dir


def test_load_etf_panels_pivots_by_date_and_symbol(tmp_path):
    data_dir = _write_etf_dataset(tmp_path)

    assert etf_daily_dir(data_dir) == (tmp_path / "materialized" / "free" / "etf_daily").resolve()

    panels = load_etf_panels(data_dir, ["511880.SH"], start="2024-01-02", end="2024-01-03")

    close = panels["close"]
    assert list(close.columns) == ["511880.SH"]
    assert list(close.index) == list(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    assert close["511880.SH"].tolist() == [100.1, 100.2]


def test_load_etf_panels_filters_symbols(tmp_path):
    data_dir = _write_etf_dataset(tmp_path)

    panels = load_etf_panels(data_dir, ["159001.SZ", "999999.SH"])

    close = panels["close"]
    assert list(close.columns) == ["159001.SZ", "999999.SH"]
    assert close["159001.SZ"].iloc[0] == 1.05
    assert close["999999.SH"].isna().all()


def test_load_etf_panels_raises_when_dataset_missing(tmp_path):
    (tmp_path / "materialized" / "ifind").mkdir(parents=True)

    try:
        load_etf_panels(tmp_path / "materialized" / "ifind", ["511880.SH"])
    except FileNotFoundError as exc:
        assert "ETF 日线未发布" in str(exc)
    else:  # pragma: no cover - 明确失败
        raise AssertionError("缺少数据集时应抛 FileNotFoundError")
