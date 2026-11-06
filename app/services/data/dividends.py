"""分红送转事件面板加载。

数据侧 ``free/dividend_actions`` 提供每 10 股派息 ``interest``、送股
``stockBonus``（另含配股字段，本模块不使用）。事件引擎的现金分红模型按
该数据在事件日给账户记现金、增加送股；不把分红自动再投资。
"""

from __future__ import annotations

import glob
from pathlib import Path

import duckdb
import pandas as pd

#: 事件引擎使用的两个宽表字段。
DIVIDEND_FIELDS = ("cash_dividend", "stock_bonus")


def dividend_actions_dir(data_dir: str | Path) -> Path:
    """按行情目录推导分红事件位置：``<materialized>/free/dividend_actions``。"""

    return Path(data_dir).resolve().parent / "free" / "dividend_actions"


def load_dividend_panels(
    data_dir: str | Path,
    symbols: list[str],
    *,
    start: object | None = None,
    end: object | None = None,
) -> dict[str, pd.DataFrame]:
    """读取指定标的的分红/送转，返回 ``{字段: 日期 × 代码 宽表}``。

    - ``cash_dividend``：每股现金分红（元），由 ``interest / 10`` 换算；
    - ``stock_bonus``：每股送股比例，由 ``stockBonus / 10`` 换算。

    无事件的位置为 0；数据集缺失时抛 ``FileNotFoundError``，由调用方决定
    是否回退到再投资口径。
    """

    root = dividend_actions_dir(data_dir)
    if not any(Path(root).glob("*.parquet")):
        raise FileNotFoundError(f"分红事件未发布: {root}")
    codes = sorted({str(symbol) for symbol in symbols})
    if not codes:
        return {}
    sql = (
        f"SELECT Symbol, time, interest, stockBonus FROM read_parquet("
        f"'{root}/*.parquet', union_by_name=true)"
    )
    connection = duckdb.connect()
    try:
        frame = connection.execute(sql).fetchdf()
    finally:
        connection.close()
    if frame.empty:
        empty_index = pd.DatetimeIndex([])
        return {
            field: pd.DataFrame(index=empty_index, columns=codes, dtype=float)
            for field in DIVIDEND_FIELDS
        }
    frame = frame[frame["Symbol"].isin(codes)].copy()
    frame["time"] = pd.to_datetime(frame["time"])
    if start is not None:
        frame = frame[frame["time"] >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame["time"] <= pd.Timestamp(end)]
    if frame.empty:
        empty_index = pd.DatetimeIndex([])
        return {
            field: pd.DataFrame(index=empty_index, columns=codes, dtype=float)
            for field in DIVIDEND_FIELDS
        }
    frame["cash_dividend"] = (
        pd.to_numeric(frame["interest"], errors="coerce").fillna(0.0) / 10.0
    )
    frame["stock_bonus"] = (
        pd.to_numeric(frame["stockBonus"], errors="coerce").fillna(0.0) / 10.0
    )
    result: dict[str, pd.DataFrame] = {}
    for field in DIVIDEND_FIELDS:
        panel = frame.pivot_table(
            index="time", columns="Symbol", values=field, aggfunc="sum"
        )
        result[field] = (
            panel.reindex(columns=codes).fillna(0.0).sort_index().astype(float)
        )
    return result


__all__ = ["DIVIDEND_FIELDS", "dividend_actions_dir", "load_dividend_panels"]
