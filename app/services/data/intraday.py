"""1 分钟行情读取：按日文件切片，供盘中时点对账使用。

数据布局：``<data_dir>/high_frequency/1min/<year>/<month>/<month>.<day>.parquet``，
列是 ``(symbol, field)`` 两级 MultiIndex，索引是 ``YYYY-MM-DD HH:MM:SS`` 字符串，
每个交易日 242 根（09:30–15:00）。

单日文件有约 2.7 万个 ``(symbol, close)`` 列，parquet 页脚解析本身就要
0.4 秒左右，因此：

* 缓存里保留 Arrow 表做单值读取，避免转 pandas 的额外开销；
* ``prefetch()`` 用后台线程按交易日顺序预读，把逐日页脚解析与事件回测的
  策略计算重叠起来。
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

import pandas as pd


class IntradayDataError(ValueError):
    """分钟数据缺失或格式不符。"""


_ABSENT = object()


def minute_file(data_dir: str | Path, date: str | pd.Timestamp) -> Path:
    day = pd.Timestamp(date)
    root = Path(data_dir) / "high_frequency" / "1min"
    return root / str(day.year) / str(day.month) / f"{day.month}.{day.day}.parquet"


@dataclass
class _MinuteDay:
    """单日分钟数据：保留 Arrow 表做单值读取，DataFrame 按需生成。"""

    table: Any
    index_name: str
    columns: frozenset[str]
    row_of: dict[str, int]
    frame: pd.DataFrame | None = dataclass_field(default=None)


class MinuteStore:
    """带少量缓存和后台预读的分钟面板读取器。

    ``price_at`` 是回测热路径（策略逐标的读取 ``last_price``）。按日文件
    有约 3 万个 ``(symbol, field)`` 列，若每次都转成 pandas 再按 MultiIndex
    取标量，单次约 5 毫秒；因此缓存里保留 Arrow 表，单值读取直接按列名和
    行号取，DataFrame 只在 ``panel()`` 被调用时才生成。
    """

    def __init__(self, data_dir: str | Path, *, max_days: int = 5) -> None:
        self.data_dir = Path(data_dir)
        self.max_days = max(1, int(max_days))
        self._days: OrderedDict[tuple[str, tuple[str, ...] | None], _MinuteDay | None] = (
            OrderedDict()
        )
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[tuple[str, tuple[str, ...] | None], Future] = {}
        self._queue: list[pd.Timestamp] = []
        self._queue_pos = 0
        self._queue_fields: tuple[str, ...] | None = ("close",)
        self._window = 0

    # ------------------------------------------------------------ 预读

    def prefetch(
        self,
        dates,
        fields: tuple[str, ...] | None = ("close",),
        *,
        window: int = 4,
        workers: int = 4,
    ) -> None:
        """按顺序预读给定交易日的分钟数据。

        只保留 ``window`` 个在途任务；消费到某一天时再提交后续日期，
        因此内存占用约为 ``(window + max_days)`` 天的 close 面板。
        """

        self.close()
        self._queue = [pd.Timestamp(item).normalize() for item in dates]
        self._queue_pos = 0
        self._queue_fields = fields
        self._window = max(1, int(window))
        self.max_days = max(self.max_days, self._window + 2)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(workers)),
            thread_name_prefix="minute-prefetch",
        )
        with self._lock:
            self._fill_locked()

    def close(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            self._futures.clear()

    def _fill_locked(self) -> None:
        executor = self._executor
        if executor is None:
            return
        while len(self._futures) < self._window and self._queue_pos < len(self._queue):
            day = self._queue[self._queue_pos]
            self._queue_pos += 1
            key = (day.strftime("%Y-%m-%d"), self._queue_fields)
            if key in self._days or key in self._futures:
                continue
            self._futures[key] = executor.submit(
                self._load, day, self._queue_fields
            )

    def _load(self, day: pd.Timestamp, fields: tuple[str, ...] | None):
        """后台读取；缺日/坏文件按「无数据」处理，交给调用方回退。"""

        try:
            return _read_day(self.data_dir, day, fields)
        except Exception:  # noqa: BLE001 - 预读失败不应中断回测
            return None

    # ------------------------------------------------------------ 读取

    def _day(
        self,
        day: pd.Timestamp,
        fields: tuple[str, ...] | None,
    ) -> _MinuteDay | None:
        key = (day.strftime("%Y-%m-%d"), fields)
        with self._lock:
            cached = self._days.get(key, _ABSENT)
            if cached is not _ABSENT:
                self._days.move_to_end(key)
                return cached
            future = self._futures.pop(key, None)
        if future is not None:
            loaded = future.result()
        else:
            try:
                loaded = _read_day(self.data_dir, day, fields)
            except IntradayDataError:
                loaded = None
            except OSError:
                loaded = None
        with self._lock:
            self._days[key] = loaded
            self._days.move_to_end(key)
            while len(self._days) > self.max_days:
                self._days.popitem(last=False)
            self._fill_locked()
        return loaded

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
        cached = self._day(day, fields)
        if cached is None:
            raise IntradayDataError(f"缺少分钟数据: {minute_file(self.data_dir, day)}")
        if cached.frame is None:
            frame = cached.table.to_pandas()
            if cached.index_name in frame.columns:
                frame = frame.set_index(cached.index_name)
            if not isinstance(frame.columns, pd.MultiIndex):
                raise IntradayDataError(
                    f"{minute_file(self.data_dir, day)}: 期望 (symbol, field) 两级列"
                )
            cached.frame = frame
        return cached.frame

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
        cached = self._day(day, (field,))
        if cached is None:
            return None
        column = f"('{symbol}', '{field}')"
        if column not in cached.columns:
            return None
        row = cached.row_of.get(minute)
        if row is None:
            return None
        value = cached.table.column(column)[row].as_py()
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)

    def closes(
        self,
        date: str | pd.Timestamp,
        time: str,
        symbols: list[str],
    ) -> pd.Series:
        """一次取多个标的在同一分钟的收盘价。"""

        day = pd.Timestamp(date).normalize()
        minute = _minute_key(day, time)
        cached = self._day(day, ("close",))
        if cached is None:
            return pd.Series(dtype=float)
        row = cached.row_of.get(minute)
        if row is None:
            return pd.Series(dtype=float)
        values: dict[str, float] = {}
        for symbol in symbols:
            column = f"('{symbol}', 'close')"
            if column not in cached.columns:
                continue
            value = cached.table.column(column)[row].as_py()
            if value is not None and not (
                isinstance(value, float) and math.isnan(value)
            ):
                values[symbol] = float(value)
        return pd.Series(values, dtype=float)


def _read_day(
    data_dir: Path,
    day: pd.Timestamp,
    fields: tuple[str, ...] | None,
) -> _MinuteDay:
    path = minute_file(data_dir, day)
    if not path.is_file():
        raise IntradayDataError(f"缺少分钟数据: {path}")
    table, index_name = _read_table(path, fields)
    columns = frozenset(table.schema.names)
    row_of: dict[str, int] = {}
    if index_name in columns:
        for row, value in enumerate(table.column(index_name).to_pylist()):
            row_of.setdefault(str(value), row)
    return _MinuteDay(
        table=table, index_name=index_name, columns=columns, row_of=row_of
    )


def _read_table(path: Path, fields: tuple[str, ...] | None):
    """读取所需列，返回 (Arrow 表, 索引列名)。"""

    import json

    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    names = parquet.schema_arrow.names
    metadata = parquet.metadata.metadata or {}
    index_columns: list[object] = []
    if b"pandas" in metadata:
        index_columns = json.loads(metadata[b"pandas"]).get("index_columns") or []
    index_name = (
        index_columns[0]
        if index_columns and isinstance(index_columns[0], str)
        else "date"
    )
    if fields is None:
        selected = list(names)
    else:
        selected = [index_name]
        for field in fields:
            suffix = f"'{field}')"
            selected.extend(name for name in names if name.endswith(suffix))
    return parquet.read(columns=selected), index_name


def _minute_key(day: pd.Timestamp, time: str) -> str:
    text = str(time).strip()
    if len(text) == 5:
        text = f"{text}:00"
    return f"{day.strftime('%Y-%m-%d')} {text}"
