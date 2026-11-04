"""聚宽 ``get_industry``：从 date_sequence 读取逐日申万行业。

行情目录里的行业字段是 iFinD 的 THS 行业代码（如 ``S48``），不是聚宽的
``801xxx``。兼容层原样返回代码，并把它同时作为 ``industry_name``——策略里
按行业分组、每行业限仓的用法不受影响，直接比较聚宽行业代码的用法需登记
口径差异。

date_sequence 是 5956 列宽表，按需投影所需标的的行业列，避免整表载入。
"""

from __future__ import annotations

import os
from bisect import bisect_right
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

_INDUSTRY_FIELD = "ths_the_sw_industry_code_stock"

#: 可选的 PIT 行业历史覆盖文件（CSV/Parquet）。列：
#: date, symbol, sw_l1_code, sw_l1_name, sw_l2_code, sw_l2_name。
#: 生产视图里的 industry_daily 是 latest_only；对账时用聚宽导出的历史快照
#: 通过这个文件覆盖，避免 2016–2020 用今天的行业归属。
INDUSTRY_HISTORY_ENV = "RE_INDUSTRY_HISTORY"


def _normalize_industry_code(value: object) -> str:
    """``801780.SI`` / ``801780`` -> ``801780``；THS 代码原样返回。"""

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if pd.isna(value):
        return ""
    return text.rsplit(".", 1)[0] if "." in text else text


def _column_symbol(name: str) -> str | None:
    """``("000001.SZ", "ths_the_sw_industry_code_stock")`` -> ``000001.SZ``。"""

    if not name.endswith(f"'{_INDUSTRY_FIELD}')"):
        return None
    parts = name.split("'")
    return parts[1] if len(parts) >= 3 else None


def _normalize_symbol(code: object) -> str:
    """``000001.XSHE`` -> ``000001.SZ``；其它保持原样。"""

    text = str(code).strip()
    for suffix, mapped in ((".XSHG", ".SH"), (".XSHE", ".SZ"), (".XBSE", ".BJ")):
        if text.upper().endswith(suffix):
            return text[: -len(suffix)] + mapped
    return text


def _normalize_code_value(value: object) -> object:
    """CSV 里 801780 会读成浮点 ``801780.0``，还原成 ``801780``。"""

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _load_history(
    path: Path,
) -> tuple[list[pd.Timestamp], dict[pd.Timestamp, pd.DataFrame]] | None:
    """读取 PIT 行业快照（CSV 或 Parquet），按日期建索引。"""

    if not path.is_file():
        raise FileNotFoundError(f"行业历史覆盖文件不存在: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path, encoding="utf-8-sig")
    if "symbol" not in frame.columns and "code" in frame.columns:
        frame = frame.rename(columns={"code": "symbol"})
    required = {"date", "symbol", "sw_l1_code"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"行业历史文件缺少列: {sorted(missing)}")
    # 聚宽导出的代码是 000001.XSHE，与引擎的 000001.SZ 对齐。
    frame["symbol"] = frame["symbol"].map(_normalize_symbol)
    for column in ("sw_l1_code", "sw_l2_code"):
        if column in frame.columns:
            frame[column] = frame[column].map(_normalize_code_value)
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame = frame.drop_duplicates(["date", "symbol"], keep="last")
    by_date = {
        date: group.set_index("symbol")
        for date, group in frame.groupby("date", sort=True)
    }
    return sorted(by_date), by_date


class IndustryStore:
    """按交易日读取个股 THS 一级行业代码。"""

    def __init__(self, data_dir: str | Path, history_path: str | Path | None = None) -> None:
        self.path = Path(data_dir) / "date_sequence" / "date_sequence.parquet"
        self._columns: dict[str, str] | None = None
        self._derived_dir = (
            Path(data_dir).parent / "derived" / "industry_daily"
        )
        self._derived: pd.DataFrame | None = None
        self._derived_by_date: dict[pd.Timestamp, pd.DataFrame] = {}
        self._derived_dates: list[pd.Timestamp] = []
        # 可选 PIT 覆盖：优先于 latest_only 的 industry_daily。
        override = history_path or os.environ.get(INDUSTRY_HISTORY_ENV)
        self._history: tuple[list[pd.Timestamp], dict[pd.Timestamp, pd.DataFrame]] | None = (
            _load_history(Path(override)) if override else None
        )

    def industries(
        self,
        symbols: list[str],
        as_of: str | object,
    ) -> dict[str, dict[str, dict[str, str]]]:
        if self._history is not None:
            return self._history_industries(symbols, as_of)
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

    def _history_snapshot(self, as_of: str | object) -> pd.DataFrame | None:
        """取不晚于 ``as_of`` 的最近一个行业快照。"""

        if self._history is None:
            return None
        dates, by_date = self._history
        target = pd.Timestamp(as_of).normalize()
        position = bisect_right(dates, target) - 1
        if position < 0:
            return None
        return by_date[dates[position]]

    def _history_industries(
        self, symbols: list[str], as_of: str | object
    ) -> dict[str, dict[str, dict[str, str]]]:
        snapshot = self._history_snapshot(as_of)
        if snapshot is None or snapshot.empty:
            return {}
        rows = snapshot.reindex(symbols)
        result: dict[str, dict[str, dict[str, str]]] = {}
        for symbol, row in rows.iterrows():
            payload: dict[str, dict[str, str]] = {}
            for level, code_column, name_column in (
                ("sw_l1", "sw_l1_code", "sw_l1_name"),
                ("sw_l2", "sw_l2_code", "sw_l2_name"),
            ):
                code = row.get(code_column)
                if code is None or pd.isna(code):
                    continue
                name = row.get(name_column)
                payload[level] = {
                    "industry_code": _normalize_industry_code(code),
                    "industry_name": (
                        str(name)
                        if name is not None and not pd.isna(name)
                        else str(code)
                    ),
                }
            if payload:
                result[str(symbol)] = payload
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
            # 聚宽返回不带 .SI 后缀的 801xxx；策略里直接拿它和 '801780' 这类
            # 字面量比较，带后缀会静默失配（例如银行/地产剔除永远不生效）。
            normalized = _normalize_industry_code(code)
            if not normalized:
                continue
            result[str(row.symbol)] = {
                "sw_l1": {
                    "industry_code": normalized,
                    "industry_name": str(name),
                }
            }
        return result

    def stocks(self, industry_code: str, as_of: str | object) -> list[str]:
        """聚宽 ``get_industry_stocks``：某申万一级行业在给定日期的成分股。"""

        target = _normalize_industry_code(industry_code)
        if not target:
            return []
        if self._history is not None:
            snapshot = self._history_snapshot(as_of)
            if snapshot is None or snapshot.empty:
                return []
            codes = snapshot.get("sw_l1_code", pd.Series(dtype=object))
            return [
                str(symbol)
                for symbol, code in zip(snapshot.index, codes)
                if _normalize_industry_code(code) == target
            ]
        target_date = pd.Timestamp(as_of).normalize()
        cache = getattr(self, "_stocks_cache", None)
        if cache is None or cache[0] != target_date:
            frame = self._derived_frame()
            if frame is None or frame.empty:
                return []
            position = bisect_right(self._derived_dates, target_date) - 1
            if position < 0:
                return []
            snapshot = self._derived_by_date[self._derived_dates[position]]
            buckets: dict[str, list[str]] = {}
            for symbol, code in zip(
                snapshot.index, snapshot.get("sw_l1_code", pd.Series(dtype=object))
            ):
                normalized = _normalize_industry_code(code)
                if normalized:
                    buckets.setdefault(normalized, []).append(str(symbol))
            cache = (target_date, buckets)
            self._stocks_cache = cache
        return list(cache[1].get(target, []))

    def _derived_frame(self) -> pd.DataFrame | None:
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
        return self._derived

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
