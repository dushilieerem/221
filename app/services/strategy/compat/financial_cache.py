"""财务表进程内缓存。

兼容层按 as_of 反复查询同一批财务 Parquet。每次查询都让 DuckDB 扫描
5000+ 个逐股文件，单次重平衡就会累积到秒级；这里把所需的列一次性读入
内存，之后只做 pandas 过滤。

缓存键包含发行根目录、表名与列集合；PIT 过滤仍按每次请求的 ``as_of`` 做，
不会把未来公告的数据暴露给历史时点。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

_CACHE: dict[tuple[str, str, tuple[str, ...]], pd.DataFrame] = {}
#: 表目录解析结果缓存；逐日查询时不要再对 5000+ 个文件做两次 glob。
_DIRECTORY_CACHE: dict[tuple[str, str], Path] = {}

_BASE_COLUMNS = ("Symbol", "m_timetag", "m_anntime")


def _resolve_directory(root: str | Path, statement: str) -> Path:
    key = (str(Path(root).resolve()), statement)
    cached = _DIRECTORY_CACHE.get(key)
    if cached is not None:
        return cached
    directory = Path(root) / "3_financial_data" / statement
    if not any(directory.glob("*.parquet")):
        # 新发布视图的 compat 目录只含策略直接读取的几张表；股本、分红等
        # 在 free/ 下的独立 dataset 目录，按别名回退。
        alias = {"dividend_factors": "dividend_actions"}.get(statement, statement)
        fallback = Path(root).parent / alias
        if any(fallback.glob("*.parquet")):
            directory = fallback
    if not any(directory.glob("*.parquet")):
        raise FileNotFoundError(f"财务表未下载: {directory}")
    _DIRECTORY_CACHE[key] = directory
    return directory


def load_statement(
    root: str | Path,
    statement: str,
    columns: list[str] | tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """读取整张财务表（全部历史），按根目录/表/列缓存。"""

    directory = _resolve_directory(root, statement)

    requested = tuple(sorted(set(columns or ())))
    key = (str(Path(root).resolve()), statement, requested)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    first_file = next(iter(directory.glob("*.parquet")))
    available = set(pq.ParquetFile(first_file).schema_arrow.names)
    base = [column for column in _BASE_COLUMNS if column in available]
    fields = base + [column for column in requested if column not in base]
    # requested 为空表示整表（如 _income_rows）；不能只选基础列。
    select = "*" if not requested else ", ".join(f'"{column}"' for column in fields)
    sql = (
        f"SELECT {select} FROM read_parquet("
        f"'{directory / '*.parquet'}', union_by_name=true)"
    )
    con = duckdb.connect()
    try:
        frame = con.execute(sql).fetch_df()
    finally:
        con.close()

    if "m_anntime" in frame.columns:
        frame["_anntime_int"] = pd.to_numeric(frame["m_anntime"], errors="coerce")
    _CACHE[key] = frame
    return frame


def visible(frame: pd.DataFrame, as_of: str | pd.Timestamp) -> pd.DataFrame:
    """只保留公告日不晚于 ``as_of`` 的记录。"""

    key = int(pd.Timestamp(as_of).strftime("%Y%m%d"))
    return frame.loc[frame["_anntime_int"] <= key]


def clear_cache() -> None:
    _CACHE.clear()
    _DIRECTORY_CACHE.clear()


__all__ = ["load_statement", "visible", "clear_cache"]
