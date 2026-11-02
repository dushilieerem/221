"""聚宽 jqfactor 描述因子的本地计算。

公式来源见 ``docs/功能包/FEAT-034-聚宽对账基线/聚宽风格因子清单.md``。
优先使用供应商已算好的 TTM / 增长率字段，避免利润表单季与累计口径混用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

from app.services.strategy.compat.financial_cache import load_statement, visible

DEFAULT_ROOT = Path(
    os.environ.get(
        "MODELSCOPE_ROOT",
        "/home/dev01/project/svquant-data/free/staging/modelscope",
    )
)


class FactorStore:
    """从 ModelScope 财务表计算 jqfactor 描述因子。"""

    def __init__(
        self,
        root: str | Path = DEFAULT_ROOT,
        *,
        data_engine=None,
    ) -> None:
        self.root = Path(root)
        self.fin = self.root / "3_financial_data"
        self.valuation = self.root / "5_technical_derived" / "valuation"
        self.data_engine = data_engine if data_engine is not None else _default_data_engine()
        self._industry_names: pd.Series | None = None
        self._valuation_cache: dict[tuple[str, str], pd.Series] = {}

    def compute(self, factor: str, symbols: list[str], as_of: str) -> pd.Series:
        handler = getattr(self, f"_factor_{factor}", None)
        if handler is None:
            raise NotImplementedError(f"jqfactor 描述因子暂未实现: {factor}")
        return handler(symbols, as_of).reindex(symbols)

    # ------------------------------------------------------------ 风格描述因子

    def _factor_earnings_growth(self, symbols: list[str], as_of: str) -> pd.Series:
        """过去 5 年 basic_eps 对 [0,1,2,3,4] 回归斜率 / EPS 绝对值均值。"""

        frame = self._annual_series("pershare_index", "s_fa_eps_basic", as_of)
        return self._growth_from_annual(frame)

    def _factor_sales_growth(self, symbols: list[str], as_of: str) -> pd.Series:
        """过去 5 年每股营业收入对 [0,1,2,3,4] 回归斜率 / 绝对值均值。"""

        income = self._annual_revenue(as_of)
        capital = self._annual_capital(as_of)
        merged = income.merge(capital, on=["Symbol", "year"], how="inner")
        merged["value"] = merged["annual_revenue"] / merged["total_capital"]
        result = self._growth_from_annual(merged[["Symbol", "year", "value"]])
        # 聚宽对银行业返回 nan（营收口径不适用）。
        banks = [symbol for symbol in result.index if self._is_bank(str(symbol))]
        if banks:
            result.loc[banks] = np.nan
        return result

    def _factor_DEGM(self, symbols: list[str], as_of: str) -> pd.Series:
        """毛利率 TTM 同比：最新 TTM 毛利率 / 去年同季 TTM 毛利率 - 1。"""

        rows = self._income_rows(as_of)
        result: dict[str, float] = {}
        for symbol, group in rows.groupby("Symbol"):
            if self._is_financial(str(symbol)):
                result[str(symbol)] = np.nan
                continue
            group = _prepare_periods(group)
            total_cost = pd.to_numeric(group["total_operating_cost"], errors="coerce")
            if total_cost.notna().sum() == 0 or float(total_cost.abs().max()) == 0:
                result[str(symbol)] = np.nan
                continue
            revenue = pd.to_numeric(group["revenue"], errors="coerce")
            cost = self._operating_cost_series(group)
            latest = str(group.index.max())
            prior = _prior_year_period(latest)
            revenue_ttm = _ttm_sum(revenue)
            prior_revenue_ttm = _ttm_sum(revenue, prior)
            cost_ttm = _ttm_at(cost)
            prior_cost_ttm = _ttm_at(cost, prior)
            if any(pd.isna(x) for x in (revenue_ttm, prior_revenue_ttm, cost_ttm, prior_cost_ttm)):
                result[str(symbol)] = np.nan
                continue
            if revenue_ttm == 0 or prior_revenue_ttm == 0:
                result[str(symbol)] = np.nan
                continue
            margin = 1.0 - float(cost_ttm) / float(revenue_ttm)
            prior_margin = 1.0 - float(prior_cost_ttm) / float(prior_revenue_ttm)
            result[str(symbol)] = (
                np.nan if prior_margin == 0 else margin / prior_margin - 1.0
            )
        return pd.Series(result, dtype=float)

    # ------------------------------------------------------------ 成长/规模

    def _factor_operating_revenue_growth_rate(self, symbols, as_of):
        return self._ttm_growth_from_per_share("revenue_ttm", as_of)

    def _factor_np_parent_company_owners_growth_rate(self, symbols, as_of):
        return self._ttm_growth_from_per_share("net_profit_parent_ttm", as_of)

    def _factor_total_profit_growth_rate(self, symbols, as_of):
        return self._ttm_growth_from_cumulative("income", "tot_profit", as_of)

    def _factor_sale_expense_ttm(self, symbols, as_of):
        return self._ttm_from_cumulative("income", "sale_expense", as_of)

    def _factor_np_parent_company_owners_ttm(self, symbols, as_of):
        return self._per_share("net_profit_parent_ttm", as_of)

    def _factor_non_recurring_gain_loss(self, symbols, as_of):
        # 聚宽口径：最新报告期的归母净利润 - 扣非净利润（单季度字段）。
        rows = self._income_rows(as_of)
        result: dict[str, float] = {}
        for symbol, group in rows.groupby("Symbol"):
            latest = group.sort_values("m_timetag").iloc[-1]
            net = pd.to_numeric(
                pd.Series([latest.get("net_profit_excl_min_int_inc")]), errors="coerce"
            ).iloc[0]
            deducted = pd.to_numeric(
                pd.Series([latest.get("deducted_net_profit")]), errors="coerce"
            ).iloc[0]
            result[str(symbol)] = (
                float(net) - float(deducted)
                if pd.notna(net) and pd.notna(deducted)
                else np.nan
            )
        return pd.Series(result, dtype=float)

    def _factor_net_operate_cash_flow_ttm(self, symbols, as_of):
        return self._per_share("operating_cf_ttm", as_of)

    def _factor_net_invest_cash_flow_ttm(self, symbols, as_of):
        return self._per_share("investing_cf_ttm", as_of)

    def _factor_total_operating_revenue_per_share_ttm(self, symbols, as_of):
        # per_share.revenue_ttm 是营业总收入 TTM（元），聚宽因子要求每股。
        return self._per_share("revenue_ttm", as_of) / self._total_shares(as_of)

    def _factor_operating_cost_ttm(self, symbols, as_of):
        # 营业成本 = 营业总成本 - 税金及附加 - 销售/管理/研发/财务费用 - 资产减值损失。
        rows = self._income_rows(as_of)
        result: dict[str, float] = {}
        for symbol, group in rows.groupby("Symbol"):
            if self._is_financial(str(symbol)):
                result[str(symbol)] = 0.0  # 银行/保险没有营业成本口径，聚宽返回 0
                continue
            group = _prepare_periods(group)
            total_cost = pd.to_numeric(group["total_operating_cost"], errors="coerce")
            if total_cost.notna().sum() == 0 or float(total_cost.abs().max()) == 0:
                result[str(symbol)] = 0.0
                continue
            cost = self._operating_cost_series(group)
            result[str(symbol)] = _ttm_at(cost)
        return pd.Series(result, dtype=float)

    def _factor_operating_profit_ttm(self, symbols, as_of):
        revenue = self._per_share("revenue_ttm", as_of)
        margin = self._ratio("operating_profit_margin", as_of)
        return revenue * margin / 100.0

    def _factor_EBIT(self, symbols, as_of):
        # 聚宽 EBIT 是单季度口径：净利润 + 所得税 + 财务费用（累计字段先差分成单季）。
        rows = self._income_rows(as_of)
        result: dict[str, float] = {}
        for symbol, group in rows.groupby("Symbol"):
            group = _prepare_periods(group)
            latest = str(group.index.max())
            net = _single_quarter(
                pd.to_numeric(group["net_profit_incl_min_int_inc"], errors="coerce"),
                latest,
            )
            tax = _single_quarter(
                pd.to_numeric(group["inc_tax"], errors="coerce"), latest
            )
            expense = _single_quarter(
                pd.to_numeric(group["financial_expense"], errors="coerce"), latest
            )
            # 银行等报表没有财务费用字段，按 0 处理；净利润或所得税缺失才返回 NaN。
            if pd.isna(expense):
                expense = 0.0
            if any(pd.isna(x) for x in (net, tax)):
                result[str(symbol)] = np.nan
            else:
                result[str(symbol)] = float(net) + float(tax) + float(expense)
        return pd.Series(result, dtype=float)

    def _industry(self) -> pd.Series:
        if self._industry_names is None:
            path = (
                self.root / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
            )
            if path.is_file():
                frame = pd.read_parquet(path, columns=["Symbol", "rs_hyname"])
                self._industry_names = frame.set_index("Symbol")["rs_hyname"]
            else:
                self._industry_names = pd.Series(dtype=str)
        return self._industry_names

    def _is_bank(self, symbol: str) -> bool:
        return "银行" in str(self._industry().get(symbol, ""))

    def _is_financial(self, symbol: str) -> bool:
        name = str(self._industry().get(symbol, ""))
        return "银行" in name or "保险" in name

    def _income_rows(self, as_of: str) -> pd.DataFrame:
        frame = visible(load_statement(self.root, "income"), as_of)
        return frame.sort_values(
            ["Symbol", "m_timetag"], kind="stable"
        ).reset_index(drop=True)

    def _operating_cost_series(self, group: pd.DataFrame) -> pd.Series:
        total = pd.to_numeric(group["total_operating_cost"], errors="coerce")
        cost = total.copy()
        for column in (
            "less_taxes_surcharges_ops",
            "sale_expense",
            "less_gerl_admin_exp",
            "research_expenses",
            "financial_expense",
            "less_impair_loss_assets",
        ):
            if column in group.columns:
                cost = cost - pd.to_numeric(group[column], errors="coerce").fillna(0.0)
        return cost.where(total.notna() & (total != 0), 0.0)

    def _factor_market_cap(self, symbols, as_of):
        # 聚宽 jqfactor 的 market_cap 为元；valuation.market_cap 才是亿元。
        return self._valuation_series("total_mv", as_of)

    # ------------------------------------------------------------ 价格类

    def _factor_sharpe_ratio_60(self, symbols, as_of):
        close = self._price_panel(as_of, symbols, "close", days=200)
        close = close.loc[: as_of].iloc[-61:]
        returns = close.pct_change().iloc[1:]
        # 聚宽用几何年化收益：(∏(1+r))^(252/n) - 1，n=60。
        annual_return = (1.0 + returns).prod() ** (252.0 / len(returns)) - 1.0
        annual_vol = returns.std() * np.sqrt(252.0)
        return (annual_return - 0.04) / annual_vol.replace(0.0, np.nan)

    def _factor_DAVOL20(self, symbols, as_of):
        turnover = self._price_panel(as_of, symbols, "turnover", days=320)
        turnover = turnover.loc[: as_of]
        short = turnover.iloc[-20:].mean()
        long = turnover.iloc[-120:].mean()
        return short / long.replace(0.0, np.nan)

    def _price_panel(
        self, as_of: str, symbols: list[str], field: str, *, days: int
    ) -> pd.DataFrame:
        if self.data_engine is None:
            raise NotImplementedError(
                "价格类因子需要 DataEngine；请传入 data_engine 或先装载行情"
            )
        end = pd.Timestamp(as_of)
        start = (end - pd.Timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
        panels = self.data_engine.get_task_panels(
            start,
            end.strftime("%Y-%m-%d"),
            fields=[field],
            symbols=list(symbols),
        )
        frame = panels[field]
        return frame.reindex(columns=pd.Index(symbols))

    # ------------------------------------------------------------ jqfactor 依赖

    #: jqfactor 依赖名 -> (财务表, 列名)。``_N`` 后缀表示往前第 N 个报告期。
    _DEPENDENCY_SOURCES: dict[str, tuple[str, str]] = {
        "roe": ("pershare_index", "equity_roe"),
        "roa": ("pershare_index", "roa"),
        "gross_profit_margin": ("pershare_index", "gross_margin"),
        "total_profit": ("income", "tot_profit"),
        "financial_expense": ("income", "financial_expense"),
        "operating_revenue": ("income", "revenue"),
        "total_current_assets": ("balance", "total_current_assets"),
        "total_non_current_assets": ("balance", "total_non_current_assets"),
        "total_current_liability": ("balance", "total_current_liability"),
        "total_non_current_liability": ("balance", "non_current_liabilities"),
    }

    def dependency_series(
        self, dependency: str, symbols: list[str], as_of: str
    ) -> pd.Series:
        """聚宽 ``Factor.dependencies`` 的 PIT 取值。"""

        return self.dependency_series_many([dependency], symbols, as_of)[dependency]

    def dependency_series_many(
        self, dependencies: list[str], symbols: list[str], as_of: str
    ) -> dict[str, pd.Series]:
        """一次算多个依赖；同一财务表只载入/排序一次。"""

        parsed: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for dependency in dependencies:
            base = dependency
            offset = 0
            match = re.fullmatch(r"(.+?)_(\d+)", dependency)
            if match is not None:
                base, offset = match.group(1), int(match.group(2))
            source = self._DEPENDENCY_SOURCES.get(base)
            if source is None:
                raise NotImplementedError(f"jqfactor 依赖暂未实现: {dependency}")
            parsed.setdefault(source, []).append((dependency, offset))

        result: dict[str, pd.Series] = {
            dependency: pd.Series(dtype=float) for dependency in dependencies
        }
        for (statement, column), items in parsed.items():
            frame = visible(load_statement(self.root, statement, [column]), as_of)
            if frame.empty:
                continue
            frame = (
                frame.drop_duplicates(["Symbol", "m_timetag"], keep="last")
                .sort_values(["Symbol", "m_timetag"])
                .copy()
            )
            frame["_rank"] = frame.groupby("Symbol").cumcount(ascending=False)
            for dependency, offset in items:
                picks = frame.loc[frame["_rank"] == offset]
                result[dependency] = (
                    picks.set_index("Symbol")[column]
                    .astype(float)
                    .reindex(symbols)
                )
        return result

    # ------------------------------------------------------------ 数据访问

    def _total_shares(self, as_of: str) -> pd.Series:
        """PIT 总股本（股），用于把每股指标还原成聚宽的总量因子。"""

        frame = self._latest_financial("capital", ["total_capital"], as_of)
        return frame.set_index("Symbol")["total_capital"].astype(float)

    def _per_share(self, column: str, as_of: str) -> pd.Series:
        return self._latest_financial("per_share", [column], as_of).set_index(
            "Symbol"
        )[column].astype(float)

    def _ratio(self, column: str, as_of: str) -> pd.Series:
        return self._latest_financial("pershare_index", [column], as_of).set_index(
            "Symbol"
        )[column].astype(float)

    def _latest_financial(
        self, statement: str, columns: list[str], as_of: str
    ) -> pd.DataFrame:
        frame = visible(load_statement(self.root, statement, columns), as_of)
        if frame.empty:
            return pd.DataFrame(columns=["Symbol", *columns])
        ranked = (
            frame.sort_values(
                ["Symbol", "m_timetag", "m_anntime"],
                ascending=[True, False, False],
                kind="stable",
            )
            .drop_duplicates("Symbol", keep="first")
        )
        return ranked[["Symbol", *columns]].reset_index(drop=True)

    def _valuation_series(self, column: str, as_of: str) -> pd.Series:
        if not self.valuation.is_dir():
            raise FileNotFoundError(f"估值数据未下载: {self.valuation}")
        key = as_of.replace("-", "")
        target = self.valuation / f"dt={key}" / "data.parquet"
        if not target.is_file():
            candidates = sorted(
                path
                for path in self.valuation.glob("dt=*/data.parquet")
                if path.parent.name.removeprefix("dt=") <= key
            )
            if not candidates:
                raise FileNotFoundError(f"估值分区为空或不早于 {as_of}")
            target = candidates[-1]
        cache_key = (str(target), column)
        cached = self._valuation_cache.get(cache_key)
        if cached is not None:
            return cached
        frame = pd.read_parquet(target, columns=["symbol", column])
        series = frame.set_index("symbol")[column].astype(float)
        self._valuation_cache[cache_key] = series
        return series

    def _annual_series(
        self, statement: str, column: str, as_of: str
    ) -> pd.DataFrame:
        frame = visible(load_statement(self.root, statement, [column]), as_of)
        annual = frame[frame["m_timetag"].astype(str).str.endswith("1231")]
        return pd.DataFrame({
            "Symbol": annual["Symbol"].to_numpy(),
            "year": annual["m_timetag"].astype(str).str[:4].to_numpy(),
            "value": annual[column].to_numpy(),
        })

    def _annual_revenue(self, as_of: str) -> pd.DataFrame:
        frame = visible(load_statement(self.root, "income", ["revenue"]), as_of)
        keyed = frame.assign(year=frame["m_timetag"].astype(str).str[:4])
        annual = keyed[
            keyed["m_timetag"].astype(str).str.endswith("1231")
        ][["Symbol", "year"]].drop_duplicates()
        quarterly = keyed.groupby(["Symbol", "year"], as_index=False)["revenue"].sum()
        merged = quarterly.merge(annual, on=["Symbol", "year"], how="inner")
        return merged.rename(columns={"revenue": "annual_revenue"})

    def _annual_capital(self, as_of: str) -> pd.DataFrame:
        frame = visible(load_statement(self.root, "capital", ["total_capital"]), as_of)
        keyed = frame.assign(year=frame["m_timetag"].astype(str).str[:4])
        ranked = (
            keyed.sort_values(
                ["Symbol", "year", "m_timetag", "m_anntime"],
                ascending=[True, True, False, False],
                kind="stable",
            )
            .drop_duplicates(["Symbol", "year"], keep="first")
        )
        return ranked[["Symbol", "year", "total_capital"]].reset_index(drop=True)

    def _ttm_from_cumulative(
        self, statement: str, column: str, as_of: str
    ) -> pd.Series:
        """累计字段的 TTM：最新累计 + 上年年报 - 上年同期累计。"""

        rows = self._period_rows(statement, column, as_of)
        if rows.empty:
            return pd.Series(dtype=float)
        wide = _wide_table(rows, column)
        # rows 已按 (Symbol, m_timetag) 排序，去重取最后一条就是最新报告期；
        # 用 groupby(...).max() 在 object 列上会退化到纯 Python 聚合。
        latest = rows.drop_duplicates("Symbol", keep="last").set_index("Symbol")[
            "m_timetag"
        ]
        periods = latest.to_numpy().astype(str)
        values = _ttm_values(wide, latest.index.to_numpy(), periods)
        return pd.Series(values, index=latest.index, dtype=float)

    def _ttm_growth_from_cumulative(
        self, statement: str, column: str, as_of: str
    ) -> pd.Series:
        rows = self._period_rows(statement, column, as_of)
        if rows.empty:
            return pd.Series(dtype=float)
        wide = _wide_table(rows, column)
        latest = rows.drop_duplicates("Symbol", keep="last").set_index("Symbol")[
            "m_timetag"
        ]
        symbols = latest.index.to_numpy()
        latest_periods = latest.to_numpy().astype(str)
        prior_periods = np.array(
            [_prior_year_period(period) for period in latest_periods]
        )
        latest_ttm = _ttm_values(wide, symbols, latest_periods)
        prior_ttm = _ttm_values(wide, symbols, prior_periods)
        with np.errstate(divide="ignore", invalid="ignore"):
            growth = latest_ttm / prior_ttm - 1.0
        growth[~np.isfinite(growth) | (prior_ttm == 0)] = np.nan
        return pd.Series(growth, index=latest.index, dtype=float)

    def _ttm_growth_from_per_share(self, column: str, as_of: str) -> pd.Series:
        rows = self._period_rows("per_share", column, as_of)
        if rows.empty:
            return pd.Series(dtype=float)
        wide = _wide_table(rows, column)
        latest = rows.drop_duplicates("Symbol", keep="last").set_index("Symbol")[
            "m_timetag"
        ]
        symbols = latest.index.to_numpy()
        latest_periods = latest.to_numpy().astype(str)
        prior_periods = np.array(
            [_prior_year_period(period) for period in latest_periods]
        )
        latest_values = _lookup(wide, symbols, latest_periods)
        prior_values = _lookup(wide, symbols, prior_periods)
        with np.errstate(divide="ignore", invalid="ignore"):
            growth = latest_values / prior_values - 1.0
        growth[~np.isfinite(growth) | (prior_values == 0)] = np.nan
        return pd.Series(growth, index=latest.index, dtype=float)

    def _period_rows(self, statement: str, column: str, as_of: str) -> pd.DataFrame:
        frame = visible(load_statement(self.root, statement, [column]), as_of)
        return (
            frame[["Symbol", "m_timetag", column]]
            .sort_values(["Symbol", "m_timetag"], kind="stable")
            .reset_index(drop=True)
        )

    def _growth_from_annual(
        self, frame: pd.DataFrame, *, value_column: str = "value"
    ) -> pd.Series:
        """最近 5 个年报值对 [0,1,2,3,4] 的回归斜率 / 绝对值均值。

        原实现逐标的调 ``np.polyfit``，5000 只标的一次重平衡要数秒；这里
        用最小二乘的闭式解向量化。
        """

        if frame.empty:
            return pd.Series(dtype=float)
        wide = frame.pivot_table(
            index="Symbol", columns="year", values=value_column, aggfunc="last"
        )
        if wide.empty:
            return pd.Series(dtype=float)
        arr = wide.to_numpy(dtype=float, copy=True)
        arr[~np.isfinite(arr)] = np.nan
        valid = ~np.isnan(arr)
        counts = valid.sum(axis=1)

        # 取每行最后 5 个有效值，左对齐到 5 列。
        order = np.argsort(~valid, axis=1, kind="stable")
        keep = np.minimum(counts, 5)
        offsets = np.arange(5)[None, :]
        columns = np.clip(counts[:, None] - keep[:, None] + offsets, 0, max(arr.shape[1] - 1, 0))
        picked = np.take_along_axis(arr, np.take_along_axis(order, columns, axis=1), axis=1)
        picked[offsets >= keep[:, None]] = np.nan

        positions = np.arange(5, dtype=float)[None, :]
        k = ~np.isnan(picked)
        counts_f = k.sum(axis=1).astype(float)
        safe_counts = np.where(counts_f > 0, counts_f, 1.0)
        sum_i = counts_f * (counts_f - 1.0) / 2.0
        sum_i2 = (counts_f - 1.0) * counts_f * (2.0 * counts_f - 1.0) / 6.0
        sum_y = np.nansum(picked, axis=1)
        sum_iy = np.nansum(np.where(k, picked * positions, np.nan), axis=1)
        denominator_var = sum_i2 - sum_i**2 / safe_counts
        mean_abs = np.nansum(np.abs(picked), axis=1) / safe_counts
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = (sum_iy - sum_i * sum_y / safe_counts) / denominator_var
            result = slope / mean_abs
        result[(counts_f < 4) | (mean_abs <= 0) | ~np.isfinite(result)] = np.nan
        return pd.Series(result, index=wide.index, dtype=float)


def _wide_table(rows: pd.DataFrame, column: str) -> pd.DataFrame:
    return rows.pivot_table(
        index="Symbol", columns="m_timetag", values=column, aggfunc="last"
    )


def _lookup(
    wide: pd.DataFrame, symbols: np.ndarray, periods: np.ndarray
) -> np.ndarray:
    row_index = wide.index.get_indexer(symbols)
    column_index = wide.columns.get_indexer(periods)
    values = wide.to_numpy(dtype=float)
    result = np.full(len(symbols), np.nan, dtype=float)
    valid = (row_index >= 0) & (column_index >= 0)
    result[valid] = values[row_index[valid], column_index[valid]]
    return result


def _ttm_values(
    wide: pd.DataFrame, symbols: np.ndarray, periods: np.ndarray
) -> np.ndarray:
    """累计字段在若干报告期的 TTM，向量化版 ``_ttm_at``。"""

    periods = np.asarray(periods, dtype=str)
    values = _lookup(wide, symbols, periods)
    years = np.array([int(period[:4]) for period in periods])
    month_days = np.array([period[4:] for period in periods])
    prior_annual = np.array([f"{year - 1}1231" for year in years])
    prior_same = np.array(
        [f"{year - 1}{month_day}" for year, month_day in zip(years, month_days)]
    )
    result = values + _lookup(wide, symbols, prior_annual) - _lookup(
        wide, symbols, prior_same
    )
    annual = month_days == "1231"
    result[annual] = values[annual]
    result[np.isnan(values)] = np.nan
    return result


def _prior_year_period(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}"


def _ttm_at(series: pd.Series, period: str | None = None) -> float:
    """累计字段在指定报告期的 TTM。"""

    if series.empty:
        return np.nan
    period = str(series.index.max()) if period is None else str(period)
    if period not in series.index:
        return np.nan
    value = series.loc[period]
    if pd.isna(value):
        return np.nan
    if period.endswith("1231"):
        return float(value)
    year = int(period[:4])
    prior_annual = f"{year - 1}1231"
    prior_same = _prior_year_period(period)
    if prior_annual not in series.index or prior_same not in series.index:
        return np.nan
    annual = series.loc[prior_annual]
    same = series.loc[prior_same]
    if pd.isna(annual) or pd.isna(same):
        return np.nan
    return float(value) + float(annual) - float(same)


def _default_data_engine():
    try:
        from app.services.data.engine import DataEngine

        engine = DataEngine.get_instance()
        return engine if getattr(engine, "provider", None) is not None else None
    except Exception:  # noqa: BLE001
        return None


def _prepare_periods(group: pd.DataFrame) -> pd.DataFrame:
    frame = group.sort_values("m_timetag").drop_duplicates("m_timetag", keep="last")
    return frame.set_index("m_timetag")


def _single_quarter(series: pd.Series, period: str) -> float:
    """累计字段在指定报告期的单季度值。"""

    if period not in series.index:
        return np.nan
    value = series.loc[period]
    if pd.isna(value):
        return np.nan
    month_day = period[4:]
    if month_day == "0331":
        return float(value)
    previous_map = {"0630": "0331", "0930": "0630", "1231": "0930"}
    previous = f"{period[:4]}{previous_map.get(month_day, '')}"
    if previous not in series.index:
        return np.nan
    previous_value = series.loc[previous]
    if pd.isna(previous_value):
        return np.nan
    return float(value) - float(previous_value)


def _ttm_sum(series: pd.Series, period: str | None = None) -> float:
    """单季度字段在指定报告期的 TTM（最后 4 个季度求和）。"""

    if series.empty:
        return np.nan
    frame = series.sort_index()
    if period is not None:
        frame = frame.loc[:period]
    if len(frame) < 4:
        return np.nan
    values = frame.iloc[-4:].astype(float)
    return np.nan if values.isna().any() else float(values.sum())
