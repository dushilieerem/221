"""模拟盘的成交价与估值价视图。

- 成交价：D 日 ``amount / volume``，即原始成交额除以成交量得到的 VWAP。
  研究行情是后复权 OHLC，但 ``amount``/``volume`` 保持原始口径，因此该
  VWAP 与未复权行情同一量纲。
- 估值价：D 日未复权 close。未复权行情单独存放在 ``unadjusted_price``，
  不能混入策略信号面板。
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd

from app.database.data.providers.field_parquet_provider import FieldParquetProvider

_TRADING_DAYS_CACHE: dict[str, pd.DatetimeIndex] = {}
_DISPLAY_PROVIDERS: dict[tuple[str, int, int], FieldParquetProvider] = {}
_CACHE_LOCK = Lock()


def _data_root(data_engine) -> Path | None:
    value = getattr(data_engine, "data_dir", None)
    return Path(value) if value is not None else None


def release_trading_days(data_engine) -> pd.DatetimeIndex:
    """返回当前 release 行情面板覆盖的交易日（按 release 缓存）。"""

    release_key = str(getattr(data_engine, "release_id", None) or "")
    cached = _TRADING_DAYS_CACHE.get(release_key)
    if cached is not None and release_key:
        return cached
    panels = data_engine.get_task_panels(None, None, fields=["close"], symbols=[])
    index = pd.DatetimeIndex(panels["close"].index).sort_values()
    if release_key:
        with _CACHE_LOCK:
            _TRADING_DAYS_CACHE[release_key] = index
    return index


def build_vwap_panel(panels) -> pd.DataFrame:
    """由 ``amount``/``volume`` 构造原始价格量纲的 VWAP 面板。

    任一输入缺失、成交量不大于 0、结果非有限或不大于 0 时置 NaN；
    不回退 close 或典型价格。
    """

    amount = panels["amount"]
    volume = panels["volume"]
    vwap = amount.astype(float) / volume.astype(float)
    valid = (
        amount.notna()
        & volume.notna()
        & (volume > 0)
        & np.isfinite(vwap)
        & (vwap > 0)
    )
    return vwap.where(valid)


def _display_candidates(data_engine) -> list[Path]:
    root = _data_root(data_engine)
    if root is None:
        return []
    return [
        root / "unadjusted_price" / "unadjusted_price.parquet",
        root / "display" / "unadjusted_price.parquet",
        root / "unadjusted_price.parquet",
    ]


def _display_provider(data_engine) -> FieldParquetProvider | None:
    for path in _display_candidates(data_engine):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        cached = _DISPLAY_PROVIDERS.get(key)
        if cached is not None:
            return cached
        try:
            provider = FieldParquetProvider(path)
        except (OSError, ValueError, KeyError):
            continue
        with _CACHE_LOCK:
            _DISPLAY_PROVIDERS[key] = provider
        return provider
    return None


def load_unadjusted_close(
    data_engine,
    *,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None,
    symbols: list[str],
) -> pd.DataFrame:
    """读取未复权 close，返回日期×交易所代码；文件缺失时返回空表。"""

    provider = _display_provider(data_engine)
    if provider is None:
        return pd.DataFrame(columns=symbols)
    panels = provider.get_task_panels(
        str(start) if start is not None else None,
        str(end) if end is not None else None,
        fields=["close"],
        symbols=list(symbols),
    )
    frame = panels["close"]
    return frame.reindex(columns=pd.Index(symbols))


def mark_price_on(
    mark_panel: pd.DataFrame,
    *,
    day: pd.Timestamp,
    symbol: str,
) -> tuple[float | None, bool]:
    """取 D 日未复权 close；缺失时回退到 ``<=D`` 的最后一个有效值。

    返回 ``(价格, 是否使用了回退值)``；完全没有数据时返回 ``(None, True)``。
    """

    if mark_panel is None or mark_panel.empty or symbol not in mark_panel.columns:
        return None, True
    series = mark_panel[symbol]
    value = series.get(day)
    if value is not None and pd.notna(value) and np.isfinite(value) and value > 0:
        return float(value), False
    earlier = series.loc[:day].dropna()
    for candidate in reversed(earlier.tolist()):
        if np.isfinite(candidate) and candidate > 0:
            return float(candidate), True
    return None, True


__all__ = [
    "build_vwap_panel",
    "load_unadjusted_close",
    "mark_price_on",
    "release_trading_days",
]
