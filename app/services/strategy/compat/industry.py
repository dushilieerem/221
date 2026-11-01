"""聚宽 ``get_industry``：从 date_sequence 读取逐日申万行业。

行情目录里的行业字段是 iFinD 的 THS 行业代码（如 ``S48``），不是聚宽的
``801xxx``。兼容层原样返回代码，并把它同时作为 ``industry_name``——策略里
按行业分组、每行业限仓的用法不受影响，直接比较聚宽行业代码的用法需登记
口径差异。

date_sequence 是 5956 列宽表，按需投影所需标的的行业列，避免整表载入。
"""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

_INDUSTRY_FIELD = "ths_the_sw_industry_code_stock"


def _column_symbol(name: str) -> str | None:
    """``("000001.SZ", "ths_the_sw_industry_code_stock")`` -> ``000001.SZ``。"""

    if not name.endswith(f"'{_INDUSTRY_FIELD}')"):
        return None
    parts = name.split("'")
    return parts[1] if len(parts) >= 3 else None


class IndustryStore:
    """按交易日读取个股 THS 一级行业代码。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / "date_sequence" / "date_sequence.parquet"
        self._columns: dict[str, str] | None = None
        self._derived_dir = (
            Path(data_dir).parent / "derived" / "industry_daily"
        )
        self._derived: pd.DataFrame | None = None
        self._derived_by_date: dict[pd.Timestamp, pd.DataFrame] = {}
        self._derived_dates: list[pd.Timestamp] = []

    def industries(
        self,
        symbols: list[str],
        as_of: str | object,
    ) -> dict[str, dict[str, dict[str, str]]]:
        if self._derived_dir.is_dir():
            return self._derived_industries(symbols, as_of)
        columns = self._columns_for()
        selected = [(symbol, columns[symbol]) for symbol in symbols if symbol in columns]
        if not selected:
            return {}
        def _quote(name: str) -> str:
            return '"' + name.replace('"', '""') + '"'

        projection = ", ".join(
            f"{_quote(column)} AS {_quote(symbol)}" for symbol, column in selected
        )
        sql = (
            f"SELECT {projection} FROM read_parquet(?) "
            "WHERE date = CAST(? AS DATE)"
        )
        con = duckdb.connect()
        try:
            frame = con.execute(sql, [str(self.path), str(as_of)[:10]]).fetch_df()
        finally:
            con.close()
        if frame.empty:
            return {}
        row = frame.iloc[0]
        result: dict[str, dict[str, dict[str, str]]] = {}
        for symbol, _ in selected:
            value = row[symbol]
            if value is None or value != value:
                continue
            code = str(value)
            result[symbol] = {
                "sw_l1": {"industry_code": code, "industry_name": code}
            }
        return result

    def _derived_industries(
        self, symbols: list[str], as_of: str | object
    ) -> dict[str, dict[str, dict[str, str]]]:
        """优先读 P0-7 的 industry_daily 长表（含 SW 代码/名称）。"""

        if self._derived is None:
            files = sorted(self._derived_dir.rglob("*.parquet"))
            if not files:
                self._derived = pd.DataFrame()
            else:
                self._derived = pd.concat(
                    [pd.read_parquet(path) for path in files], ignore_index=True
                )
                self._derived["date"] = pd.to_datetime(
                    self._derived["date"]
                ).dt.normalize()
                self._derived_by_date = {
                    date: group.set_index("symbol")
                    for date, group in self._derived.groupby("date", sort=True)
                }
                self._derived_dates = sorted(self._derived_by_date)
        frame = self._derived
        if frame is None or frame.empty:
            return {}
        target = pd.Timestamp(as_of).normalize()
        position = bisect_right(self._derived_dates, target) - 1
        if position < 0:
            return {}
        snapshot = self._derived_by_date[self._derived_dates[position]]
        rows = snapshot.reindex(symbols).reset_index()
        result: dict[str, dict[str, dict[str, str]]] = {}
        for row in rows.itertuples(index=False):
            code = getattr(row, "sw_l1_code", None)
            name = getattr(row, "sw_l1_name", None)
            if code is None or pd.isna(code):
                code = getattr(row, "ths_code", None)
            if code is None or pd.isna(code):
                continue
            name = name if name is not None and not pd.isna(name) else str(code)
            result[str(row.symbol)] = {
                "sw_l1": {"industry_code": str(code), "industry_name": str(name)}
            }
        return result

    def _columns_for(self) -> dict[str, str]:
        if self._columns is not None:
            return self._columns
        if not self.path.is_file():
            raise FileNotFoundError(f"行业数据不存在: {self.path}")
        names = pq.ParquetFile(self.path).schema_arrow.names
        columns: dict[str, str] = {}
        for name in names:
            symbol = _column_symbol(name)
            if symbol is not None:
                columns[symbol] = name
        self._columns = columns
        return columns
