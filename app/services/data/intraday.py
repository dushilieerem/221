"""1 分钟行情读取：按日文件切片，供盘中时点对账使用。

数据布局：``<data_dir>/high_frequency/1min/<year>/<month>/<month>.<day>.parquet``，
列是 ``(symbol, field)`` 两级 MultiIndex，索引是 ``YYYY-MM-DD HH:MM:SS`` 字符串，
每个交易日 242 根（09:30–15:00）。
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pandas as pd


class IntradayDataError(ValueError):
    """分钟数据缺失或格式不符。"""


def minute_file(data_dir: str | Path, date: str | pd.Timestamp) -> Path:
    day = pd.Timestamp(date)
    root = Path(data_dir) / "high_frequency" / "1min"
    return root / str(day.year) / str(day.month) / f"{day.month}.{day.day}.parquet"


class MinuteStore:
    """带少量缓存的分钟面板读取器。"""

    def __init__(self, data_dir: str | Path, *, max_days: int = 5) -> None:
        self.data_dir = Path(data_dir)
        self.max_days = max(1, int(max_days))
        self._cache: OrderedDict[str, pd.DataFrame] = OrderedDict()

    def panel(
        self,
        date: str | pd.Timestamp,
        fields: tuple[str, ...] | None = ("close",),
    ) -> pd.DataFrame:
        """读取某交易日的分钟面板。

        ``fields=None`` 读全表；默认只读 ``close``——事件回测的盘中成交价
        只需要收盘价，而按日宽表有 6 个字段、约 3 万列，只读 close 可把
        单日读取耗时降约 5 倍、内存降约 6 倍。
        """

        day = pd.Timestamp(date).normalize()
        key = (day.strftime("%Y-%m-%d"), fields)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        path = minute_file(self.data_dir, day)
        if not path.is_file():
            raise IntradayDataError(f"缺少分钟数据: {path}")
        if fields is None:
            frame = pd.read_parquet(path)
        else:
            import json

            import pyarrow.parquet as pq

            parquet = pq.ParquetFile(path)
            names = parquet.schema_arrow.names
            metadata = parquet.metadata.metadata or {}
            index_columns = []
            if b"pandas" in metadata:
                index_columns = json.loads(metadata[b"pandas"]).get(
                    "index_columns"
                ) or []
            index_name = (
                index_columns[0]
                if index_columns and isinstance(index_columns[0], str)
                else "date"
            )
            selected = [index_name]
            for field in fields:
                suffix = f"'{field}')"
                selected.extend(name for name in names if name.endswith(suffix))
            frame = parquet.read(columns=selected).to_pandas()
            # pyarrow 会按 pandas 元数据把索引列还原成索引；只有在列里仍存在
            # 索引列时才需要显式 set_index。
            if index_name in frame.columns:
                frame = frame.set_index(index_name)
        if not isinstance(frame.columns, pd.MultiIndex):
            raise IntradayDataError(f"{path}: 期望 (symbol, field) 两级列")
        self._cache[key] = frame
        while len(self._cache) > self.max_days:
            self._cache.popitem(last=False)
        return frame

    def price_at(
        self,
        date: str | pd.Timestamp,
        time: str,
        symbol: str,
        *,
        field: str = "close",
    ) -> float | None:
        """取指定分钟的价格；``time`` 支持 ``09:30`` 或 ``09:30:00``。"""

        day = pd.Timestamp(date).normalize()
        minute = _minute_key(day, time)
        frame = self.panel(day)
        if (symbol, field) not in frame.columns:
            return None
        if minute not in frame.index:
            return None
        value = frame.at[minute, (symbol, field)]
        return float(value) if pd.notna(value) else None

    def closes(
        self,
        date: str | pd.Timestamp,
        time: str,
        symbols: list[str],
    ) -> pd.Series:
        """一次取多个标的在同一分钟的收盘价。"""

        day = pd.Timestamp(date).normalize()
        minute = _minute_key(day, time)
        frame = self.panel(day)
        available = [
            (symbol, "close") for symbol in symbols if (symbol, "close") in frame.columns
        ]
        if not available or minute not in frame.index:
            return pd.Series(dtype=float)
        values = frame.loc[minute, available]
        values.index = [symbol for symbol, _ in available]
        return pd.to_numeric(values, errors="coerce")


def _minute_key(day: pd.Timestamp, time: str) -> str:
    text = str(time).strip()
    if len(text) == 5:
        text = f"{text}:00"
    return f"{day.strftime('%Y-%m-%d')} {text}"
