"""``MetricEngine`` - 指标统一入口。

依据 ``docs/99-历史归档/FEAT-001-QuantCore基础能力/04-class-design.md`` MetricEngine 节与 ``docs/99-历史归档/FEAT-001-QuantCore基础能力/05-runtime-flow.md`` 第 5 节：

    属性：
      metrics: Dict[str, BaseMetric]

    方法：
      register(metric: BaseMetric)
      calculate(metric_names: List[str], data) -> Dict[str, float]
      calculate_all(data) -> Dict[str, float]

运行流程：遍历请求的指标名 -> 取出指标 -> ``metric.calculate(data)`` -> 收集结果。

**关键原则**：Backtest 不算 Sharpe，Factor 不算 IC，全部集中在此。
Research 只调用 ``metric.calculate(...)``。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import logging
import math
from typing import Any, Dict

import numpy as np
import pandas as pd

from app.services.factor.errors import (
    FactorBacktestDataError,
    FactorBacktestEmptyResult,
)
from app.services.metric.base import BaseMetric
from app.services.metric.factor_analysis import (
    _compute_quantile_metrics,
    _daily_ic,
    _industry_equity,
)
from app.models.metric import MetricContext
from app.services.metric.metrics.standard import default_metrics
from app.services.portfolio.returns import (
    GrossReturnBundle,
    calculate_group_returns,
    calculate_group_turnover,
    calculate_gross_returns,
)


_LOGGER = logging.getLogger(__name__)


class MetricEngine:
    """所有指标的统一入口。

    引擎只管理三件事：指标注册、指标查询和批量执行。具体指标仍由
    :class:`BaseMetric` 实现，结果保持原有的 ``dict[str, float]`` 契约。
    通过把单指标执行收敛到 ``calculate_one``，可以让 API、策略回测和因子
    回测共用同一套名称解析与错误行为。
    """

    def __init__(
        self,
        register_defaults: bool = True,
        *,
        metrics: Iterable[BaseMetric] | Mapping[str, BaseMetric] | None = None,
    ) -> None:
        self.metrics: Dict[str, BaseMetric] = {}
        if register_defaults:
            self.register_many(default_metrics().values())
        if metrics is not None:
            self.register_many(metrics)

    def register(self, metric: BaseMetric) -> None:
        """注册指标。"""
        if not isinstance(metric, BaseMetric):
            raise TypeError(
                f"只能注册 BaseMetric 子类，得到 {type(metric).__name__}"
            )
        self.metrics[metric.name] = metric

    def register_many(
        self,
        metrics: Iterable[BaseMetric] | Mapping[str, BaseMetric],
    ) -> None:
        """批量注册指标；映射的 key 仅用于兼容输入，最终以 ``metric.name`` 为准。"""

        values = metrics.values() if isinstance(metrics, Mapping) else metrics
        for metric in values:
            self.register(metric)

    @property
    def names(self) -> tuple[str, ...]:
        """当前已注册指标名（只读快照）。"""

        return tuple(self.metrics)

    def get_metric(self, name: str) -> BaseMetric:
        """按名称取得指标，统一生成可读的未注册错误。"""

        try:
            return self.metrics[name]
        except KeyError as exc:
            raise KeyError(
                f"未注册指标 {name!r}，已注册: {list(self.metrics)}"
            ) from exc

    def calculate_one(self, name: str, ctx: MetricContext) -> float:
        """计算单个指标。"""

        metric = self.get_metric(name)
        return float(metric.calculate(ctx))

    def calculate(
        self,
        metric_names: Iterable[str] | str,
        ctx: MetricContext,
    ) -> Dict[str, float]:
        """计算指定指标。"""
        result: Dict[str, float] = {}
        # ``str`` 也是 Iterable，但调用方传入单个指标名时应按一个指标处理，
        # 不能被拆成字符逐个查找。
        names = (metric_names,) if isinstance(metric_names, str) else metric_names
        for name in names:
            result[name] = self.calculate_one(name, ctx)
        return result

    def calculate_all(self, ctx: MetricContext) -> Dict[str, float]:
        """计算全部已注册指标。"""
        return self.calculate(self.metrics, ctx)

    @staticmethod
    def _portfolio_returns(
        close: pd.DataFrame,
        positions: pd.DataFrame,
        *,
        commission_rate: float = 0.0,
        stamp_tax: float = 0.0,
        slippage_rate: float = 0.0,
    ) -> pd.Series:
        """由持有权重计算逐期收益；保留首个持有期收益。"""

        details = MetricEngine._portfolio_return_details(
            close,
            positions,
            commission_rate=commission_rate,
            stamp_tax=stamp_tax,
            slippage_rate=slippage_rate,
        )
        return details["net_returns"].iloc[1:].dropna()

    @staticmethod
    def _portfolio_return_details(
        close: pd.DataFrame,
        positions: pd.DataFrame,
        *,
        commission_rate: float = 0.0,
        stamp_tax: float = 0.0,
        slippage_rate: float = 0.0,
    ) -> dict[str, Any]:
        """返回收益及按权重变化拆分的交易成本明细。

        因子回测没有真实成交量和成交价，成本以归一化组合权重变化表示。
        因此 ``trade_count`` 只能表示调仓意图中的标的权重变化次数，不能
        冒充事件引擎中的真实成交笔数。
        """

        prices = close.reindex(index=positions.index, columns=positions.columns)
        returns = prices.pct_change(fill_method=None)
        # PortfolioEngine 已将目标权重转换成完整日历上的持有权重：日期 t
        # 的 positions 承担 t-1 收盘到 t 收盘收益。这里不能再次 shift，
        # 否则会把首个持有期错误地延后一日。
        held = positions.ffill().fillna(0.0)
        portfolio_returns = (held * returns).sum(axis=1)
        change = held.diff().fillna(0.0)
        costs = MetricEngine._trading_costs(
            change,
            commission_rate=commission_rate,
            stamp_tax=stamp_tax,
            slippage_rate=slippage_rate,
        )
        return {
            "gross_returns": portfolio_returns,
            "net_returns": portfolio_returns - costs["total"],
            "held": held,
            "weight_change": change,
            "turnover": change.abs().sum(axis=1),
            "sell_turnover": (-change.clip(upper=0.0)).sum(axis=1),
            "costs": costs,
        }

    @staticmethod
    def _execution_forward_returns(
        forward_returns: pd.DataFrame,
        close: pd.DataFrame,
        open_prices: pd.DataFrame | None,
        execution_timing: str,
    ) -> pd.DataFrame:
        """远期收益矩阵，按成交时点平移，供行业曲线与行业 IC 使用。

        行业分析把「信号日的因子值」与「该信号实际能拿到的持仓期收益」配对：
        默认（``same_close``）配对 ``(t, t+1]``，``next_close`` 换成
        ``(t+1, t+2]``，``next_open`` 换成次日的开盘到收盘。远期收益的期数由
        ``forward_return_days`` 决定（当前固定为 1），因此三种口径的持仓期
        长度一致，只是起点随成交时点后移。

        IC / RankIC、行业 IC 与行业曲线都用它，全链路保持同一成交口径。
        """

        if execution_timing == "same_close":
            return forward_returns
        if execution_timing == "next_close":
            return forward_returns.shift(-1)
        if execution_timing == "next_open":
            if open_prices is None:
                raise ValueError(
                    "execution_timing='next_open' 需要开盘价，但评测层没有收到 "
                    "open 面板"
                )
            prices_open = open_prices.reindex(
                index=close.index, columns=close.columns
            )
            intraday = close / prices_open.replace(0.0, float("nan")) - 1.0
            return intraday.reindex(
                index=forward_returns.index, columns=forward_returns.columns
            ).shift(-1)
        raise ValueError(
            f"不支持的 execution_timing: {execution_timing!r}，"
            "可选 same_close | next_close | next_open"
        )

    @staticmethod
    def _layered_returns(
        asset_returns: pd.DataFrame,
        group_assignments: pd.DataFrame,
        rebalance_mask: pd.Series,
        n_groups: int,
        *,
        execution_timing: str,
        close: pd.DataFrame,
        open_prices: pd.DataFrame | None,
    ) -> pd.DataFrame:
        """分层曲线与主组合用**同一成交口径**，便于直接对比分组单调性。

        口径映射与主组合一致：

        * ``same_close``：分组在信号日收盘成交、次日生效（``lag=1``），
          收益用收盘价对收盘价；
        * ``next_close``：晚一天生效（``lag=2``），收益仍是收盘价对收盘价；
        * ``next_open``：持仓 ``lag=1`` 生效，但当日收益要拆成两段——
          隔夜跳空按**上一个**分组（``lag=2``）承担、日内按**当前**分组
          （``lag=1``）承担。

        仍是不含交易成本的毛收益（成本只作用于主组合）。
        """

        if execution_timing == "next_open":
            if open_prices is None:
                raise ValueError(
                    "execution_timing='next_open' 需要开盘价，但评测层没有收到 "
                    "open 面板"
                )
            prices_open = open_prices.reindex(
                index=close.index, columns=close.columns
            )
            gap = prices_open / close.shift(1) - 1.0
            intraday = close / prices_open.replace(0.0, float("nan")) - 1.0
            return calculate_group_returns(
                intraday, group_assignments, rebalance_mask, n_groups, lag=1
            ) + calculate_group_returns(
                gap, group_assignments, rebalance_mask, n_groups, lag=2
            )
        if execution_timing not in {"same_close", "next_close"}:
            raise ValueError(
                f"不支持的 execution_timing: {execution_timing!r}，"
                "可选 same_close | next_close | next_open"
            )
        return calculate_group_returns(
            asset_returns,
            group_assignments,
            rebalance_mask,
            n_groups,
            lag=2 if execution_timing == "next_close" else 1,
        )

    @staticmethod
    def _next_open_returns(
        held: pd.DataFrame,
        close: pd.DataFrame,
        open_prices: pd.DataFrame | None,
    ) -> pd.Series:
        """信号次日开盘成交时主组合的逐期收益。

        当日收益拆成两段：隔夜跳空由**昨日**持仓承担（开盘才换仓），日内由
        **今日**持仓承担。权重不变时两段相加退化为收盘价对收盘价，只差二阶
        交叉项；建仓当日只赚日内、清仓当日只赚跳空，都与真实成交一致。

        ``held`` 已是「第 t 日实际持有」的权重（next_open 下由
        ``PortfolioEngine.build`` 用 ``shift(1)`` 产出），位移与「信号日收盘
        成交」一致，差别只在这条公式上。
        """

        if open_prices is None:
            raise ValueError(
                "execution_timing='next_open' 需要开盘价，但评测层没有收到 "
                "open 面板"
            )
        prices_open = open_prices.reindex(index=close.index, columns=close.columns)
        overnight_gap = prices_open / close.shift(1) - 1.0
        intraday = close / prices_open.replace(0.0, float("nan")) - 1.0
        return (held.shift(1).fillna(0.0) * overnight_gap).sum(axis=1) + (
            held * intraday
        ).sum(axis=1)

    @staticmethod
    def _trading_costs(
        weight_change: pd.DataFrame,
        *,
        commission_rate: float = 0.0,
        stamp_tax: float = 0.0,
        slippage_rate: float = 0.0,
    ) -> pd.DataFrame:
        """按持有权重变化拆分的逐期交易成本。

        因子回测没有真实成交量与成交价，成本以归一化组合权重变化表示：
        佣金与滑点按 ``|Δ权重|`` 计，印花税只对卖出（权重下降）部分计。
        返回的每一列都是「占组合净值的比例」，不是货币金额。

        首个持有期的建仓成本不计入：``weight_change`` 的首行被填为 0，
        与向量化策略引擎的口径一致。
        """

        turnover = weight_change.abs().sum(axis=1)
        sell_turnover = (-weight_change.clip(upper=0.0)).sum(axis=1)
        costs = pd.DataFrame(
            {
                "commission": turnover * float(commission_rate),
                "stamp_tax": sell_turnover * float(stamp_tax),
                "slippage": turnover * float(slippage_rate),
            },
            index=weight_change.index,
        )
        costs["total"] = costs.sum(axis=1)
        return costs

    @staticmethod
    def _display_equity(returns: pd.Series) -> pd.Series:
        if returns is None or returns.empty:
            return pd.Series(dtype=float)
        displayed = returns.copy()
        displayed.iloc[0] = 0.0
        return (1.0 + displayed).cumprod()

    @staticmethod
    def _empty_factor_statistics(reason: str) -> dict[str, Any]:
        """IC 不可用时的空统计。

        ``unavailable_reason`` 区分「因子/数据的性质」与「取数或配置问题」，
        让调用方不必靠 ``ic_series`` 为空去猜是哪一种。取值：
          missing_inputs                因子值或远期收益缺失
          no_common_axis                两者没有共同日期或标的
          insufficient_cross_section    没有任何交易日满足 >= 2 个有效样本
          zero_cross_sectional_variance 有样本但逐日相关系数全不可计算
        """
        return {
            "ic_series": pd.DataFrame(columns=["value", "sample_size"]),
            "rank_ic_series": pd.Series(dtype=float),
            "summary": {
                "ic_mean": None,
                "rank_ic_mean": None,
                "ic_std": None,
                "ic_ir": None,
                "valid_days": 0,
                "actual_start": None,
                "actual_end": None,
            },
            "unavailable_reason": reason,
        }

    @staticmethod
    def _rowwise_corr(left: pd.DataFrame, right: pd.DataFrame) -> pd.Series:
        """用矩阵归约计算逐行 Pearson 相关，避免 ``corrwith`` 的行级开销。"""

        left_values = left.to_numpy(dtype=float, na_value=np.nan)
        right_values = right.to_numpy(dtype=float, na_value=np.nan)
        valid = np.isfinite(left_values) & np.isfinite(right_values)
        count = valid.sum(axis=1, dtype=float)
        safe_count = np.maximum(count, 1.0)
        left_values = np.where(valid, left_values, 0.0)
        right_values = np.where(valid, right_values, 0.0)
        left_sum = left_values.sum(axis=1)
        right_sum = right_values.sum(axis=1)
        left_sq_sum = np.square(left_values).sum(axis=1)
        right_sq_sum = np.square(right_values).sum(axis=1)
        cross_sum = (left_values * right_values).sum(axis=1)
        covariance = cross_sum - left_sum * right_sum / safe_count
        left_variance = left_sq_sum - np.square(left_sum) / safe_count
        right_variance = right_sq_sum - np.square(right_sum) / safe_count
        denominator = np.sqrt(
            np.maximum(left_variance, 0.0)
            * np.maximum(right_variance, 0.0)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            correlations = covariance / denominator
        correlations[(count < 2) | (denominator <= 0.0)] = np.nan
        return pd.Series(correlations, index=left.index, dtype=float)

    @staticmethod
    def _factor_statistics(
        factor_values: pd.DataFrame | None,
        forward_returns: pd.DataFrame | None,
        periods_per_year: int,
    ) -> dict[str, Any]:
        """一次对齐并计算因子 IC、RankIC 及其汇总。"""

        if factor_values is None or forward_returns is None:
            return MetricEngine._empty_factor_statistics("missing_inputs")
        dates = factor_values.index.intersection(forward_returns.index)
        symbols = factor_values.columns.intersection(forward_returns.columns)
        if dates.empty or symbols.empty:
            return MetricEngine._empty_factor_statistics("no_common_axis")

        factor = factor_values.loc[dates, symbols]
        future = forward_returns.loc[dates, symbols]
        valid = factor.notna() & future.notna()
        sample_size = valid.sum(axis=1)
        enough = sample_size >= 2
        factor = factor.loc[enough]
        future = future.loc[enough]
        sample_size = sample_size.loc[enough]
        if factor.empty:
            return MetricEngine._empty_factor_statistics(
                "insufficient_cross_section"
            )

        ic = MetricEngine._rowwise_corr(factor, future)
        rank_ic = MetricEngine._rowwise_corr(
            factor.rank(axis=1),
            future.rank(axis=1),
        ).dropna()
        ic_series = pd.DataFrame({
            "value": ic,
            "sample_size": sample_size,
        }).dropna(subset=["value"])
        ic_series = ic_series[ic_series["sample_size"] >= 2]
        if ic_series.empty:
            return MetricEngine._empty_factor_statistics(
                "zero_cross_sectional_variance"
            )

        values = ic_series["value"]
        mean = float(values.mean())
        std = float(values.std(ddof=1)) if len(values) > 1 else None
        ir = None if std is None else (
            0.0 if std == 0 else mean / std * (periods_per_year ** 0.5)
        )
        return {
            "ic_series": ic_series,
            "rank_ic_series": rank_ic,
            "summary": {
                "ic_mean": mean,
                "rank_ic_mean": (
                    float(rank_ic.mean()) if not rank_ic.empty else None
                ),
                "ic_std": std,
                "ic_ir": ir,
                "valid_days": int(len(values)),
                "actual_start": pd.Timestamp(values.index.min()).date().isoformat(),
                "actual_end": pd.Timestamp(values.index.max()).date().isoformat(),
            },
            "unavailable_reason": None,
        }

    @staticmethod
    def _calculate_factor_metrics(
        returns: pd.Series,
        equity: pd.Series,
        positions: pd.DataFrame,
        factor_statistics: Mapping[str, Any],
        periods_per_year: int,
    ) -> dict[str, float]:
        """批量计算因子回测内置指标，避免逐指标重复扫描宽表。"""

        nan = float("nan")
        if returns is None or returns.empty:
            metrics = {
                name: nan
                for name in (
                    "annual_return", "sharpe", "sortino", "calmar",
                    "max_drawdown", "volatility", "ic", "rank_ic",
                    "win_rate", "turnover",
                )
            }
        else:
            mean = float(returns.mean())
            std = float(returns.std())
            downside = np.minimum(returns.to_numpy(dtype=float), 0.0)
            downside_std = float(np.sqrt(np.mean(downside ** 2)))
            annual_return = mean * periods_per_year
            sharpe = 0.0 if math.isnan(std) or std == 0 else (
                mean / std * math.sqrt(periods_per_year)
            )
            sortino = (
                0.0 if mean == 0 else float("inf")
            ) if downside_std == 0 else mean / downside_std * math.sqrt(periods_per_year)
            running_max = equity.cummax()
            drawdown = (equity - running_max) / running_max
            max_drawdown = float(drawdown.min())
            calmar = (
                nan if math.isnan(annual_return) or math.isnan(max_drawdown)
                or max_drawdown == 0
                else annual_return / abs(max_drawdown)
            )
            position_diff = positions.diff().abs().iloc[1:]
            turnover = (
                float((position_diff.sum(axis=1) / 2.0).mean())
                if len(positions) >= 2 else nan
            )
            metrics = {
                "annual_return": annual_return,
                "sharpe": sharpe,
                "sortino": float(sortino),
                "calmar": float(calmar),
                "max_drawdown": max_drawdown,
                "volatility": std * math.sqrt(periods_per_year),
                "ic": float(factor_statistics["summary"]["ic_mean"])
                if factor_statistics["summary"]["ic_mean"] is not None else nan,
                "rank_ic": float(factor_statistics["summary"]["rank_ic_mean"])
                if factor_statistics["summary"]["rank_ic_mean"] is not None else nan,
                "win_rate": float((returns > 0).mean()),
                "turnover": turnover,
            }
        if returns is None or returns.empty:
            summary = factor_statistics["summary"]
            metrics["ic"] = (
                float(summary["ic_mean"]) if summary["ic_mean"] is not None else nan
            )
            metrics["rank_ic"] = (
                float(summary["rank_ic_mean"])
                if summary["rank_ic_mean"] is not None else nan
            )
        return metrics

    @staticmethod
    def _factor_quantile_metrics(
        analysis: Mapping[str, Any],
        benchmark_equity: pd.Series | None,
        periods_per_year: int,
    ) -> tuple[Any, ...]:
        """计算分层组合相对基准的完整指标。"""

        layered_equity = analysis.get("layered_equity")
        if not isinstance(layered_equity, pd.DataFrame) or layered_equity.empty:
            return ()
        if benchmark_equity is None or benchmark_equity.empty:
            return ()

        layered_returns = analysis.get("layered_returns")
        relative = benchmark_equity.pct_change(fill_method=None).reindex(
            layered_returns.index
            if isinstance(layered_returns, pd.DataFrame)
            else benchmark_equity.index
        )
        # 分层收益保留首个入场/持有期样本，而基准没有对应的信号收益。
        if (
            isinstance(layered_returns, pd.DataFrame)
            and not layered_returns.empty
            and not relative.empty
        ):
            relative.iloc[0] = 0.0
        return tuple(_compute_quantile_metrics(
            layered_equity,
            benchmark_equity,
            periods_per_year=periods_per_year,
            layered_returns=(
                layered_returns
                if isinstance(layered_returns, pd.DataFrame)
                else None
            ),
            relative_benchmark_returns=relative,
            group_turnover=analysis.get("group_turnover"),
        ))

    @staticmethod
    def _merge_ff3_metrics(
        metrics: dict[str, float],
        analysis: Mapping[str, Any],
    ) -> None:
        """把 FF3 回归标量并入统一指标映射。"""

        ff3 = analysis.get("ff3_regression")
        if not isinstance(ff3, Mapping):
            return
        try:
            alpha = float(ff3.get("alpha"))
        except (TypeError, ValueError):
            return
        if ff3.get("n_obs", 0) <= 0 or math.isnan(alpha):
            return
        for name, key in (
            ("ff3_alpha", "alpha"),
            ("ff3_beta_mkt", "beta_mkt"),
            ("ff3_beta_smb", "beta_smb"),
            ("ff3_beta_hml", "beta_hml"),
            ("ff3_r_squared", "r_squared"),
        ):
            if key in ff3:
                metrics[name] = float(ff3[key])

    def _factor_analysis(
        self,
        factor_values: pd.DataFrame,
        future: pd.DataFrame,
        portfolio: Mapping[str, Any],
        evaluation: Mapping[str, Any],
        *,
        analysis_level: str,
        industry_map: pd.Series | None = None,
        market_fields: Mapping[str, pd.DataFrame] | None = None,
        rebalance: str | int = 1,
        groups: int | None = 5,
    ) -> dict[str, Any]:
        """计算因子回测的 core/full 分析，不读取市场数据。

        ``future`` 是调用方传进来的**按成交时点平移过**的远期收益矩阵，
        IC/行业 IC/行业曲线据此与本口径的一致。
        """

        if analysis_level not in {"core", "full"}:
            return {}

        out: dict[str, Any] = {
            "layered_returns": evaluation["layered_returns"],
            "layered_equity": evaluation["layered_equity"],
            "predictive_summary": evaluation["predictive_summary"],
            # IC 不可用的原因（可用时为 None）；core 与 full 都带回。
            "ic_unavailable_reason": evaluation.get("ic_unavailable_reason"),
            "group_turnover": evaluation.get("group_turnover", pd.DataFrame()),
        }
        if analysis_level == "full":
            # evaluate_factor_portfolio 已经完成一次因子/远期收益对齐和 IC
            # 计算；full 只复用结果，不再重新扫描日期×标的矩阵。
            ic_series = evaluation.get("ic_series")
            if not isinstance(ic_series, pd.DataFrame):
                ic_series = _daily_ic(factor_values, future)
            out["ic_series"] = ic_series
            out["cumulative_ic"] = ic_series["value"].cumsum()
            rolling = ic_series["value"].rolling(60, min_periods=60)
            out["rolling_ir"] = (rolling.mean() / rolling.std(ddof=1)).dropna()

        layered = evaluation["layered_returns"]
        group_count = int(groups or 5)
        if not layered.empty and "Q1" in layered and f"Q{group_count}" in layered:
            out["long_short_returns"] = layered["Q1"] - layered[f"Q{group_count}"]
            out["long_short_equity"] = 1.0 + out["long_short_returns"].cumsum()

        if analysis_level != "full":
            return out

        if industry_map is not None:
            try:
                from app.services.factor.riskmodel import industry_ic_ir

                out["industry_ic_ir"] = industry_ic_ir(
                    factor_values, future, industry_map
                )
                out["industry_equity"], out["industry_group_equity"] = _industry_equity(
                    factor_values, future, industry_map, group_count
                )
            except Exception as exc:  # noqa: BLE001 - 辅助分析失败不阻断主流程
                _LOGGER.warning("行业因子分析失败，已跳过: %s", exc, exc_info=True)

        fields = market_fields or {}
        current_mv = fields.get("current_mv")
        if current_mv is None:
            return out
        try:
            from app.services.factor.riskmodel import (
                ff3_factors,
                ff3_regression,
                market_weighted_returns,
            )

            close = portfolio["close"]
            market_ret = market_weighted_returns(
                close,
                current_mv,
                rebalance=rebalance,
            )
            out["benchmark_equity"] = 1.0 + market_ret.cumsum()
            pb_mrq = fields.get("pb_mrq")
            if pb_mrq is not None and "long_short_returns" in out:
                ff3 = ff3_factors(
                    close,
                    current_mv,
                    pb_mrq,
                    rebalance=rebalance,
                    n_groups=group_count,
                )
                out["ff3"] = ff3
                out["ff3_regression"] = ff3_regression(
                    out["long_short_returns"], ff3
                )
        except Exception as exc:  # noqa: BLE001 - 辅助分析失败不阻断主流程
            _LOGGER.warning("FF3 因子分析失败，已跳过: %s", exc, exc_info=True)
        return out

    def evaluate_factor_portfolio(
        self,
        portfolio: Mapping[str, Any],
        *,
        commission_rate: float = 0.0,
        stamp_tax: float = 0.0,
        slippage_rate: float = 0.0,
        execution_timing: str = "next_close",
        open_prices: pd.DataFrame | None = None,
        periods_per_year: int = 252,
        analysis_level: str | None = None,
        benchmark_equity: pd.Series | None = None,
        benchmark_loader: Callable[[pd.DatetimeIndex], pd.Series] | None = None,
        industry_map: pd.Series | None = None,
        market_fields: Mapping[str, pd.DataFrame] | None = None,
        rebalance: str | int = 1,
        groups: int | None = 5,
    ) -> dict[str, Any]:
        """评价 PortfolioEngine 产物并返回普通 Python 映射。

        这里是因子回测的评价入口；不创建信号对象，也不依赖应用层 schema。
        ``calculate_all`` 仍保留给策略和通用指标调用方。

        当传入 ``analysis_level`` 时，本方法同时完成因子回测的基准、分层、
        IC、行业、FF3 和分位数评价。基准由 ``benchmark_loader`` 延迟提供，
        这样调用方可以在主组合净值生成后按相同日期对齐，而 MetricEngine
        不需要直接读取文件或依赖具体 Provider。
        """

        close = portfolio["close"]
        positions = portfolio["positions"]
        factor_values = portfolio.get("factor_values")
        forward_returns = portfolio.get("forward_returns")
        # 全部基于远期收益的统计（IC / RankIC、行业 IC、行业曲线）统一改用
        # 「按成交时点平移」后的矩阵，保证口径与主组合一致。FF3 用的是收盘价
        # 而非远期收益，不受影响。
        if forward_returns is not None:
            forward_returns = self._execution_forward_returns(
                forward_returns, close, open_prices, execution_timing
            )
        return_bundle: GrossReturnBundle = calculate_gross_returns(close, positions)
        # 主组合按请求的费率扣减交易成本：佣金与滑点按 |Δ权重| 换手计，
        # 印花税只对卖出部分计。净值、收益、指标因此都是净口径。
        #
        # 成交时点决定用哪条收益公式：next_close 沿用收盘价对收盘价（持仓
        # 位移已由 PortfolioEngine 按口径调好），next_open 走两段式（隔夜
        # 跳空由昨日持仓承担、日内由今日持仓承担）并需要开盘价。
        #
        # 分层曲线与由它派生的多空价差仍是毛收益、且沿用收盘价口径：它们
        # 衡量的是因子的排序能力，属于诊断口径。这一分工写进了 meta 的
        # cost_model 与 execution_model。
        costs = self._trading_costs(
            return_bundle.weight_change,
            commission_rate=commission_rate,
            stamp_tax=stamp_tax,
            slippage_rate=slippage_rate,
        )
        portfolio_returns = return_bundle.portfolio_returns
        if execution_timing == "next_open":
            portfolio_returns = self._next_open_returns(
                return_bundle.held_weights, close, open_prices
            )
        elif execution_timing not in {"same_close", "next_close"}:
            raise ValueError(
                f"不支持的 execution_timing: {execution_timing!r}，"
                "可选 same_close | next_close | next_open"
            )
        returns = (portfolio_returns - costs["total"]).iloc[1:].dropna()
        equity = self._display_equity(returns)
        positions_out = return_bundle.held_weights.reindex(returns.index)
        factor_statistics = self._factor_statistics(
            factor_values,
            forward_returns,
            int(periods_per_year),
        )
        ctx = MetricContext(
            equity_curve=equity,
            returns=returns,
            factor_values=factor_values,
            forward_returns=forward_returns,
            positions=positions_out,
            close=close,
            target_weights=portfolio.get("target_weights"),
            periods_per_year=int(periods_per_year),
        )
        metrics = self._calculate_factor_metrics(
            returns,
            equity,
            positions_out,
            factor_statistics,
            int(periods_per_year),
        )
        change = return_bundle.weight_change
        transaction_summary = {
            # 这是按标的权重变化计数的调仓意图，不是真实成交笔数。
            "trade_count": int((change.abs().to_numpy() > 0).sum()),
            "total_turnover": float(return_bundle.turnover.sum()),
            # 成本口径为「占组合净值的比例」，与 total_turnover 同为全区间累加，
            # 不是货币金额（因子回测的净值从 1.0 起算，没有真实资金规模）。
            "commission_cost": float(costs["commission"].sum()),
            "stamp_tax_cost": float(costs["stamp_tax"].sum()),
            "slippage_cost": float(costs["slippage"].sum()),
            "total_cost": float(costs["total"].sum()),
        }
        # 这些附加标量只属于完整因子评价入口；通用 calculate_all() 的
        # 默认十项指标保持不变，避免影响策略回测的旧注册契约。
        metrics.update({
            name: float(value)
            for name, value in transaction_summary.items()
            if name != "trade_count"
        })
        metrics["trade_count"] = float(transaction_summary["trade_count"])

        group_count = portfolio.get("group_count")
        group_turnover = pd.DataFrame()
        if group_count is None:
            layered_returns = pd.DataFrame()
        else:
            layered_returns = self._layered_returns(
                return_bundle.asset_returns,
                portfolio["group_assignments"],
                portfolio["rebalance_mask"],
                int(group_count),
                execution_timing=execution_timing,
                close=close,
                open_prices=open_prices,
            ).reindex(returns.index)
            # 逐分位单边换手，与分层曲线共用同一份「持有分组」映射（含 lag），
            # 口径对齐主组合 metrics.turnover：逐日 Σ|Δw|/2 后取均值。
            group_turnover = calculate_group_turnover(
                returns.index,
                layered_returns.columns,
                portfolio["group_assignments"],
                portfolio["rebalance_mask"],
                int(group_count),
                lag=2 if execution_timing == "next_close" else 1,
            )
        layered_equity = layered_returns.copy()
        if not layered_equity.empty:
            layered_equity.iloc[0] = 0.0
            layered_equity = (1.0 + layered_equity).cumprod()

        predictive = factor_statistics["summary"]
        result = {
            "metrics": metrics,
            "returns": returns,
            "equity_curve": equity,
            "positions": positions_out,
            "layered_returns": layered_returns,
            "layered_equity": layered_equity,
            "predictive_summary": predictive,
            "transaction_summary": transaction_summary,
            "context": ctx,
            "ic_series": factor_statistics["ic_series"],
            "ic_unavailable_reason": factor_statistics.get("unavailable_reason"),
            "group_turnover": group_turnover,
        }
        if analysis_level is not None:
            if equity.empty:
                raise FactorBacktestEmptyResult("回测未生成有效净值")
            if benchmark_equity is None and benchmark_loader is not None:
                benchmark_equity = benchmark_loader(equity.index)
            if not isinstance(benchmark_equity, pd.Series) or benchmark_equity.empty:
                raise FactorBacktestDataError("指标评价未生成有效基准净值")
            factor_analysis = self._factor_analysis(
                factor_values,
                forward_returns,
                portfolio,
                result,
                analysis_level=analysis_level,
                industry_map=industry_map,
                market_fields=market_fields,
                rebalance=rebalance,
                groups=groups,
            )
            self._merge_ff3_metrics(metrics, factor_analysis)
            result.update({
                "benchmark_equity": benchmark_equity,
                "factor_analysis": factor_analysis,
                "quantile_metrics": self._factor_quantile_metrics(
                    factor_analysis,
                    benchmark_equity,
                    periods_per_year,
                ),
            })
        return result

    def evaluate(self, portfolio: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        """因子回测完整评价入口的简短别名。"""

        return self.evaluate_factor_portfolio(portfolio, **kwargs)

    @staticmethod
    def _predictive_summary(
        factor_values: pd.DataFrame | None,
        forward_returns: pd.DataFrame | None,
        periods_per_year: int,
    ) -> dict[str, Any]:
        return MetricEngine._factor_statistics(
            factor_values,
            forward_returns,
            periods_per_year,
        )["summary"]
