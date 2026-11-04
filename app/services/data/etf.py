"""ETF 日线面板加载。

ETF 不并入 ``ifind/market_data`` 股票池（避免被当作股票），而是作为独立
数据集发布在视图的 ``free/etf_daily`` 下；契约要求使用方按 ``symbol + time``
与行情面板 join。事件引擎里策略可能用货币 ETF 做现金管理（例如破净 v3 的
511880），这里按需把它们并成与股票面板同构的「日期 × 代码」宽表。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

#: etf_daily 提供的行情字段。``amount`` 单位是万元，与股票面板的元不同，
#: 策略目前只用价格与成交量，保持原样以免误导。
ETF_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def etf_daily_dir(data_dir: str | Path) -> Path:
    """按行情目录推导 ETF 数据集位置：``<materialized>/free/etf_daily``。"""

    return Path(data_dir).resolve().parent / "free" / "etf_daily"


def load_etf_panels(
    data_dir: str | Path,
    symbols: list[str],
    *,
    start: object | None = None,
    end: object | None = None,
    fields: tuple[str, ...] = ETF_FIELDS,
) -> dict[str, pd.DataFrame]:
    """读取指定 ETF 的日线，返回 ``{字段: 日期 × 代码 宽表}``。

    缺数据集时抛 ``FileNotFoundError``，由调用方决定是否降级；单个 ETF 在
    区间内没有数据时对应列全为 NaN，不报错。
    """

    root = etf_daily_dir(data_dir)
    if not any(root.glob("year=*/date=*.parquet")):
        raise FileNotFoundError(f"ETF 日线未发布: {root}")
    codes = sorted({str(symbol) for symbol in symbols})
    if not codes:
        return {}
    conditions = ["symbol in ({})".format(", ".join(_literal(code) for code in codes))]
    if start is not None:
        conditions.append(f"time >= DATE {_literal(str(start)[:10])}")
    if end is not None:
        conditions.append(f"time <= DATE {_literal(str(end)[:10])}")
    select = ", ".join(fields)
    sql = (
        f"SELECT symbol, time, {select} FROM read_parquet("
        f"'{root}/year=*/date=*.parquet', union_by_name=true) "
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
            for field in fields
        }
    frame["time"] = pd.to_datetime(frame["time"])
    result: dict[str, pd.DataFrame] = {}
    for field in fields:
        panel = frame.pivot_table(
            index="time", columns="symbol", values=field, aggfunc="last"
        )
        result[field] = panel.reindex(columns=codes).sort_index().astype(float)
    return result


def _literal(value: str) -> str:
    """DuckDB 字符串字面量；代码来自请求，转义单引号即可。"""

    return "'" + value.replace("'", "''") + "'"
