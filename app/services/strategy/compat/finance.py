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
_INCOME_TABLE = "finance.STK_INCOME_STATEMENT"


class _DividendFields:
    code = Field(_TABLE, "code")
    a_registration_date = Field(_TABLE, "a_registration_date")
    bonus_amount_rmb = Field(_TABLE, "bonus_amount_rmb")


class _IncomeFields:
    """聚宽利润表 ``STK_INCOME_STATEMENT`` 的子集；金额为年内累计。"""

    code = Field(_INCOME_TABLE, "code")
    total_operating_revenue = Field(_INCOME_TABLE, "total_operating_revenue")
    operating_cost = Field(_INCOME_TABLE, "operating_cost")
    np_parent_company_owners = Field(_INCOME_TABLE, "np_parent_company_owners")
    sale_expense = Field(_INCOME_TABLE, "sale_expense")
    administration_expense = Field(_INCOME_TABLE, "administration_expense")
    financial_expense = Field(_INCOME_TABLE, "financial_expense")
    pub_date = Field(_INCOME_TABLE, "pub_date")
    end_date = Field(_INCOME_TABLE, "end_date")
    report_type = Field(_INCOME_TABLE, "report_type")


class FinanceNamespace:
    """聚宽 ``finance`` 命名空间；覆盖 ``STK_XR_XD`` 与 ``STK_INCOME_STATEMENT``。"""

    STK_XR_XD = _DividendFields()
    STK_INCOME_STATEMENT = _IncomeFields()

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self._prepared: pd.DataFrame | None = None
        self._income: pd.DataFrame | None = None

    def _prepared_frame(self) -> pd.DataFrame:
        """全量分红 + PIT 股本；与查询日期无关，进程内只算一次。"""

        if self._prepared is not None:
            return self._prepared
        dividends = load_statement(
            self.root,
            "dividend_factors",
            ["time", "registration_date", "interest", "stockBonus"],
        )
        if "registration_date" in dividends.columns:
            registration = pd.to_datetime(
                dividends["registration_date"], errors="coerce", format="mixed"
            ).astype("datetime64[ns]")
        else:
            # 旧数据集没有登记日列：沿用除权日近似，行为与之前一致。
            registration = None
        capital = load_statement(self.root, "capital", ["total_capital"])

        frame = dividends[["Symbol", "time", "interest", "stockBonus"]].copy()
        frame["time"] = pd.to_datetime(
            frame["time"], errors="coerce", format="mixed"
        ).astype("datetime64[ns]")
        frame = frame.dropna(subset=["time"]).sort_values(["time", "Symbol"])
        # 聚宽 STK_XR_XD.a_registration_date 是股权登记日，不是除权日。
        # 数据侧有 registration_date 时优先使用（匹配不到为 NaT 时回退除权日），
        # 否则保持旧行为。
        if registration is not None:
            frame["a_registration_date"] = registration.reindex(
                frame.index
            ).to_numpy()
        else:
            frame["a_registration_date"] = frame["time"].to_numpy()
        frame["a_registration_date"] = frame["a_registration_date"].where(
            frame["a_registration_date"].notna(), frame["time"]
        )

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
        # 送股/转增：派息按登记日股本计算，而股本表里新股本在送股公告时
        # 就已出现（早于派息记录日）。若选中的股本恰好是本次送股后的值
        # （比值≈1+送股/10），回退到送股前的股本，避免派息总额被放大
        # 1.5 倍、股息率失真（聚宽 bonus_amount_rmb 按登记日股本）。
        stock_bonus = pd.to_numeric(
            merged["stockBonus"], errors="coerce"
        ).fillna(0.0)
        adjusted = total_shares.copy()
        need_fix = stock_bonus > 0
        if need_fix.any():
            history = {
                symbol: list(zip(
                    group.sort_values("announce")["announce"].to_numpy(),
                    group.sort_values("announce")["total_capital"].to_numpy(dtype=float),
                ))
                for symbol, group in shares.groupby("Symbol")
            }
            for position in merged.index[need_fix]:
                symbol = merged.at[position, "Symbol"]
                current = float(total_shares.at[position])
                records = history.get(symbol)
                if records is None or not np.isfinite(current):
                    continue
                selected = merged.at[position, "announce"]
                previous = [
                    value for announce, value in records
                    if announce <= selected and value != current
                ]
                if not previous:
                    continue
                base = float(previous[-1])
                target = 1.0 + float(stock_bonus.at[position]) / 10.0
                if base > 0 and abs(current / base - target) <= 0.02:
                    adjusted.at[position] = base
        total_shares = adjusted
        bonus = cash_per_share * total_shares / 10000.0
        bonus = bonus.replace([np.inf, -np.inf], np.nan)
        # 预先把列名改成聚宽查询字段，后续每次查询直接布尔过滤，避免重复
        # 构造 20 万行的输出帧（dividend_query 会按 limit 做日期/代码二分）。
        prepared = pd.DataFrame({
            f"{_TABLE}.code": merged["Symbol"].to_numpy(),
            f"{_TABLE}.a_registration_date": merged["a_registration_date"].to_numpy(),
            f"{_TABLE}.bonus_amount_rmb": bonus.to_numpy(),
        })
        self._prepared = prepared
        return prepared

    def run_query(self, q: Query) -> pd.DataFrame:
        table = q.fields[0].table if q.fields else _TABLE
        if table == _INCOME_TABLE:
            return self._query_frame(q, self._income_frame())
        if table != _TABLE:
            raise NotImplementedError(f"兼容层暂不支持 finance 表: {table}")

        output = self._prepared_frame()
        if q.where is not None:
            output = output[evaluate(output, q.where)]
        for order in q.orders:
            column = f"{order.field.table}.{order.field.name}"
            output = output.sort_values(column, ascending=order.ascending)
        return self._slice(q, output, table)

    @staticmethod
    def _slice(q: Query, output: pd.DataFrame, table: str) -> pd.DataFrame:
        if q.offset_count:
            output = output.iloc[int(q.offset_count):]
        if q.limit_count is not None:
            output = output.head(int(q.limit_count))
        output = output.copy()
        # 聚宽 run_query 返回裸字段名（code / a_registration_date / ...）。
        return output.rename(
            columns={
                column: column.rsplit(".", 1)[-1]
                for column in output.columns
                if column.startswith(f"{table}.")
            }
        ).reset_index(drop=True)

    def _query_frame(self, q: Query, output: pd.DataFrame) -> pd.DataFrame:
        if q.where is not None:
            output = output[evaluate(output, q.where)]
        for order in q.orders:
            column = f"{order.field.table}.{order.field.name}"
            output = output.sort_values(column, ascending=order.ascending, kind="stable")
        return self._slice(q, output, _INCOME_TABLE)

    def _income_frame(self) -> pd.DataFrame:
        """利润表历史快照；``revenue`` 与归母净利润是单季度，这里按年内累加。"""

        if self._income is not None:
            return self._income
        columns = [
            "revenue",
            "total_operating_cost",
            "less_taxes_surcharges_ops",
            "sale_expense",
            "less_gerl_admin_exp",
            "research_expenses",
            "financial_expense",
            "less_impair_loss_assets",
            "net_profit_excl_min_int_inc",
        ]
        raw = load_statement(self.root, "income", columns).copy()
        period = raw["m_timetag"].astype(str)
        year = period.str[:4]
        revenue = pd.to_numeric(raw["revenue"], errors="coerce")
        parent = pd.to_numeric(raw["net_profit_excl_min_int_inc"], errors="coerce")
        revenue_cum = revenue.groupby([raw["Symbol"], year]).cumsum()
        parent_cum = parent.groupby([raw["Symbol"], year]).cumsum()
        expense = sum(
            pd.to_numeric(raw[column], errors="coerce").fillna(0.0)
            for column in (
                "less_taxes_surcharges_ops",
                "sale_expense",
                "less_gerl_admin_exp",
                "research_expenses",
                "financial_expense",
                "less_impair_loss_assets",
            )
        )
        frame = pd.DataFrame(
            {
                f"{_INCOME_TABLE}.code": raw["Symbol"].to_numpy(),
                f"{_INCOME_TABLE}.total_operating_revenue": revenue_cum.to_numpy(dtype=float),
                f"{_INCOME_TABLE}.operating_cost": (
                    pd.to_numeric(raw["total_operating_cost"], errors="coerce") - expense
                ).to_numpy(dtype=float),
                f"{_INCOME_TABLE}.np_parent_company_owners": parent_cum.to_numpy(dtype=float),
                f"{_INCOME_TABLE}.sale_expense": pd.to_numeric(
                    raw["sale_expense"], errors="coerce"
                ).to_numpy(dtype=float),
                f"{_INCOME_TABLE}.administration_expense": pd.to_numeric(
                    raw["less_gerl_admin_exp"], errors="coerce"
                ).to_numpy(dtype=float),
                f"{_INCOME_TABLE}.financial_expense": pd.to_numeric(
                    raw["financial_expense"], errors="coerce"
                ).to_numpy(dtype=float),
                f"{_INCOME_TABLE}.pub_date": pd.to_datetime(
                    raw["m_anntime"].astype(str), format="%Y%m%d", errors="coerce"
                ),
                f"{_INCOME_TABLE}.end_date": pd.to_datetime(
                    period, format="%Y%m%d", errors="coerce"
                ),
                f"{_INCOME_TABLE}.report_type": 0,
            }
        )
        self._income = frame
        return frame


__all__ = ["FinanceNamespace", "DEFAULT_ROOT"]
