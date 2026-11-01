"""聚宽指数成分：读取行情目录里的历史权重快照。

数据布局：``<data_dir>/index/index_weights/index_weights_<code>.parquet``，
宽表（日期 × 标的，值为权重），覆盖 2005 年至今的逐日快照。取 ``date`` 当日
或之前最近一个快照的非空列作为成分股，避免用最新成分回看历史。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

#: 聚宽指数代码后缀 -> 本仓库代码后缀。
_SUFFIX = {"XSHG": "SH", "XSHE": "SZ"}


def normalize_index_code(code: str) -> str:
    """``000300.XSHG`` -> ``000300.SH``；已是本仓库代码时原样返回。"""

    text = str(code).strip()
    if "." not in text:
        return text
    base, suffix = text.rsplit(".", 1)
    return f"{base}.{_SUFFIX.get(suffix.upper(), suffix.upper())}"


class IndexPriceStore:
    """指数日线：``<data_dir>/index/index_price/index_price_<code>.parquet``。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "index" / "index_price"
        self._cache: dict[str, pd.DataFrame] = {}

    def has(self, index_code: str) -> bool:
        return (self.root / f"index_price_{normalize_index_code(index_code)}.parquet").is_file()

    def history(
        self,
        index_code: str,
        fields: list[str],
        *,
        count: int | None = None,
        start: object | None = None,
        end: object | None = None,
    ) -> pd.DataFrame:
        frame = self._load(normalize_index_code(index_code))
        if start is not None:
            frame = frame.loc[frame.index >= pd.Timestamp(start)]
        if end is not None:
            frame = frame.loc[frame.index <= pd.Timestamp(end)]
        if count is not None:
            frame = frame.iloc[-int(count):]
        columns = [field for field in fields if field in frame.columns]
        return frame.loc[:, columns].copy()

    def _load(self, code: str) -> pd.DataFrame:
        cached = self._cache.get(code)
        if cached is not None:
            return cached
        path = self.root / f"index_price_{code}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"指数行情数据不存在: {path}")
        frame = pd.read_parquet(path).sort_index()
        self._cache[code] = frame
        return frame


class IndexMembership:
    """按交易日读取指数成分（权重非空即视为成分股）。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "index" / "index_weights"
        self._cache: dict[str, pd.DataFrame] = {}

    def members(self, index_code: str, date: str | pd.Timestamp | None = None) -> list[str]:
        code = normalize_index_code(index_code)
        frame = self._load(code)
        if date is None:
            row = frame.iloc[-1]
        else:
            target = pd.Timestamp(date).normalize()
            visible = frame.loc[frame.index <= target]
            if visible.empty:
                raise ValueError(f"指数 {code} 在 {target.date()} 及之前没有成分快照")
            row = visible.iloc[-1]
        return [str(symbol) for symbol in row.dropna().index]

    def _load(self, code: str) -> pd.DataFrame:
        cached = self._cache.get(code)
        if cached is not None:
            return cached
        path = self.root / f"index_weights_{code}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"指数成分数据不存在: {path}")
        frame = pd.read_parquet(path).sort_index()
        self._cache[code] = frame
        return frame
