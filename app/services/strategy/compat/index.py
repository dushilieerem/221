"""聚宽指数成分：读取行情目录里的历史权重快照。

数据布局：``<data_dir>/index/index_weights/index_weights_<code>.parquet``，
宽表（日期 × 标的，值为权重），覆盖 2005 年至今的逐日快照。取 ``date`` 当日
或之前最近一个快照的非空列作为成分股，避免用最新成分回看历史。

iFinD 没有覆盖的指数（399317/399101/399303/000015）改读数据发布视图的
``derived/index_membership``（按年分区、长表，权重可能为空）。长表有行即成分，
不能按权重是否为空判断。
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
        base = Path(data_dir)
        self.root = base / "index" / "index_price"
        # 发布视图的派生指数日线：``views/current/derived/index_price``。
        self.derived = base.parent / "derived" / "index_price" / "index_price.parquet"
        self._cache: dict[str, pd.DataFrame] = {}
        self._derived_index: set[str] | None = None

    def has(self, index_code: str) -> bool:
        if (self.root / f"index_price_{normalize_index_code(index_code)}.parquet").is_file():
            return True
        return normalize_index_code(index_code) in self._derived_codes()

    def _derived_codes(self) -> set[str]:
        if self._derived_index is None:
            if not self.derived.is_file():
                self._derived_index = set()
            else:
                frame = pd.read_parquet(self.derived, columns=["index_code"])
                self._derived_index = set(frame["index_code"].astype(str).unique())
        return self._derived_index

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
        if path.is_file():
            frame = pd.read_parquet(path).sort_index()
        elif code in self._derived_codes():
            frame = self._load_derived(code)
        else:
            raise FileNotFoundError(f"指数行情数据不存在: {path}")
        self._cache[code] = frame
        return frame

    def _load_derived(self, code: str) -> pd.DataFrame:
        frame = pd.read_parquet(self.derived).loc[
            lambda df: df["index_code"].astype(str) == code
        ]
        frame = frame.drop(columns=["index_code", "source"], errors="ignore")
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.set_index("date").sort_index()


class IndexMembership:
    """按交易日读取指数成分（快照里有该标的即视为成分股）。"""

    def __init__(self, data_dir: str | Path) -> None:
        base = Path(data_dir)
        self.root = base / "index" / "index_weights"
        # 发布视图的派生指数成分：``views/current/derived/index_membership``。
        self.derived = base.parent / "derived" / "index_membership"
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
        if path.is_file():
            frame = pd.read_parquet(path).sort_index()
        else:
            frame = self._load_derived(code)
            if frame is None:
                raise FileNotFoundError(f"指数成分数据不存在: {path}")
        self._cache[code] = frame
        return frame

    def _load_derived(self, code: str) -> pd.DataFrame | None:
        partitions = sorted(self.derived.glob("year=*/part.parquet"))
        if not partitions:
            return None
        pieces = []
        for path in partitions:
            frame = pd.read_parquet(
                path,
                columns=["date", "index_code", "symbol"],
                filters=[("index_code", "==", code)],
            )
            if not frame.empty:
                pieces.append(frame)
        if not pieces:
            return None
        long = pd.concat(pieces, ignore_index=True)
        long = long.dropna(subset=["date", "symbol"])
        long["date"] = pd.to_datetime(long["date"])
        # 长表有行即成分；权重为空不等于不是成分，统一记 1.0。
        long["member"] = 1.0
        return (
            long.pivot_table(
                index="date",
                columns="symbol",
                values="member",
                aggfunc="max",
            )
            .sort_index()
        )
