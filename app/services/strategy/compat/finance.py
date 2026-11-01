"""聚宽 ``finance.run_query`` 的最小实现：分红事件。

免费源的 ``dividend_factors`` 提供每 10 股派息 ``interest``，没有聚宽的
``bonus_amount_rmb``（万元）。这里用 PIT 总股本换算：

    每股现金 = interest / 10
    派息总额（万元） = 每股现金 × 总股本 / 10000

``time`` 作为 ``a_registration_date`` 的近似（免费源未区分登记日/除权日）。
差异登记在 FEAT-034 的口径说明里，不静默当成聚宽原值。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from app.services.strategy.compat.financial_cache import load_statement, visible
from app.services.strategy.compat.query import Field, Query, evaluate

DEFAULT_ROOT = Path(
    os.environ.get(
        "MODELSCOPE_ROOT",
        "/home/dev01/project/svquant-data/free/staging/modelscope",
    )
)

_TABLE = "finance.STK_XR_XD"


class _DividendFields:
    code = Field(_TABLE, "code")
    a_registration_date = Field(_TABLE, "a_registration_date")
    bonus_amount_rmb = Field(_TABLE, "bonus_amount_rmb")


class FinanceNamespace:
    """聚宽 ``finance`` 命名空间；当前只覆盖 ``STK_XR_XD``。"""

    STK_XR_XD = _DividendFields()

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self._prepared: pd.DataFrame | None = None

    def _prepared_frame(self) -> pd.DataFrame:
        """全量分红 + PIT 股本；与查询日期无关，进程内只算一次。"""

        if self._prepared is not None:
            return self._prepared
        dividends = load_statement(
            self.root, "dividend_factors", ["time", "interest", "stockBonus"]
        )
        capital = load_statement(self.root, "capital", ["total_capital"])

        frame = dividends[["Symbol", "time", "interest", "stockBonus"]].copy()
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce").astype(
            "datetime64[ns]"
        )
        frame = frame.dropna(subset=["time"]).sort_values(["time", "Symbol"])

        shares = capital[["Symbol", "m_anntime", "total_capital"]].copy()
        shares["announce"] = pd.to_datetime(
            shares["m_anntime"].astype(str), format="%Y%m%d", errors="coerce"
        ).astype("datetime64[ns]")
        shares = shares.dropna(subset=["announce"]).sort_values(
            ["announce", "Symbol"]
        )
        merged = pd.merge_asof(
            frame,
            shares,
            left_on="time",
            right_on="announce",
            by="Symbol",
            direction="backward",
        )
        cash_per_share = pd.to_numeric(
            merged["interest"], errors="coerce"
        ).fillna(0.0) / 10.0
        total_shares = pd.to_numeric(merged["total_capital"], errors="coerce")
        bonus = cash_per_share * total_shares / 10000.0
        bonus = bonus.replace([np.inf, -np.inf], np.nan)
        # 预先把列名改成聚宽查询字段，后续每次查询直接布尔过滤，避免重复
        # 构造 20 万行的输出帧（dividend_query 会按 limit 做日期/代码二分）。
        prepared = pd.DataFrame({
            f"{_TABLE}.code": merged["Symbol"].to_numpy(),
            f"{_TABLE}.a_registration_date": merged["time"].to_numpy(),
            f"{_TABLE}.bonus_amount_rmb": bonus.to_numpy(),
        })
        self._prepared = prepared
        return prepared

    def run_query(self, q: Query) -> pd.DataFrame:
        table = q.fields[0].table if q.fields else _TABLE
        if table != _TABLE:
            raise NotImplementedError(f"兼容层暂不支持 finance 表: {table}")

        output = self._prepared_frame()
        if q.where is not None:
            output = output[evaluate(output, q.where)]
        for order in q.orders:
            column = f"{order.field.table}.{order.field.name}"
            output = output.sort_values(column, ascending=order.ascending)
        if q.limit_count is not None:
            output = output.head(int(q.limit_count))
        output = output.copy()
        # 聚宽 run_query 返回裸字段名（code / a_registration_date / ...）。
        output = output.rename(
            columns={
                column: column.rsplit(".", 1)[-1]
                for column in output.columns
                if column.startswith(f"{_TABLE}.")
            }
        )
        return output.reset_index(drop=True)


__all__ = ["FinanceNamespace", "DEFAULT_ROOT"]
