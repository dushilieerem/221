"""数据层共享工具。"""

from __future__ import annotations

from typing import Mapping, Optional

import pandas as pd





def first_panel_date(panels: Mapping[str, pd.DataFrame]) -> Optional[pd.Timestamp]:
    """返回已按回测区间过滤后的首个实际行情日。"""
    if "close" in panels:
        close = panels["close"]
        # 日期探针可能只读取索引而不读取任何标的列；DataFrame.empty 在
        # ``(N, 0)`` 时也会返回 True，但它仍然包含有效交易日。
        if len(close.index):
            return pd.Timestamp(close.index[0])
    for field in panels:
        if field == "close":
            continue
        frame = panels[field]
        if len(frame.index):
            return pd.Timestamp(frame.index[0])
    return None


def filter_by_date(
    df: pd.DataFrame,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    """按日期索引闭区间过滤。"""
    if start is not None:
        df = df.loc[df.index >= pd.to_datetime(start)]
    if end is not None:
        df = df.loc[df.index <= pd.to_datetime(end)]
    return df
