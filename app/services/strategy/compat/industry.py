"""聚宽 ``get_industry``：从 date_sequence 读取逐日申万行业。

行情目录里的行业字段是 iFinD 的 THS 行业代码（如 ``S48``），不是聚宽的
``801xxx``。兼容层原样返回代码，并把它同时作为 ``industry_name``——策略里
按行业分组、每行业限仓的用法不受影响，直接比较聚宽行业代码的用法需登记
口径差异。

date_sequence 是 5956 列宽表，按需投影所需标的的行业列，避免整表载入。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
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

    def industries(
        self,
        symbols: list[str],
        as_of: str | object,
    ) -> dict[str, dict[str, dict[str, str]]]:
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
