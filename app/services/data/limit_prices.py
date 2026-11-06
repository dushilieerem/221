"""交易所涨跌停价面板加载。

数据侧的 ``derived/limit_prices`` 已按交易日和板块规则算好 ``upper_limit`` /
``lower_limit``（含 ST、创业板、科创板、北交所及 2026-07-06 后的 ST 规则），
事件引擎优先使用这份权威结果；数据集缺失时才回退到「昨收 × 板块比例」的
本地推算。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

LIMIT_FIELDS = ("upper_limit", "lower_limit")


def limit_prices_dir(data_dir: str | Path) -> Path:
    """按行情目录推导涨跌停数据集位置：``<materialized>/derived/limit_prices``。"""

    return Path(data_dir).resolve().parent / "derived" / "limit_prices"


def load_limit_panels(
    data_dir: str | Path,
    symbols: list[str],
    *,
    start: object | None = None,
    end: object | None = None,
) -> dict[str, pd.DataFrame]:
    """读取指定标的的涨跌停价，返回 ``{字段: 日期 × 代码 宽表}``。

    缺数据集时抛 ``FileNotFoundError``，调用方按降级处理；单个标的在区间内
    没有记录时对应列为 NaN，不报错。
    """

    root = limit_prices_dir(data_dir)
    if not any(root.glob("year=*/part.parquet")):
        raise FileNotFoundError(f"涨跌停价未发布: {root}")
    codes = sorted({str(symbol) for symbol in symbols})
    if not codes:
        return {}
    conditions = ["symbol in ({})".format(", ".join(_literal(code) for code in codes))]
    if start is not None:
        conditions.append(f"date >= {_literal(str(start)[:10])}")
    if end is not None:
        conditions.append(f"date <= {_literal(str(end)[:10])}")
    select = ", ".join(LIMIT_FIELDS)
    sql = (
        f"SELECT symbol, date, {select} FROM read_parquet("
        f"'{root}/year=*/part.parquet', union_by_name=true) "
        f"WHERE {' AND '.join(conditions)}"
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
            for field in LIMIT_FIELDS
        }
    frame["date"] = pd.to_datetime(frame["date"])
    result: dict[str, pd.DataFrame] = {}
    for field in LIMIT_FIELDS:
        panel = frame.pivot_table(
            index="date", columns="symbol", values=field, aggfunc="last"
        )
        result[field] = panel.reindex(columns=codes).sort_index().astype(float)
    return result


def _literal(value: str) -> str:
    """DuckDB 字符串字面量；代码来自请求，转义单引号即可。"""

    return "'" + value.replace("'", "''") + "'"


__all__ = ["LIMIT_FIELDS", "limit_prices_dir", "load_limit_panels"]
