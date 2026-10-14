"""FEAT-030 四引擎边界与时序契约测试。"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from app.models.factor_backtest import FactorBacktestSpec
from app.services.backtest.engine import BacktestEngine
from app.services.data.engine import DataEngine
from app.services.factor.base import BaseFactor
from app.services.factor.engine import FactorEngine
from app.services.factor.errors import FactorBacktestDataError
from app.services.portfolio.engine import PortfolioEngine
from app.services.metric.engine import MetricEngine


class _ResolvedFactor(BaseFactor):
    name = "resolved_factor"

    def compute(self, data):
        return data.close


def test_factor_engine_initializes_task_data_before_backtest_orchestration(monkeypatch):
    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    data_engine = Mock()
    data_engine.get_task_panels.return_value = {
        "close": pd.DataFrame({"A": [100.0, 101.0]}, index=dates)
    }
    data_engine.get_universe_at.return_value = ["A"]
    data_engine.build_filter_mask.return_value = {
        "mask": pd.DataFrame(True, index=dates, columns=["A"]),
        "status": {},
        "warnings": [],
    }
    monkeypatch.setattr(FactorEngine, "_resolve_factor", lambda *_args: None)
    spec = FactorBacktestSpec(
        factor_name="factor",
        start="2026-01-02",
        end=None,
    )
    monkeypatch.setattr(DataEngine, "_instance", data_engine)

    factor_engine = FactorEngine(spec)

    assert factor_engine.spec is spec
    assert factor_engine.actual_end == "2026-01-05"
    assert not hasattr(factor_engine, "effective_spec")
    assert not hasattr(factor_engine, "panel")
    assert factor_engine.market_field("close").equals(
        data_engine.get_task_panels.return_value["close"]
    )
    assert spec.end is None


def test_factor_engine_resolves_spec_and_stores_current_factor(monkeypatch):
    factor = _ResolvedFactor()
    calls: list[dict] = []

    def load_factor_from_spec(**kwargs):
        calls.append(kwargs)
        return factor

    monkeypatch.setattr(
        "app.services.factor.loader.load_factor_from_spec",
        load_factor_from_spec,
    )
    spec = FactorBacktestSpec(
        factor_name="resolved_factor",
        factor_code="def resolved_factor(self):\n    return self.close",
        library_names=("custom",),
    )

    engine = FactorEngine()
    panel = Mock()
    panel.close = pd.DataFrame({"A": [1.0]})
    values = engine.compute_from_spec(spec, panel)

    expected = panel.close.copy()
    expected.index.name = "date"
    pd.testing.assert_frame_equal(values, expected)
    assert engine.factor_metadata() == {
        "name": "resolved_factor",
        "author": "anonymous",
    }
    assert engine.factors["resolved_factor"] is factor
    assert calls and calls[0]["spec"] is spec
    assert calls[0]["factor_name"] == "resolved_factor"


def test_data_engine_filter_is_optional_and_reports_missing_state():
    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    close = pd.DataFrame({"A": [100.0, 110.0]}, index=dates)

    result = DataEngine().build_filter_mask(
        {"close": close},
        filter_suspended=True,
        filter_limit_up=True,
    )

    assert result["status"] == {
        "is_suspended": "skipped_missing_field",
        "is_st": "disabled",
        "is_limit_up": "skipped_missing_field",
        "is_limit_down": "disabled",
        # 退市剔除恒开启且没有开关；该面板没有 listed_status，按数据降级跳过。
        "is_delisted": "skipped_missing_field",
    }
    assert result["mask"].all().all()
    assert len(result["warnings"]) == 3

    partial = DataEngine().build_filter_mask(
        {
            "close": close,
            "up_and_down_status": pd.DataFrame(
                {"A": ["涨停", None]}, index=dates
            ),
        },
        filter_limit_up=True,
    )
    assert partial["status"]["is_limit_up"] == "enabled_partial"
    assert any("覆盖不足" in warning for warning in partial["warnings"])
    assert not bool(partial["mask"].loc[dates[0], "A"])
    assert bool(partial["mask"].loc[dates[1], "A"])


def test_data_engine_builds_status_masks_from_raw_status_text():
    dates = pd.date_range("2026-01-02", periods=4, freq="B")
    panels = {
        "close": pd.DataFrame(1.0, index=dates, columns=["A", "B"]),
        "listed_status": pd.DataFrame(
            [[" ST ", "在市"], ["*ST", "在市"], ["在市", "暂停上市"], [None, "在市"]],
            index=dates,
            columns=["A", "B"],
        ),
        "up_and_down_status": pd.DataFrame(
            [["涨停", "非涨跌停"], ["跌停", "涨停"], ["停牌", "跌停"], ["非涨跌停", None]],
            index=dates,
            columns=["A", "B"],
        ),
    }

    masks = DataEngine().build_status_masks(panels)

    assert bool(masks["is_st"].loc[dates[0], "A"])
    assert bool(masks["is_st"].loc[dates[1], "A"])
    assert not bool(masks["is_st"].loc[dates[2], "B"])
    assert bool(masks["is_limit_up"].loc[dates[0], "A"])
    assert bool(masks["is_limit_down"].loc[dates[1], "A"])
    assert bool(masks["is_limit"].loc[dates[1], "A"])
    assert bool(masks["is_suspended"].loc[dates[2], "A"])
    assert not bool(masks["is_suspended"].loc[dates[3], "B"])

    # is_delisted 只在白名单 {在市, ST, *ST} 上为 False（" ST " 需先去空格），
    # 暂停上市与缺失值都按不可投资处理。
    assert not bool(masks["is_delisted"].loc[dates[0], "A"])
    assert not bool(masks["is_delisted"].loc[dates[1], "A"])
    assert bool(masks["is_delisted"].loc[dates[2], "B"])  # 暂停上市
    assert bool(masks["is_delisted"].loc[dates[3], "A"])  # 缺失值
    assert not masks["is_delisted"].loc[:, "B"].iloc[:2].any()


def test_status_masks_treat_unknown_and_missing_status_as_delisted():
    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    panels = {
        "close": pd.DataFrame(1.0, index=dates, columns=["A", "B", "C", "D"]),
        "listed_status": pd.DataFrame(
            [
                ["在市", "终止上市", "退市整理", None],
                ["*ST", "暂停上市", "ST", "某个未来新增状态"],
            ],
            index=dates,
            columns=["A", "B", "C", "D"],
        ),
    }

    delisted = DataEngine().build_status_masks(panels)["is_delisted"]

    assert not delisted["A"].any()  # 在市 / *ST 属于白名单
    assert bool(delisted["B"].all())  # 终止上市 / 暂停上市
    assert bool(delisted["C"].iloc[0])  # 退市整理
    assert not bool(delisted["C"].iloc[1])  # ST 仍在白名单内，可投资
    assert bool(delisted["D"].all())  # 缺失值与未知取值一律不可投资


def test_delisted_exclusion_is_always_on_without_any_switch():
    dates = pd.date_range("2026-01-02", periods=3, freq="B")
    panels = {
        "close": pd.DataFrame(1.0, index=dates, columns=["A", "B"]),
        "listed_status": pd.DataFrame(
            {"A": ["在市"] * 3, "B": ["在市", "退市整理", "终止上市"]}, index=dates
        ),
    }

    # 四个可选开关全部关闭，退市剔除仍然生效 —— 这是本改动的核心契约。
    info = DataEngine().build_filter_mask(panels)

    assert info["status"]["is_delisted"] == "enabled"
    assert info["status"]["is_suspended"] == "disabled"
    assert bool(info["mask"].loc[dates[0], "B"])  # 点时：退市当日之前仍可用
    assert not bool(info["mask"].loc[dates[1], "B"])
    assert not bool(info["mask"].loc[dates[2], "B"])
    assert bool(info["mask"].loc[:, "A"].all())


def test_delisted_exclusion_never_passes_missing_cells_through():
    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    panels = {
        "close": pd.DataFrame(1.0, index=dates, columns=["A", "B"]),
        "listed_status": pd.DataFrame(
            {"A": ["在市", "在市"], "B": ["在市", None]}, index=dates
        ),
    }

    info = DataEngine().build_filter_mask(panels)

    # 缺失值必须按不可投资剔除，而不是走 apply_status_filter 的「按放行处理」。
    assert not bool(info["mask"].loc[dates[1], "B"])
    assert not any("按放行处理" in warning for warning in info["warnings"])
    # 单元格级缺失不告警：生产数据里那是标的上市前的空档，有数百万个，
    # 逐次告警只会淹没真正的信号。字段整体缺失才告警。
    assert not info["warnings"]
    assert info["status"]["is_delisted"] == "enabled"


def test_build_filter_mask_st_and_limit_down_use_their_own_source_fields():
    dates = pd.date_range("2026-01-02", periods=3, freq="B")
    close = pd.DataFrame({"A": [1.0] * 3, "B": [1.0] * 3}, index=dates)

    # 只有 listed_status：ST 过滤生效，跌停过滤因缺 up_and_down_status 被跳过。
    only_listed = DataEngine().build_filter_mask(
        {
            "close": close,
            "listed_status": pd.DataFrame(
                {"A": ["ST", "在市", "在市"], "B": ["在市"] * 3}, index=dates
            ),
        },
        filter_st=True,
        filter_limit_down=True,
    )
    assert only_listed["status"]["is_st"] == "enabled"
    assert only_listed["status"]["is_limit_down"] == "skipped_missing_field"
    assert not bool(only_listed["mask"].loc[dates[0], "A"])
    assert bool(only_listed["mask"].loc[dates[1], "A"])
    assert bool(only_listed["mask"].loc[dates[0], "B"])

    # 跌停状态缺失的单元格按放行处理，整体记为 enabled_partial。
    partial = DataEngine().build_filter_mask(
        {
            "close": close,
            "up_and_down_status": pd.DataFrame(
                {"A": [None, "跌停", "非涨跌停"], "B": ["非涨跌停"] * 3},
                index=dates,
            ),
        },
        filter_limit_down=True,
    )
    assert partial["status"]["is_limit_down"] == "enabled_partial"
    assert bool(partial["mask"].loc[dates[0], "A"])
    assert not bool(partial["mask"].loc[dates[1], "A"])
    assert bool(partial["mask"].loc[dates[2], "A"])


def test_delisted_mask_is_not_cached_without_release_identity():
    """没有 release 身份时不做缓存：同形状但内容不同的面板不能互相串味。"""

    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    close = pd.DataFrame({"A": [1.0, 1.0]}, index=dates)

    def mask_for(statuses):
        return DataEngine().build_filter_mask(
            {
                "close": close,
                "listed_status": pd.DataFrame({"A": statuses}, index=dates),
            }
        )["mask"]

    allowed = mask_for(["在市", "在市"])
    blocked = mask_for(["终止上市", "终止上市"])

    assert allowed.all().all()
    assert not blocked.any().any()


def test_factor_engine_reads_each_filter_flag_without_filters_enabled(monkeypatch):
    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    data_engine = Mock()
    data_engine.get_task_panels.return_value = {
        "close": pd.DataFrame({"A": [100.0, 101.0]}, index=dates)
    }
    data_engine.get_universe_at.return_value = ["A"]
    data_engine.build_filter_mask.return_value = {
        "mask": pd.DataFrame(True, index=dates, columns=["A"]),
        "status": {"is_st": "enabled"},
        "warnings": [],
    }
    monkeypatch.setattr(FactorEngine, "_resolve_factor", lambda *_args: None)
    monkeypatch.setattr(DataEngine, "_instance", data_engine)
    # 只开 ST 过滤、不设派生总开关 filters_enabled，掩码构造仍必须触发。
    spec = FactorBacktestSpec(
        factor_name="factor",
        start="2026-01-02",
        end=None,
        filter_st=True,
    )

    factor_engine = FactorEngine(spec)

    assert spec.filters_enabled is False
    assert data_engine.build_filter_mask.call_args.kwargs["filter_st"] is True
    assert factor_engine.eligibility_mask is not None


def test_factor_spec_identity_covers_all_filter_flags():
    base = {"factor_name": "factor", "start": "2026-01-02", "end": "2026-01-31"}
    variants = [
        FactorBacktestSpec(**base),
        FactorBacktestSpec(**base, filter_suspended=True),
        FactorBacktestSpec(**base, filter_st=True),
        FactorBacktestSpec(**base, filter_limit_up=True),
        FactorBacktestSpec(**base, filter_limit_down=True),
    ]

    # 每个开关都必须改变计算身份，否则会命中彼此的回测缓存。
    assert len({spec.identity() for spec in variants}) == len(variants)


def test_factor_engine_masks_before_cross_sectional_preprocessing():
    dates = pd.date_range("2026-01-02", periods=1)
    values = pd.DataFrame([[1.0, 2.0, 1000.0]], index=dates, columns=list("ABC"))
    eligible = pd.DataFrame([[True, True, False]], index=dates, columns=list("ABC"))

    processed = FactorEngine().preprocess(
        values,
        eligibility_mask=eligible,
        winsorize=2,
        standardize=1,
    )

    assert pd.isna(processed.loc[dates[0], "C"])
    assert math.isclose(processed.loc[dates[0], "A"], -1 / 2**0.5, abs_tol=1e-6)
    assert math.isclose(processed.loc[dates[0], "B"], 1 / 2**0.5, abs_tol=1e-6)


def test_portfolio_and_metric_preserve_first_forward_holding_period():
    dates = pd.date_range("2026-01-02", periods=4, freq="B")
    close = pd.DataFrame({"A": [100.0, 110.0, 121.0, 133.1]}, index=dates)
    factor = pd.DataFrame({"A": [1.0, 1.0, 1.0, 1.0]}, index=dates)

    portfolio = PortfolioEngine().build(
        factor,
        close,
        groups=None,
        portfolio_type="long_only",
        rebalance=1,
        stock_num=1,
    )
    evaluation = MetricEngine().evaluate(portfolio)

    pd.testing.assert_series_equal(
        evaluation["returns"],
        pd.Series([0.1, 0.1, 0.1], index=dates[1:], name=None),
    )
    pd.testing.assert_series_equal(
        evaluation["equity_curve"],
        pd.Series([1.0, 1.1, 1.21], index=dates[1:], name=None),
    )
    assert pd.isna(portfolio["forward_returns"].loc[dates[-1], "A"])
    assert portfolio["positions"].loc[dates[1], "A"] == 1.0


def test_portfolio_skips_rebalance_logs_by_default_and_supports_opt_in():
    dates = pd.date_range("2026-01-02", periods=3, freq="B")
    close = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 100.0, 100.0]},
        index=dates,
    )
    factor = pd.DataFrame(
        [[2.0, 1.0], [1.0, 2.0], [2.0, 1.0]],
        index=dates,
        columns=["A", "B"],
    )

    default_portfolio = PortfolioEngine().build(
        factor,
        close,
        groups=None,
        portfolio_type="long_only",
        rebalance=1,
        stock_num=1,
    )
    detailed_portfolio = PortfolioEngine().build(
        factor,
        close,
        groups=None,
        portfolio_type="long_only",
        rebalance=1,
        stock_num=1,
        include_rebalance_logs=True,
    )

    assert default_portfolio["rebalance_logs"] is None
    assert not detailed_portfolio["rebalance_logs"].empty


def test_metric_engine_owns_factor_analysis_and_benchmark_alignment():
    dates = pd.date_range("2026-01-02", periods=5, freq="B")
    symbols = list("ABCD")
    close = pd.DataFrame(
        {
            "A": [100.0, 101.0, 102.0, 103.0, 104.0],
            "B": [100.0, 102.0, 104.0, 106.0, 108.0],
            "C": [100.0, 99.0, 98.0, 97.0, 96.0],
            "D": [100.0, 98.0, 96.0, 94.0, 92.0],
        },
        index=dates,
    )
    factor = pd.DataFrame(
        [
            [4.0, 3.0, 2.0, 1.0],
            [4.0, 3.0, 2.0, 1.0],
            [1.0, 2.0, 3.0, 4.0],
            [1.0, 2.0, 3.0, 4.0],
            [4.0, 3.0, 2.0, 1.0],
        ],
        index=dates,
        columns=symbols,
    )
    portfolio = PortfolioEngine().build(
        factor,
        close,
        groups=2,
        portfolio_type="long_only",
        rebalance=1,
        stock_num=2,
    )
    benchmark = pd.Series(
        [1.0, 1.01, 1.02, 1.03],
        index=dates[1:],
    )
    loaded_dates = []

    def load_benchmark(index):
        loaded_dates.extend(index)
        return benchmark

    evaluation = MetricEngine().evaluate_factor_portfolio(
        portfolio,
        analysis_level="core",
        benchmark_loader=load_benchmark,
        groups=2,
    )

    assert loaded_dates == list(dates[1:])
    assert evaluation["benchmark_equity"] is benchmark
    assert "factor_analysis" in evaluation
    assert "long_short_returns" in evaluation["factor_analysis"]
    assert [item.quantile for item in evaluation["quantile_metrics"]] == [
        "Q1", "Q2", "benchmark"
    ]

    with pytest.raises(FactorBacktestDataError, match="指标评价未生成有效基准净值"):
        MetricEngine().evaluate_factor_portfolio(
            portfolio,
            analysis_level="core",
            benchmark_equity=pd.DataFrame(),
            groups=2,
        )


def test_portfolio_aligns_forward_returns_after_full_price_shift():
    dates = pd.date_range("2026-01-02", periods=4, freq="B")
    close = pd.DataFrame({"A": [100.0, 110.0, 121.0, 133.1]}, index=dates)
    factor = pd.DataFrame({"A": [1.0, 1.0, 1.0]}, index=dates[:3])

    portfolio = PortfolioEngine().build(
        factor,
        close,
        groups=None,
        portfolio_type="long_only",
        rebalance=1,
        stock_num=1,
    )

    assert math.isclose(
        portfolio["forward_returns"].loc[dates[2], "A"],
        0.1,
        abs_tol=1e-12,
    )


def test_metric_engine_reports_weight_change_cost_summary():
    dates = pd.date_range("2026-01-02", periods=3, freq="B")
    close = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=dates)
    factor = pd.DataFrame({"A": [1.0, 1.0, 1.0]}, index=dates)
    portfolio = PortfolioEngine().build(
        factor,
        close,
        groups=None,
        portfolio_type="long_only",
        rebalance=1,
        stock_num=1,
    )

    evaluation = MetricEngine().evaluate(
        portfolio,
        commission_rate=0.01,
        stamp_tax=0.002,
        slippage_rate=0.003,
    )

    summary = evaluation["transaction_summary"]
    assert summary["trade_count"] == 1
    assert summary["total_turnover"] == 1.0
    # 成本真正参与主组合收益：换手 1.0，佣金 1%、滑点 0.3%、本次无卖出故无印花税。
    assert summary["commission_cost"] == pytest.approx(0.01)
    assert summary["stamp_tax_cost"] == pytest.approx(0.0)
    assert summary["slippage_cost"] == pytest.approx(0.003)
    assert summary["total_cost"] == pytest.approx(0.013)
    assert evaluation["metrics"]["total_cost"] == pytest.approx(0.013)

    # 净收益 = 毛收益 − 当日成本：首个持有期承担 0.087（= 0.1 − 0.013）。
    returns = evaluation["returns"]
    assert returns.iloc[0] == pytest.approx(0.087)
    assert returns.iloc[1] == pytest.approx(0.1)


def test_metric_engine_costs_default_to_zero():
    """不传费率时与扣成本前的毛收益逐位一致——不能给既有回测悄悄加成本。"""

    dates = pd.date_range("2026-01-02", periods=3, freq="B")
    close = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=dates)
    factor = pd.DataFrame({"A": [1.0, 1.0, 1.0]}, index=dates)
    portfolio = PortfolioEngine().build(
        factor, close, groups=None, portfolio_type="long_only", rebalance=1, stock_num=1
    )

    evaluation = MetricEngine().evaluate(portfolio)

    summary = evaluation["transaction_summary"]
    assert summary["total_cost"] == pytest.approx(0.0)
    assert evaluation["returns"].iloc[0] == pytest.approx(0.1)
    assert evaluation["equity_curve"].iloc[-1] == pytest.approx(1.1)


def test_factor_path_does_not_reference_legacy_signal_algorithm():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    engine_source = (root / "app" / "services" / "backtest" / "engine.py").read_text(
        encoding="utf-8"
    )
    factor_source = (root / "app" / "services" / "experiment" / "factor.py").read_text(
        encoding="utf-8"
    )
    assert "FactorQuantileStrategy" not in engine_source
    assert "FactorGroupStrategy" not in engine_source
    assert "NativeEngine" not in engine_source
    assert "_FactorBacktestAlgorithm" not in factor_source
