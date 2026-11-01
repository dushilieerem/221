"""聚宽兼容层的证券主数据：名称、上市日、行业。"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd

DEFAULT_ROOT = Path(
    os.environ.get(
        "MODELSCOPE_ROOT",
        "/home/dev01/project/svquant-data/free/staging/modelscope",
    )
)


class SecurityMaster:
    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self._names: dict[str, str] = {}
        self._start_dates: dict[str, date] = {}
        self._industries: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        path = (
            self.root / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
        )
        if not path.is_file():
            return
        frame = pd.read_parquet(
            path, columns=["Symbol", "Name", "J_start", "rs_hyname"]
        )
        for row in frame.itertuples(index=False):
            symbol = str(row.Symbol)
            name = str(row.Name) if pd.notna(row.Name) else symbol
            self._names[symbol] = name
            self._industries[symbol] = str(row.rs_hyname) if pd.notna(row.rs_hyname) else ""
            start = _parse_date(row.J_start)
            if start is not None:
                self._start_dates[symbol] = start

    def name(self, symbol: str) -> str:
        return self._names.get(symbol, str(symbol))

    def start_date(self, symbol: str) -> date | None:
        return self._start_dates.get(symbol)

    def industry(self, symbol: str) -> str:
        return self._industries.get(symbol, "")


def _parse_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return pd.Timestamp(text, format=fmt).date()
        except (TypeError, ValueError):
            continue
    try:
        return pd.Timestamp(text).date()
    except (TypeError, ValueError):
        return None
