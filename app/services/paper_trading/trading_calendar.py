"""模拟盘使用的交易日判断。

``marketdata/calendar/calendar_all_a.parquet`` 只登记交易日，不登记节假日；
超过该文件覆盖范围（通常是最近一次 release 之后）时只能按工作日乐观判断，
再结合行情面板是否已有当日数据决定是否可结算。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from threading import Lock

import pandas as pd

_CACHE: dict[tuple[str, int, int], pd.DatetimeIndex] = {}
_CACHE_LOCK = Lock()


def _data_root(data_dir: str | Path | None) -> Path | None:
    if not data_dir:
        return None
    root = Path(data_dir).expanduser()
    current = root / "current"
    return current if current.is_dir() else root


def calendar_path(data_dir: str | Path | None) -> Path | None:
    root = _data_root(data_dir)
    if root is None:
        return None
    for candidate in (
        root / "calendar" / "calendar_all_a.parquet",
        root / "calendar_all_a.parquet",
    ):
        if candidate.is_file():
            return candidate
    return None


def load_trading_days(data_dir: str | Path | None) -> pd.DatetimeIndex | None:
    """读取已登记的交易日；文件缺失或不可读时返回 None。"""

    path = calendar_path(data_dir)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        frame = pd.read_parquet(path)
    except (OSError, ValueError):
        return None
    date_column = "date" if "date" in frame.columns else frame.columns[0]
    days = pd.to_datetime(frame[date_column], errors="coerce").dropna()
    if "is_trade_day" in frame.columns:
        days = days[frame["is_trade_day"].fillna(False).astype(bool)]
    index = pd.DatetimeIndex(sorted(set(days)))
    with _CACHE_LOCK:
        _CACHE[key] = index
    return index


def classify_day(
    data_dir: str | Path | None,
    day: date | pd.Timestamp,
) -> str:
    """返回 ``trading_day`` / ``not_trading_day`` / ``unknown_trading_day``。

    ``unknown_trading_day`` 表示超过日历覆盖范围且是工作日，可能是交易日，
    需要继续看行情面板有没有当日数据。
    """

    timestamp = pd.Timestamp(day).normalize()
    days = load_trading_days(data_dir)
    if days is not None and len(days):
        if timestamp <= days[-1]:
            return "trading_day" if timestamp in days else "not_trading_day"
    if timestamp.weekday() >= 5:
        return "not_trading_day"
    return "unknown_trading_day"


def next_trading_day(data_dir: str | Path | None, day: date | pd.Timestamp) -> pd.Timestamp | None:
    """返回严格晚于 ``day`` 的下一个交易日。"""

    timestamp = pd.Timestamp(day).normalize()
    days = load_trading_days(data_dir)
    if days is not None and len(days) and timestamp < days[-1]:
        later = days[days > timestamp]
        if len(later):
            return later[0]
    candidate = timestamp + timedelta(days=1)
    for _ in range(14):
        if candidate.weekday() < 5:
            return candidate
        candidate += timedelta(days=1)
    return None


def previous_trading_day(data_dir: str | Path | None, day: date | pd.Timestamp) -> pd.Timestamp | None:
    """返回严格早于 ``day`` 的上一个交易日（仅用于展示/诊断）。"""

    timestamp = pd.Timestamp(day).normalize()
    days = load_trading_days(data_dir)
    if days is not None and len(days):
        earlier = days[days < timestamp]
        if len(earlier):
            return earlier[-1]
    candidate = timestamp - timedelta(days=1)
    for _ in range(14):
        if candidate.weekday() < 5:
            return candidate
        candidate -= timedelta(days=1)
    return None


__all__ = [
    "calendar_path",
    "classify_day",
    "load_trading_days",
    "next_trading_day",
    "previous_trading_day",
]
