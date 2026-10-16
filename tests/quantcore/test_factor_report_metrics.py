"""ROC 对比发现的报告指标回归：经过评价入口验证，而非只测底层函数。"""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from app.models.factor_backtest import FactorBacktestSpec
from app.services.metric.engine import MetricEngine
from app.services.metric.factor_analysis import _compute_quantile_metrics
from app.services.portfolio.engine import PortfolioEngine


@pytest.mark.parametrize("timing, expected", [
    ("same_close", [0.5, 0.0, 1.0, 0.0]),
    ("next_open", [0.5, 0.0, 1.0, 0.0]),
    ("next_close", [0.0, 0.5, 0.0, 1.0]),
])
def test_evaluation_preserves_stock_axis_and_first_signal_for_turnover(timing, expected):
    dates = pd.bdate_range("2025-01-02", periods=5)
    close = pd.DataFrame(100.0, index=dates, columns=list("ABCD"))
    factor = pd.DataFrame(
        [[4, 3, 2, 1], [4, 3, 2, 1], [1, 2, 3, 4],
         [1, 2, 3, 4], [1, 2, 3, 4]],
        index=dates, columns=close.columns, dtype=float,
    )
    portfolio = PortfolioEngine().build(
        factor, close, groups=2, execution_timing=timing, rebalance=1,
    )
    result = MetricEngine().evaluate_factor_portfolio(
        portfolio, groups=2, execution_timing=timing, open_prices=close,
        analysis_level="core", benchmark_equity=pd.Series(1.0, index=dates[1:]),
    )
    turnover = result["group_turnover"]
    pd.testing.assert_index_equal(turnover.index, result["layered_returns"].index)
    for group in ["Q1", "Q2"]:
        assert turnover[group].tolist() == pytest.approx(expected)
    for metric in result["quantile_metrics"][:-1]:
        assert metric.turnover == pytest.approx(np.mean(expected))


def test_benchmark_win_rate_compares_daily_returns_not_cumulative_returns():
    dates = pd.bdate_range("2025-01-02", periods=4)
    returns = pd.DataFrame({"Q1": [0.0, 0.02, 0.02, 0.02]}, index=dates)
    benchmark = pd.Series([1.0, 1.01, 1.0201, 1.030301], index=dates)
    metric = _compute_quantile_metrics((1 + returns).cumprod(), benchmark,
                                       layered_returns=returns)[0]
    # 首日平局不算赢，之后3天都是2% > 1%；旧实现只认出1天，返回25%。
    assert metric.benchmark_win_rate == pytest.approx(0.75)


def test_benchmark_win_rate_uses_common_finite_relative_samples_and_strict_win():
    dates = pd.bdate_range("2025-01-02", periods=5)
    returns = pd.DataFrame({"Q1": [0.03, 0.02, 0.01, np.nan, 0.04]}, index=dates)
    benchmark = pd.Series(1.0, index=dates)
    relative = pd.Series([0.02, 0.02, 0.02, 0.0, np.inf], index=dates)
    metric = _compute_quantile_metrics(
        (1 + returns.fillna(0)).cumprod(), benchmark,
        layered_returns=returns, relative_benchmark_returns=relative,
    )[0]
    # 有效共同样本3天：胜、平、负；缺失和无穷值不进入分母。
    assert metric.benchmark_win_rate == pytest.approx(1 / 3)
    no_common = _compute_quantile_metrics(
        (1 + returns.fillna(0)).cumprod(), benchmark, layered_returns=returns,
        relative_benchmark_returns=pd.Series(dtype=float),
    )[0]
    assert no_common.benchmark_win_rate is None


def test_report_metric_revision_changes_computation_identity():
    spec = FactorBacktestSpec(factor_name="roc")
    assert spec.metric_version != "factor-scorecard-v3"
    assert spec.identity() != replace(spec, metric_version="factor-scorecard-v3").identity()
