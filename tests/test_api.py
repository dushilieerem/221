"""app 因子回测 API 测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.config as config_module
from app.config import (
    configure_runtime_settings,
    default_runtime_settings,
    get_runtime_settings,
    reset_runtime_settings,
)
from app.main import app
from app.services.data.engine import DataEngine
from app.services.factor.market_data import _market_symbol, _panel_symbol
from app.services.factor.metrics import _compute_quantile_metrics
from app.services.factor.result_serializers import (
    _display_instrument,
    _sample_positions,
    _serialize_rebalance_logs,
)
from app.services.portfolio.grouping import rank_groups


client = TestClient(app)


@pytest.fixture(autouse=True)
def _restore_runtime_settings():
    previous = config_module._runtime_settings
    yield
    if previous is None:
        reset_runtime_settings()
    else:
        configure_runtime_settings(previous)

COMBO_FACTOR_CODE = '''def volume_20d(self, window: int = 20):
    return self.volume / self.volume.rolling(window).mean()

def amount_20d(self, window: int = 20):
    return self.amount / self.amount.rolling(window).mean()

def combo_factor(self):
    return 0.5 * self.volume_20d() + 0.5 * self.amount_20d()'''

BUILTIN_COMBO_FACTOR_CODE = '''def combo_factor(self):
    return 0.5 * self.bbi() + 0.5 * self.alpha158_kmid()'''

STRATEGY_CODE = '''import pandas as pd

class EqualWeightMomentum:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        top_n = 3
        momentum = self.close.pct_change(20)
        ranks = momentum.rank(axis=1, ascending=False, method="first")
        weights = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        weights[ranks <= top_n] = 1.0 / top_n
        return weights'''

STRUCTURED_STRATEGY_CODE = 'from app.services.strategy.vectorized.factor_strategy import EqualWeight, EveryNTradingDays, FactorPortfolioStrategy, TopN, factor, signal\n\nclass CompositeFactorStrategy(FactorPortfolioStrategy):\n    selector = TopN(3)\n    weighting = EqualWeight()\n    rebalance = EveryNTradingDays(15)\n\n    @factor\n    def momentum_20d(self):\n        return self.close.pct_change(20)\n\n    @factor\n    def value_ep(self):\n        return 1.0 / self.pe_ttm\n\n    @signal\n    def score(self):\n        return 0.6 * self.rank(self.momentum_20d()) + 0.4 * self.rank(self.value_ep())'


@pytest.fixture()
def market_data_dir(tmp_path, monkeypatch):
    """生成最小行情和 CSI300 基准数据。"""
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2022-01-03", periods=90)
    symbols = [f"S{i:03d}" for i in range(12)]
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (90, 12)), axis=0))
    for name, values in (
        ("open", close * 0.995),
        ("high", close * 1.01),
        ("low", close * 0.99),
        ("close", close),
        ("volume", rng.uniform(1e6, 1e7, (90, 12))),
        ("amount", rng.uniform(1e8, 1e9, (90, 12))),
        ("current_mv", rng.uniform(1e10, 5e10, (90, 12))),
    ):
        frame = pd.DataFrame(values, index=dates, columns=symbols)
        frame.index.name = "datetime"
        frame.reset_index().to_parquet(tmp_path / f"{name}.parquet", index=False)

    status = pd.DataFrame("非涨跌停", index=dates, columns=symbols)
    status.index.name = "datetime"
    status.reset_index().to_parquet(tmp_path / "up_and_down_status.parquet", index=False)

    # 退市剔除恒开启，行情 fixture 必须提供 listed_status，否则退市过滤会
    # 退化成 skipped_missing_field，主路径得不到覆盖。默认全部「在市」。
    listed = pd.DataFrame("在市", index=dates, columns=symbols)
    listed.index.name = "datetime"
    listed.reset_index().to_parquet(tmp_path / "listed_status.parquet", index=False)

    display_dir = tmp_path / "display"
    display_dir.mkdir()
    display_columns = pd.MultiIndex.from_product(
        [symbols, ["open", "high", "low", "close"]]
    )
    display = pd.DataFrame(index=dates, columns=display_columns, dtype=float)
    for index, symbol in enumerate(symbols):
        # 与研究行情刻意使用不同数值，确保接口确实读取展示专用文件。
        display[(symbol, "open")] = close[:, index] + 10.0
        display[(symbol, "high")] = close[:, index] + 12.0
        display[(symbol, "low")] = close[:, index] + 8.0
        display[(symbol, "close")] = close[:, index] + 11.0
    display.index.name = "date"
    display.to_parquet(display_dir / "unadjusted_price.parquet")

    pd.DataFrame({
        "code": symbols,
        "name": [f"股票{i:03d}" for i in range(12)],
        "industry": ["银行"] * 6 + ["医药"] * 6,
    }).to_parquet(tmp_path / "stock_info.parquet", index=False)

    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    benchmark_close = np.linspace(4000.0, 4300.0, len(dates))
    benchmark = pd.DataFrame(
        {
            "open": benchmark_close * 0.995,
            "high": benchmark_close * 1.01,
            "low": benchmark_close * 0.99,
            "close": benchmark_close,
        },
        index=dates,
    )
    benchmark.to_parquet(benchmark_dir / "index_price_000300.SH.parquet")
    pd.DataFrame(
        {symbol: [1 / len(symbols)] for symbol in symbols}, index=[dates[0]]
    ).to_parquet(benchmark_dir / "index_weights_000905.SH.parquet")
    pd.DataFrame(
        {
            symbol: [1 / len(symbols), 0.5 if symbol in {"S000", "S001"} else float("nan")]
            for symbol in symbols
        },
        index=[dates[0], dates[-1]],
    ).to_parquet(benchmark_dir / "index_weights_000300.SH.parquet")
    configure_runtime_settings(
        replace(
            default_runtime_settings(),
            data_dir=tmp_path,
            cache_dir=tmp_path / "backtest_cache",
        )
    )
    data_engine = DataEngine()
    data_engine.load_parquet(tmp_path)
    monkeypatch.setattr(DataEngine, "_instance", data_engine)
    monkeypatch.setattr(app.state, "data_engine", data_engine)
    yield tmp_path
    data_engine.close()


@pytest.fixture()
def supermind_api_data_dir(tmp_path, monkeypatch):
    """将已验证的 SuperMind fixture 转为 API 可读取的字段 Parquet。"""
    fixture_dir = (
        Path(__file__).resolve().parent
        / "quantcore"
        / "fixtures"
        / "supermind"
    )
    market_cap = pd.read_csv(fixture_dir / "market_cap.csv", index_col=0)
    factor_dates = pd.to_datetime(
        [str(value).split(" ")[0] for value in market_cap.columns],
        format="%Y/%m/%d",
    )
    factor_values = market_cap.T
    factor_values.index = factor_dates
    factor_values.columns = market_cap.index.astype(str)

    close = pd.read_csv(fixture_dir / "price.csv", index_col=0)
    close.index = pd.to_datetime(close.index, format="%Y/%m/%d")
    close = close.loc[:, factor_values.columns]

    def write_field(name, frame):
        frame = frame.copy()
        frame.index.name = "datetime"
        frame.reset_index().to_parquet(tmp_path / f"{name}.parquet", index=False)

    for field in ("close", "current_mv"):
        write_field(field, close if field == "close" else factor_values)

    display_dir = tmp_path / "display"
    display_dir.mkdir()
    display_columns = pd.MultiIndex.from_product(
        [close.columns, ["open", "high", "low", "close"]]
    )
    display = pd.DataFrame(index=close.index, columns=display_columns, dtype=float)
    for symbol in close.columns:
        for field in ("open", "high", "low", "close"):
            display[(symbol, field)] = close[symbol]
    display.index.name = "date"
    display.to_parquet(display_dir / "unadjusted_price.parquet")

    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    benchmark_close = close.mean(axis=1)
    benchmark = pd.DataFrame(
        {
            "open": benchmark_close,
            "high": benchmark_close,
            "low": benchmark_close,
            "close": benchmark_close,
        },
        index=benchmark_close.index,
    )
    benchmark.to_parquet(benchmark_dir / "index_price_000300.SH.parquet")

    configure_runtime_settings(
        replace(
            default_runtime_settings(),
            data_dir=tmp_path,
            cache_dir=tmp_path / "backtest_cache",
            cache_enabled=False,
        )
    )
    data_engine = DataEngine()
    data_engine.load_parquet(tmp_path)
    monkeypatch.setattr(DataEngine, "_instance", data_engine)
    monkeypatch.setattr(app.state, "data_engine", data_engine)
    yield {
        "root": tmp_path,
        "factor_values": factor_values,
        "close": close,
        "benchmark_close": benchmark_close,
    }
    data_engine.close()


def _request_payload():
    return {
        "factor_code": COMBO_FACTOR_CODE,
        "factor_name": "combo_factor",
        "universe": "CSI500",
        "portfolio_type": "long_only",
        "rebalance_days": 15,
        "groups": 3,
        "benchmark": "CSI300",
        "analysis_level": "full",
    }


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_factor_backtest_minimal_builtin_request_uses_web_defaults():
    from app.schemas.backtest import FactorBacktestRequest

    request = FactorBacktestRequest(
        library_name="momentum_lib",
        factor_name="momentum_5d",
    )

    assert request.portfolio_type == "long_only"
    assert request.universe == "CSI300"
    assert request.rebalance_days == 1
    assert request.commission_rate == 0.0
    assert request.stamp_tax == 0.0
    assert request.slippage_rate == 0.0


def test_daily_bars_returns_filtered_ohlc(market_data_dir):
    response = client.get(
        "/api/market-data/daily/S000",
        params={"start": "2022-01-05", "end": "2022-01-07"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "S000"
    assert body["name"] == "股票000"
    assert len(body["bars"]) == 3
    assert body["date_range"] == {"start": "2022-01-05", "end": "2022-01-07"}
    assert set(body["bars"][0]) == {"date", "open", "high", "low", "close"}
    assert body["bars"][0]["open"] == pytest.approx(body["bars"][0]["close"] - 1.0)


def test_market_data_symbols_returns_sorted_stock_codes(market_data_dir):
    response = client.get("/api/market-data/symbols")

    assert response.status_code == 200
    assert response.json() == {
        "symbols": [
            {"symbol": f"S{i:03d}", "name": f"股票{i:03d}"}
            for i in range(12)
        ],
        "unmapped_count": 0,
    }


def _wait_factor_task(payload: dict, *, timeout: float = 30.0):
    submission = client.post("/api/backtest/factor", json=payload)
    assert submission.status_code == 202
    submission_body = submission.json()
    assert submission.headers["Location"] == submission_body["poll_url"]
    assert submission.headers["Retry-After"] == "1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(submission_body["poll_url"])
        assert response.status_code == 200
        if response.json()["status"] in {"succeeded", "failed", "cancelled"}:
            return response
        time.sleep(0.02)
    raise AssertionError(f"因子回测任务超时: {submission_body['run_id']}")


def test_market_data_indices_returns_available_indices(market_data_dir):
    response = client.get("/api/market-data/indices")

    assert response.status_code == 200
    assert response.json() == {
        "indices": [{"code": "000300.SH", "name": "沪深300"}],
    }


def test_index_constituents_returns_latest_snapshot(market_data_dir):
    response = client.get("/api/market-data/indices/000300.SH/constituents")

    assert response.status_code == 200
    assert response.json() == {
        "index": {"code": "000300.SH", "name": "沪深300"},
        "constituents": [
            {"symbol": "S000", "name": "股票000"},
            {"symbol": "S001", "name": "股票001"},
        ],
        "unmapped_count": 0,
    }


def test_index_constituents_rejects_unknown_code(market_data_dir):
    assert client.get("/api/market-data/indices/000001.SH/constituents").status_code == 404


def test_market_symbol_conversion():
    assert _market_symbol("000001") == "000001.SZ"
    assert _market_symbol("600519") == "600519.SH"
    assert _market_symbol("430047") == "430047.BJ"
    assert _market_symbol("920000") == "920000.BJ"
    assert _panel_symbol("000001.SZ") == "000001"


def test_daily_bars_returns_not_found_or_invalid_range(market_data_dir):
    assert client.get("/api/market-data/daily/UNKNOWN").status_code == 404
    assert client.get(
        "/api/market-data/daily/S000",
        params={"start": "2022-01-07", "end": "2022-01-05"},
    ).status_code == 422


def test_daily_bars_returns_service_unavailable_when_field_missing(market_data_dir):
    path = market_data_dir / "display" / "unadjusted_price.parquet"
    display = pd.read_parquet(path)
    display = display.loc[:, display.columns.get_level_values(1) != "high"]
    display.to_parquet(path)
    response = client.get("/api/market-data/daily/S000")
    assert response.status_code == 503


def test_factor_backtest_groups_and_benchmark(market_data_dir):
    response = _wait_factor_task(_request_payload())
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "succeeded"
    body = task["result"]
    assert body["benchmark_curve"][0]["value"] == pytest.approx(1.0)
    assert body["meta"]["rebalance_days"] == 15
    assert body["meta"]["groups"] == 3
    assert body["meta"]["benchmark"] == "CSI300"
    assert body["meta"]["factor_price_adjustment"] == "hfq"
    assert body["meta"]["factor_price_fields"] == ["open", "high", "low", "close"]
    assert body["meta"]["factor_price_source"].replace("\\", "/").endswith(
        "data_engine.open_high_low_close"
    )
    assert body["meta"]["factor_price_precision"] == "source_precision_no_rounding"
    assert body["meta"]["return_price_adjustment"] == "hfq"
    assert body["meta"]["return_price_source"] == "data_engine.close"
    assert body["meta"]["return_price_precision"] == "source_precision_no_rounding"
    assert body["factor_analysis"]["definition"]["grouping_method"] == "rank_vectorized"
    assert body["factor_analysis"]["cumulative_series"][0]["layer1"] == pytest.approx(1.0)
    assert len(body["factor_analysis"]["cumulative_series"]) > 0
    assert "layer3" in body["factor_analysis"]["cumulative_series"][-1]
    assert len(body["factor_analysis"]["ic_series"]) > 0
    assert len(body["factor_analysis"]["cumulative_ic_series"]) == len(
        body["factor_analysis"]["ic_series"]
    )
    assert body["factor_analysis"]["cumulative_ic_series"][-1]["value"] == pytest.approx(
        sum(point["value"] for point in body["factor_analysis"]["ic_series"])
    )
    assert len(body["factor_analysis"]["rolling_ir_series"]) > 0
    assert len(body["factor_analysis"]["industry_equity_curves"]) == 2
    industry_groups = body["factor_analysis"]["industry_group_equity_curves"]
    assert len(industry_groups) == 2
    assert set(industry_groups[0]["points"][-1]["groups"]) == {"G1", "G2", "G3"}
    quantile_metrics = body["factor_analysis"]["quantile_metrics"]
    assert [item["quantile"] for item in quantile_metrics] == [
        "Q1", "Q2", "Q3", "benchmark"
    ]
    assert set(quantile_metrics[0]) == {
        "quantile",
        "annual_return",
        "volatility",
        "sharpe",
        "max_drawdown",
        "annual_excess_return",
        "excess_volatility",
        "information_ratio",
        "benchmark_win_rate",
        # 日均单边换手；分组是等权诊断桶，口径与主组合 metrics.turnover 一致
        "turnover",
    }
    # 分组行有换手，基准行没有（基准不是按本口径换仓的组合）。
    assert all(item["turnover"] is not None for item in quantile_metrics[:-1])
    assert quantile_metrics[-1]["turnover"] is None
    assert all(
        item["annual_return"] is not None
        and item["annual_excess_return"] is not None
        for item in quantile_metrics[:-1]
    )
    benchmark_metrics = quantile_metrics[-1]
    assert benchmark_metrics["annual_return"] is not None
    assert benchmark_metrics["volatility"] is not None
    assert benchmark_metrics["sharpe"] is not None
    assert benchmark_metrics["max_drawdown"] is not None
    assert benchmark_metrics["annual_excess_return"] is None
    assert benchmark_metrics["excess_volatility"] is None
    assert benchmark_metrics["information_ratio"] is None
    assert benchmark_metrics["benchmark_win_rate"] is None
    assert len(body["benchmark_curve"]) > 0
    assert body["excess_return"] is not None


def test_factor_backtest_supermind_fixture_matches_rank_and_metric_contract(
    supermind_api_data_dir,
):
    """通过真实 API 端点验证 rank、首笔收益和分组/基准指标口径。"""
    factor_values = supermind_api_data_dir["factor_values"]
    close = supermind_api_data_dir["close"]
    benchmark_close = supermind_api_data_dir["benchmark_close"]
    symbols = list(close.columns)

    # 默认 next_close 口径：信号次日收盘成交、再次日生效，因此第 row 日
    # 持有的是用 row-2 日收盘信号选出的分组，首个展示期还没有持仓。
    # 主组合与分层曲线都用这一条口径，所以下面的期望值同时对两者成立。
    expected_returns = {}
    expected_equity = {}
    for group_number in range(1, 6):
        raw = [0.0]
        for row_number in range(2, len(close)):
            labels = rank_groups(
                factor_values.iloc[[row_number - 2]],
                5,
            ).iloc[0]
            members = [
                symbol for symbol in symbols
                if labels.loc[symbol] == group_number
            ]
            daily = (
                close.iloc[row_number][members]
                / close.iloc[row_number - 1][members]
                - 1.0
            ).mean()
            raw.append(float(daily))
        raw_returns = pd.Series(raw, index=close.index[1:])
        display_returns = raw_returns.copy()
        display_returns.iloc[0] = 0.0
        expected_returns[group_number] = raw_returns
        expected_equity[group_number] = (1.0 + display_returns).cumprod()

    benchmark_curve = benchmark_close.loc[close.index[1:]]
    benchmark_curve = benchmark_curve / benchmark_curve.iloc[0]
    benchmark_returns = benchmark_curve.pct_change().fillna(0.0)

    response = _wait_factor_task({
            "factor_code": "def market_cap(self):\n    return self.current_mv",
            "factor_name": "market_cap",
            "universe_snapshot": {
                "asset_id": "supermind-fixture",
                "version": "1",
                "content_hash": "fixture",
                "symbols": symbols,
            },
            "start": "2026-04-01",
            "end": "2026-04-30",
            "portfolio_type": "long_only",
            "rebalance_days": 1,
            "groups": 5,
            "benchmark": "CSI300",
            "analysis_level": "core",
        })

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["meta"]["factor_price_adjustment"] == "hfq"
    assert body["meta"]["return_price_adjustment"] == "hfq"
    assert body["meta"]["factor_price_source"].replace("\\", "/").endswith(
        "data_engine.open_high_low_close"
    )
    assert body["meta"]["factor_price_precision"] == "source_precision_no_rounding"
    assert body["factor_analysis"]["definition"]["grouping_method"] == "rank_vectorized"
    assert body["factor_analysis"]["definition"]["group_order"] == (
        "Q1_highest_factor_value_QN_lowest_factor_value"
    )

    points = body["factor_analysis"]["cumulative_series"]
    assert [point["date"] for point in points] == [
        date.strftime("%Y-%m-%d") for date in close.index[1:]
    ]
    assert all(point["layer1"] == pytest.approx(1.0) for point in points[:1])
    for group_number in range(1, 6):
        values = [point[f"layer{group_number}"] for point in points]
        assert values == pytest.approx(expected_equity[group_number].tolist())

    metrics = {
        item["quantile"]: item
        for item in body["factor_analysis"]["quantile_metrics"]
    }
    for group_number in range(1, 6):
        raw = expected_returns[group_number]
        expected_annual = (1.0 + raw).prod() ** (252 / len(raw)) - 1.0
        expected_volatility = raw.std(ddof=1) * np.sqrt(252)
        expected_sharpe = raw.mean() / raw.std(ddof=1) * np.sqrt(252)
        metric_equity = pd.concat([
            pd.Series([1.0]),
            (1.0 + raw).cumprod(),
        ], ignore_index=True)
        expected_drawdown = (
            metric_equity
            / metric_equity.cummax()
            - 1.0
        ).min()
        actual = metrics[f"Q{group_number}"]
        assert actual["annual_return"] == pytest.approx(expected_annual, abs=1e-12)
        assert actual["volatility"] == pytest.approx(expected_volatility, abs=1e-12)
        assert actual["sharpe"] == pytest.approx(expected_sharpe, abs=1e-12)
        assert actual["max_drawdown"] == pytest.approx(expected_drawdown, abs=1e-12)

    # 主组合与 Q1 分层现在同口径（long_only 且 stock_num 覆盖全池），
    # 因此可以直接拿上面的分层手算基准断言主组合指标。
    assert body["metrics"]["annual_return"] == pytest.approx(
        (1.0 + expected_returns[1]).prod() ** (252 / len(expected_returns[1])) - 1.0,
        abs=1e-12,
    )

    benchmark = metrics["benchmark"]
    assert benchmark["annual_return"] == pytest.approx(
        (1.0 + benchmark_returns).prod() ** (252 / len(expected_returns[1])) - 1.0,
        abs=1e-12,
    )
    assert benchmark["volatility"] == pytest.approx(
        benchmark_returns.std(ddof=1) * np.sqrt(252), abs=1e-12
    )
    assert benchmark["sharpe"] == pytest.approx(
        benchmark_returns.mean() / benchmark_returns.std(ddof=1) * np.sqrt(252),
        abs=1e-12,
    )


def test_quantile_metrics_return_absolute_and_relative_benchmark_metrics():
    dates = pd.date_range("2022-01-03", periods=4, freq="B")
    layered_equity = pd.DataFrame(
        {
            "layer1": [1.0, 1.10, 1.00, 1.20],
            "layer2": [1.0, 1.00, 1.05, 1.10],
        },
        index=dates,
    )
    benchmark_equity = pd.Series([1.0, 1.05, 1.00, 1.10], index=dates)
    layered_returns = pd.DataFrame(
        {
            # 第一项模拟因子信号日到入场日的额外收益。
            "layer1": [0.03, 0.10, -1 / 11, 0.20],
            "layer2": [0.01, 0.00, 0.05, 10 / 105],
        },
        index=dates,
    )

    metrics = _compute_quantile_metrics(
        layered_equity,
        benchmark_equity,
        periods_per_year=4,
        layered_returns=layered_returns,
        relative_benchmark_returns=pd.Series(
            [0.05, 0.01, 0.01, 0.01],
            index=dates,
        ),
    )

    q1 = metrics[0]
    group_returns = layered_returns["layer1"]
    benchmark_returns = benchmark_equity.pct_change().fillna(0.0)
    relative_benchmark_returns = pd.Series(
        [0.05, 0.01, 0.01, 0.01],
        index=dates,
        name="benchmark",
    )
    aligned = pd.concat(
        [group_returns.rename("group"), relative_benchmark_returns],
        axis=1,
        join="inner",
    ).dropna()
    excess = aligned["group"] - aligned["benchmark"]
    cumulative_excess_factor = (
        1.0
        + (1.0 + aligned["group"]).prod()
        - (1.0 + aligned["benchmark"]).prod()
    )
    common_annual_excess = cumulative_excess_factor ** (4 / len(aligned)) - 1.0
    assert q1.quantile == "Q1"
    assert q1.annual_return == pytest.approx(
        (1.0 + group_returns).prod() ** (4 / len(group_returns)) - 1
    )
    assert q1.volatility == pytest.approx(group_returns.std(ddof=1) * 2)
    assert len(aligned) == 4
    assert q1.annual_excess_return == pytest.approx(
        common_annual_excess
    )
    assert q1.excess_volatility == pytest.approx(excess.std() * 2)
    assert q1.information_ratio == pytest.approx(
        q1.annual_excess_return / q1.excess_volatility
    )
    benchmark_cumulative_returns = benchmark_equity / benchmark_equity.iloc[0] - 1.0
    assert q1.benchmark_win_rate == pytest.approx(
        (group_returns > benchmark_cumulative_returns).mean()
    )

    drawdown_metrics = _compute_quantile_metrics(
        pd.DataFrame({"layer1": [1.0, 1.0]}, index=dates[:2]),
        pd.Series([1.0, 1.0], index=dates[:2]),
        periods_per_year=4,
        layered_returns=pd.DataFrame(
            {"layer1": [-0.05, 0.0]},
            index=dates[:2],
        ),
    )
    assert drawdown_metrics[0].max_drawdown == pytest.approx(-0.05)

    benchmark = metrics[-1]
    assert benchmark.quantile == "benchmark"
    assert benchmark.annual_return == pytest.approx(
        (1.0 + benchmark_returns).prod() ** (4 / len(layered_returns)) - 1
    )
    assert benchmark.volatility == pytest.approx(benchmark_returns.std(ddof=1) * 2)
    assert benchmark.sharpe == pytest.approx(
        benchmark_returns.mean() / benchmark_returns.std(ddof=1) * 2
    )
    assert benchmark.annual_excess_return is None
    assert benchmark.excess_volatility is None
    assert benchmark.information_ratio is None
    assert benchmark.benchmark_win_rate is None


def test_factor_backtest_uses_pre_start_history_for_rolling_factor(market_data_dir):
    payload = _request_payload()
    payload.update({
        "factor_code": "def momentum_20d(self):\n    return self.close.pct_change(20)",
        "factor_name": "momentum_20d",
        "start": "2022-02-01",
        "end": "2022-03-31",
        "analysis_level": "core",
    })
    response = _wait_factor_task(payload)

    assert response.status_code == 200
    body = response.json()["result"]
    # 20 日动量在收益区间首日就有预热值；净值从首个持有日开始，
    # 而不是等到收益区间内再滚满 20 天。
    assert body["date_range"] == {"start": "2022-02-02", "end": "2022-03-31"}


def test_factor_backtest_accepts_factor_preprocessing_options(market_data_dir):
    payload = _request_payload()
    payload.update({
        "winsorize": 1,
        "standardize": 2,
        "analysis_level": "core",
    })
    response = _wait_factor_task(payload)

    assert response.status_code == 200
    body = response.json()["result"]
    assert body["factor_analysis"]["definition"]["factor_preprocess"] == {
        "winsorize": 1,
        "standardize": 2,
            "order": ["eligibility_mask", "winsorize", "standardize"],
        "axis": "cross_sectional",
    }


def test_factor_backtest_loads_sqlite_factor_by_stable_id(market_data_dir):
    payload = _request_payload()
    payload.pop("factor_code")
    payload.update({
        "library_name": "主因子库",
        # SV10_0004 已随 beebeb4 的因子库裁剪（432→331）移除，改用同分类仍在
        # 库中的 SV10_0001（收益率滚动峰度）。
        "factor_name": "SV10_0001",
    })
    response = _wait_factor_task(payload)

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_factor_backtest_loads_sqlite_factor_by_function_name(market_data_dir):
    payload = _request_payload()
    payload.pop("factor_code")
    payload.update({
        "library_name": "主因子库",
        "factor_name": "bbi",
    })
    response = _wait_factor_task(payload)

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


@pytest.mark.parametrize(
    ("library_name", "factor_name"),
    [
        ("主因子库", "SV01_0010"),
        ("主因子库", "SV09_0024"),
    ],
)
def test_factor_backtest_loads_factors_registered_in_sqlite(
    market_data_dir, library_name, factor_name
):
    """API 因子注册必须跟随 SQLite 因子库，而不是 runner 中的重复名单。"""
    payload = _request_payload()
    payload.pop("factor_code")
    payload.update({"library_name": library_name, "factor_name": factor_name})
    response = _wait_factor_task(payload)

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_factor_api_lists_factors_from_sqlite_catalog():
    from app.api.routes import _builtin_factors

    names = {factor.name for factor in _builtin_factors()}
    assert len(names) == 331
    assert {"SV01_0010", "SV09_0024"}.issubset(names)


def test_factor_api_lists_factor_libraries():
    response = client.get("/api/factor-libraries")

    assert response.status_code == 200
    payload = response.json()
    assert "libraries" in payload
    assert "factors" not in payload
    libraries = {item["name"]: item for item in payload["libraries"]}
    assert set(libraries) == {"主因子库"}
    assert libraries["主因子库"]["factor_count"] == 331

    # /api/factors 只保留指定库因子资源，不再承担因子库目录功能。
    assert client.get("/api/factors").status_code == 404


def test_factor_api_lists_factors_for_one_library():
    response = client.get("/api/factors/主因子库")

    assert response.status_code == 200
    payload = response.json()
    assert payload["library_name"] == "主因子库"
    names = {item["name"] for item in payload["factors"]}
    assert {"SV01_0010", "SV01_0020", "SV02_0012"}.issubset(names)

    missing = client.get("/api/factors/not_a_factor_library")
    assert missing.status_code == 404


def test_factor_api_returns_category_name_instead_of_category_id():
    response = client.get("/api/factors/主因子库")

    assert response.status_code == 200
    factor = response.json()["factors"][0]
    assert factor["category_name"] == "动量因子"
    assert "category_id" not in factor


def test_factor_api_registry_uses_index_yaml_as_its_only_source(tmp_path, monkeypatch):
    """替换索引后，API 注册范围应随 YAML 变化，而非依赖硬编码库名。"""
    library_name = "temporary_indexed_lib"
    (tmp_path / f"{library_name}.py").write_text(
        'from app.services.factor.base import FactorLibrary\n\nclass TemporaryIndexedLibrary(FactorLibrary):\n    def temporary_factor(self):\n        return self.close\n',
        encoding="utf-8",
    )
    (tmp_path / "index.yaml").write_text(
        "libraries:\n"
        f"  {library_name}:\n"
        f"    file: {library_name}.py\n",
        encoding="utf-8",
    )
    configure_runtime_settings(
        replace(
            get_runtime_settings(),
            factor_dir=tmp_path,
            factor_index=tmp_path / "index.yaml",
        )
    )

    from app.services.catalog.factor_catalog import sync_catalog
    from app.services.factor.runner import (
        _load_factor_from_builtin_library,
        builtin_factor_library_names,
    )

    sync_catalog(factor_dir=tmp_path)
    assert builtin_factor_library_names() == (library_name,)
    assert _load_factor_from_builtin_library(library_name, "temporary_factor").name == (
        "temporary_factor"
    )


def test_builtin_factor_loader_rejects_unsynchronized_index(tmp_path, monkeypatch):
    """源码已有因子但 index.yaml 未同步时，API 不应静默加载。"""
    library_name = "stale_indexed_lib"
    (tmp_path / f"{library_name}.py").write_text(
        'from app.services.factor.base import FactorLibrary\n\nclass StaleIndexedLibrary(FactorLibrary):\n    def first_factor(self):\n        return self.close\n',
        encoding="utf-8",
    )
    (tmp_path / "index.yaml").write_text(
        "libraries:\n"
        f"  {library_name}:\n"
        f"    file: {library_name}.py\n"
        "    class: StaleIndexedLibrary\n"
        "    factor_count: 0\n"
        "    factors: []\n",
        encoding="utf-8",
    )
    configure_runtime_settings(
        replace(
            get_runtime_settings(),
            factor_dir=tmp_path,
            factor_index=tmp_path / "index.yaml",
        )
    )

    from app.services.factor.runner import _load_builtin_factor_library

    with pytest.raises(ValueError, match="未同步"):
        _load_builtin_factor_library(library_name)


def test_factor_backtest_custom_factor_can_combine_builtin_library_methods(market_data_dir):
    payload = _request_payload()
    payload.update({
        "factor_code": BUILTIN_COMBO_FACTOR_CODE,
        "factor_name": "combo_factor",
        "library_name": ["主因子库"],
    })
    response = _wait_factor_task(payload)

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_factor_backtest_requires_exactly_one_factor_source(market_data_dir):
    payload = _request_payload()
    payload.pop("factor_code")
    assert client.post("/api/backtest/factor", json=payload).status_code == 422

    payload = _request_payload()
    payload.pop("factor_code")
    payload["library_name"] = ["主因子库", "主因子库"]
    assert client.post("/api/backtest/factor", json=payload).status_code == 422


def test_factor_backtest_accesses_csi300_close(market_data_dir):
    payload = _request_payload()
    payload["factor_code"] = '''def relative_to_csi300(self):
    return self.close.pct_change(5).sub(self.csi300.close.pct_change(5), axis=0)'''
    payload["factor_name"] = "relative_to_csi300"
    response = _wait_factor_task(payload)
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_factor_backtest_rejects_index_series_as_stock_factor(market_data_dir):
    payload = _request_payload()
    payload["factor_code"] = '''def index_close(self):
    return self.csi300.close'''
    payload["factor_name"] = "index_close"
    response = _wait_factor_task(payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "不能直接作为股票因子返回" in body["error"]


def test_factor_backtest_without_industry_mapping_returns_empty_curves(market_data_dir):
    (market_data_dir / "stock_info.parquet").unlink()
    response = _wait_factor_task(_request_payload())
    assert response.status_code == 200
    body = response.json()["result"]
    assert body["factor_analysis"]["industry_equity_curves"] == []
    assert body["factor_analysis"]["industry_group_equity_curves"] == []

    cached_response = _wait_factor_task(_request_payload())
    assert cached_response.json()["status"] == "succeeded"
    assert cached_response.json()["result"] == body


def test_factor_backtest_defaults_to_core_analysis(market_data_dir):
    payload = _request_payload()
    payload.pop("analysis_level")
    response = _wait_factor_task(payload)
    body = response.json()["result"]
    analysis = body["factor_analysis"]
    assert analysis["definition"]["analysis_level"] == "core"
    assert set(analysis["layered_returns"]) >= {"layer1", "layer2", "layer3"}
    assert [item["quantile"] for item in analysis["quantile_metrics"]] == [
        "Q1", "Q2", "Q3", "benchmark"
    ]
    assert analysis["industry_equity_curves"] == []
    assert analysis["ic_series"] == []
    assert body["meta"]["timings_seconds"]["api_total"] >= 0


def test_data_change_invalidates_cache(market_data_dir):
    first = _wait_factor_task(_request_payload())
    assert first.json()["status"] == "succeeded"
    path = market_data_dir / "volume.parquet"
    path.touch()
    second = _wait_factor_task(_request_payload())
    assert second.json()["status"] == "succeeded"


@pytest.mark.parametrize(
    "removed_field",
    ["backtest_period", "data_dir", "backtest_engine", "initial_capital", "include_equity", "include_factor_values",
     "include_positions", "rebalance", "stock_num"],
)
def test_removed_factor_request_fields_are_rejected(market_data_dir, removed_field):
    response = client.post(
        "/api/backtest/factor", json={**_request_payload(), removed_field: "legacy"}
    )
    assert response.status_code == 422


def test_invalid_factor_code_returns_error(market_data_dir):
    response = _wait_factor_task(
        {"factor_code": "this is not Python", "factor_name": "bad"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] is not None


def test_strategy_backtest_returns_equity_and_positions(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": STRATEGY_CODE,
            "benchmark": "CSI300",
            "stamp_tax": 0.001,
            "include_positions": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    meta = body["meta"]
    # 交易成本参数由 _cost_meta 单独构造，拆出来断言以免精确字典过长。
    cost_meta = {
        key: meta.pop(key)
        for key in ("commission_rate", "stamp_tax", "slippage_rate",
                    "min_commission", "cost_model",
                    "execution_timing", "execution_model")
    }
    assert meta == {
        "strategy": "EqualWeightMomentum",
        "universe": "CSI500",
        "benchmark": "CSI300",
        "engine_mode": "vectorized",
        "strategy_id": None,
        "strategy_version": None,
        "code_hash": None,
        "filter_suspended": False,
        "filter_st": False,
        "filter_limit_up": False,
        "filter_limit_down": False,
        "filter_status": {
            "is_suspended": "disabled",
            "is_st": "disabled",
            "is_limit_up": "disabled",
            "is_limit_down": "disabled",
            # 退市剔除恒开启且没有开关，四个可选开关全关时它仍然生效。
            "is_delisted": "enabled",
        },
    }
    # 成本参数必须回显，否则结果无法自证用了什么费率。
    assert cost_meta["commission_rate"] == 0.0
    assert cost_meta["stamp_tax"] == 0.001
    assert cost_meta["slippage_rate"] == 0.0
    # 向量化模式按换手率建模，没有单笔成交，因此最低手续费不适用。
    assert cost_meta["min_commission"] is None
    assert cost_meta["cost_model"]["commission"] == "turnover_times_rate"
    assert cost_meta["cost_model"]["min_commission"] == "not_applicable"
    assert cost_meta["execution_timing"] == "next_close"
    assert cost_meta["execution_model"]["fill_price"] == "next_close"
    assert cost_meta["execution_model"]["signal"] == "at_close"
    assert body["metrics"]["total_return"] is not None
    assert len(body["equity_curve"]) > 0
    assert len(body["benchmark_curve"]) > 0
    assert body["excess_return"] is not None
    assert body["equity_curve"][0]["value"] == pytest.approx(1_000_000.0)
    assert body["benchmark_curve"][0]["value"] == pytest.approx(1.0)
    assert body["positions"]
    assert body["rebalance_logs"] is None


def test_strategy_backtest_scales_vectorized_equity_by_initial_capital(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": STRATEGY_CODE,
            "initial_capital": 250_000.0,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["equity_curve"][0]["value"] == pytest.approx(250_000.0)
    assert body["final_equity"] == pytest.approx(body["equity_curve"][-1]["value"])


def test_strategy_backtest_routes_catalog_event_strategy_to_event_engine(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "donchian_a_share_event", "engine_mode": "event"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["meta"]["engine_mode"] == "event"
    assert body["event_positions"]
    assert body["events"]
    assert body["orders"] == []
    assert body["fills"] == []
    assert body["assumptions"]["execution"] == "next_close"


def test_strategy_backtest_injects_benchmark_ohlc_for_ichimoku(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "hosoda_ichimoku_a_share_event", "engine_mode": "event"},
    )

    assert response.status_code == 200
    assert response.json()["error"] is None


def test_event_strategy_reports_missing_benchmark_ohlc_field(market_data_dir):
    path = market_data_dir / "benchmark" / "index_price_000300.SH.parquet"
    benchmark = pd.read_parquet(path).drop(columns="open")
    benchmark.to_parquet(path)

    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "hosoda_ichimoku_a_share_event", "engine_mode": "event"},
    )

    assert response.status_code == 200
    assert response.json()["error"] == "ValueError: 基准 CSI300 缺少日线字段: ['open']"


def test_strategy_backtest_rejects_mismatched_catalog_engine_mode(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "donchian_a_share_event", "engine_mode": "vectorized"},
    )

    assert response.status_code == 200
    assert "execution_mode" in response.json()["error"]


def test_event_strategy_reports_symbols_missing_from_selected_universe(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''class SingleStockStrategy:
    fixed_symbols = ("S999",)

    def initialize(self, context):
        pass''',
            "engine_mode": "event",
            "universe": "CSI500",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] == "ValueError: 策略固定标的行情不可用: ['S999']"


def test_event_strategy_accepts_exchange_code_against_raw_panel_symbols(market_data_dir):
    for field in ("open", "high", "low", "close"):
        path = market_data_dir / f"{field}.parquet"
        panel = pd.read_parquet(path)
        panel["600519"] = 100.0
        panel.to_parquet(path, index=False)
    constituents_path = market_data_dir / "benchmark" / "index_weights_000300.SH.parquet"
    constituents = pd.read_parquet(constituents_path)
    constituents["600519"] = 1
    constituents.to_parquet(constituents_path)

    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''class SingleStockStrategy:
    fixed_symbols = ("600519.SH",)

    def initialize(self, context):
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        context.orders.target_quantity("600519.SH", 100)''',
            "engine_mode": "event",
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None
    assert response.json()["fills"][0]["symbol"] == "600519.SH"
    assert response.json()["meta"]["trading_symbols"] == ["600519.SH"]


def test_fixed_event_strategy_ignores_selected_universe(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''class SingleStockStrategy:
    fixed_symbols = ("S002",)

    def initialize(self, context):
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        context.orders.target_quantity("S002", 100)''',
            "engine_mode": "event",
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None
    assert response.json()["fills"][0]["symbol"] == "S002"


def test_legacy_event_symbol_ignores_selected_universe(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''class SingleStockStrategy:
    def __init__(self):
        self.symbol = "S002"

    def initialize(self, context):
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        context.orders.target_quantity(self.symbol, 100)''',
            "engine_mode": "event",
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None
    assert response.json()["meta"]["trading_symbols"] == ["S002"]


def test_declared_symbols_ignores_selected_universe(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''class BasketStrategy:
    symbols = ("S002", "S003")

    def initialize(self, context):
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        context.orders.target_quantity("S002", 100)''',
            "engine_mode": "event",
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None
    assert response.json()["meta"]["trading_symbols"] == ["S002", "S003"]


def test_pair_strategy_ignores_selected_universe(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''import pandas as pd

class PairStrategy:
    pair_symbols = ("S002", "S003")

    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        assert list(self.close.columns) == ["S002", "S003"]
        return pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)''',
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None
    assert response.json()["meta"]["trading_symbols"] == ["S002", "S003"]


def test_legacy_pair_strategy_ignores_selected_universe(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''import pandas as pd

class PairStrategy:
    def __init__(self, close, stock_a="S002", stock_b="S003"):
        self.close = close
        self.stock_a = stock_a
        self.stock_b = stock_b

    def generate_signals(self):
        assert list(self.close.columns) == ["S002", "S003"]
        return pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)''',
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None
    assert response.json()["meta"]["trading_symbols"] == ["S002", "S003"]


def test_vectorized_strategy_receives_exchange_code_columns(market_data_dir):
    path = market_data_dir / "close.parquet"
    close = pd.read_parquet(path)
    close["600519"] = 100.0
    close.to_parquet(path, index=False)
    constituents_path = market_data_dir / "benchmark" / "index_weights_000300.SH.parquet"
    constituents = pd.read_parquet(constituents_path)
    constituents["600519"] = 1
    constituents.to_parquet(constituents_path)

    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": '''import pandas as pd

class ExchangeCodeStrategy:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        assert "600519.SH" in self.close.columns
        assert "600519" not in self.close.columns
        return pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)''',
            "universe": "CSI300",
        },
    )

    assert response.status_code == 200
    assert response.json()["error"] is None


def test_strategy_backtest_returns_rebalance_logs_on_demand(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": STRATEGY_CODE,
            "benchmark": "CSI300",
            "include_rebalance_logs": True,
        },
    )

    assert response.status_code == 200
    logs = response.json()["rebalance_logs"]
    assert logs
    trade = logs[0]["trades"][0]
    assert set(trade) == {"symbol", "name", "side", "previous_weight", "target_weight", "weight_change"}
    assert trade["side"] in {"buy", "sell"}


def test_strategy_backtest_returns_vectorized_execution_timeline_on_demand(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_code": STRATEGY_CODE, "include_execution_timeline": True},
    )

    assert response.status_code == 200
    timeline = response.json()["execution_timeline"]
    assert timeline
    assert timeline[0]["type"] == "rebalance"
    assert timeline[0]["payload"]["trades"]


def test_strategy_backtest_returns_event_execution_timeline_on_demand(market_data_dir):
    strategy_code = '''class DailyBuy:
    def initialize(self, context):
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        context.orders.target_quantity("S000", 100)'''
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": strategy_code,
            "engine_mode": "event",
            "include_execution_timeline": True,
        },
    )

    assert response.status_code == 200
    timeline = response.json()["execution_timeline"]
    assert timeline
    assert {item["type"] for item in timeline} >= {"audit", "order", "fill", "position"}


def test_strategy_response_instruments_include_exchange_code_and_name():
    stock_names = {"000001": "平安银行"}
    positions = pd.DataFrame(
        {"000001": [1.0]}, index=pd.to_datetime(["2024-01-02"])
    )
    logs = pd.DataFrame(
        [{
            "symbol": "000001",
            "side": "buy",
            "previous_weight": 0.0,
            "target_weight": 1.0,
            "weight_change": 1.0,
        }],
        index=pd.to_datetime(["2024-01-02"]),
    )

    holding = _sample_positions(positions, stock_names)[0].holdings[0]
    trade = _serialize_rebalance_logs(logs, stock_names)[0].trades[0]

    assert holding.model_dump() == {
        "symbol": "000001.SZ", "name": "平安银行", "weight": 1.0,
    }
    assert trade.model_dump()["symbol"] == "000001.SZ"
    assert trade.model_dump()["name"] == "平安银行"


def test_display_instrument_accepts_exchange_code_panel_symbol():
    assert _display_instrument("600519.SH", {"600519": "贵州茅台"}) == (
        "600519.SH", "贵州茅台",
    )


def test_strategy_catalog_lists_and_runs_saved_version(market_data_dir, tmp_path, monkeypatch):
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "equal_weight_v1.py").write_text(STRATEGY_CODE, encoding="utf-8")
    (strategy_dir / "index.yaml").write_text(
        """version: 2
strategies:
  equal_weight:
    name: 等权动量
    strategy_type: momentum
    execution_mode: vectorized
    current_version: v1
    tags: [momentum]
    versions:
      v1:
        file: equal_weight_v1.py
        entrypoint: EqualWeightMomentum
""",
        encoding="utf-8",
    )
    configure_runtime_settings(
        replace(
            get_runtime_settings(),
            strategy_dir=strategy_dir,
            strategy_index=strategy_dir / "index.yaml",
        )
    )

    listed = client.get("/api/strategies")
    assert listed.status_code == 200
    assert listed.json() == {
        "strategies": [{
            "id": "equal_weight",
            "name": "等权动量",
            "strategy_type": "momentum",
            "execution_mode": "vectorized",
            "current_version": "v1",
            "entrypoint": "EqualWeightMomentum",
            "tags": ["momentum"],
        }]
    }

    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "equal_weight"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["error"] is None
    assert body["meta"]["strategy_id"] == "equal_weight"
    assert body["meta"]["strategy_version"] == "v1"
    assert len(body["meta"]["code_hash"]) == 64


def test_strategy_catalog_rejects_unknown_version_and_mixed_source(market_data_dir, tmp_path, monkeypatch):
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "index.yaml").write_text(
        """version: 2
strategies:
  empty:
    name: 空策略
    strategy_type: test
    execution_mode: vectorized
    current_version: v1
    versions:
      v1:
        file: missing.py
        entrypoint: Missing
""",
        encoding="utf-8",
    )
    configure_runtime_settings(
        replace(
            get_runtime_settings(),
            strategy_dir=strategy_dir,
            strategy_index=strategy_dir / "index.yaml",
        )
    )

    response = client.post("/api/backtest/strategy", json={"strategy_id": "empty"})
    assert response.status_code == 200
    assert "策略源码不存在" in response.json()["error"]

    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "empty", "strategy_code": STRATEGY_CODE},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "strategy_id",
    [
        "dual_moving_average",
        "cross_sectional_momentum",
        "short_term_reversal",
        "low_volatility",
        "donchian_breakout",
    ],
)
def test_builtin_strategy_library_entries_run_with_close_panel(market_data_dir, strategy_id):
    response = client.post("/api/backtest/strategy", json={"strategy_id": strategy_id})

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["meta"]["strategy_id"] == strategy_id
    assert body["meta"]["strategy_version"] == "v1"


def test_builtin_dual_thrust_strategy_injects_required_market_panels(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_id": "dual_thrust_a_share"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["meta"]["strategy_id"] == "dual_thrust_a_share"


def test_strategy_backtest_returns_business_error_for_invalid_contract(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_code": "class Invalid:\n    pass"},
    )
    assert response.status_code == 200
    assert "generate_signals" in response.json()["error"]


def test_strategy_backtest_returns_business_error_when_benchmark_is_unavailable(market_data_dir):
    (market_data_dir / "benchmark" / "index_price_000300.SH.parquet").unlink()
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_code": STRATEGY_CODE, "benchmark": "CSI300"},
    )
    assert response.status_code == 200
    assert "index_price_000300.SH.parquet" in response.json()["error"]


def test_strategy_backtest_rejects_unknown_fields(market_data_dir):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_code": STRATEGY_CODE, "unknown": True},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "removed_field",
    ["strategy_params", "factor_code", "factor_name", "data_dir", "backtest_engine", "include_equity"],
)
def test_strategy_backtest_rejects_removed_request_fields(market_data_dir, removed_field):
    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_code": STRATEGY_CODE, removed_field: "legacy"},
    )
    assert response.status_code == 422


def test_structured_factor_portfolio_strategy_backtest(market_data_dir):
    pe_ttm = pd.DataFrame(
        np.linspace(5.0, 30.0, 90 * 12).reshape(90, 12),
        index=pd.bdate_range("2022-01-03", periods=90),
        columns=[f"S{i:03d}" for i in range(12)],
    )
    pe_ttm.index.name = "datetime"
    pe_ttm.reset_index().to_parquet(market_data_dir / "pe_ttm.parquet", index=False)
    DataEngine.get_instance().load_parquet(market_data_dir)

    response = client.post(
        "/api/backtest/strategy",
        json={"strategy_code": STRUCTURED_STRATEGY_CODE, "include_positions": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["meta"]["strategy"] == "CompositeFactorStrategy"
    assert body["positions"]


def _write_structured_extra_fields(
    market_data_dir, *, listed_st=None, listed_delisted=None, market_status=None
):
    """为结构化策略补写 pe_ttm 与状态字段，供过滤相关用例共享。"""

    dates = pd.bdate_range("2022-01-03", periods=90)
    symbols = [f"S{i:03d}" for i in range(12)]
    pe_ttm = pd.DataFrame(
        np.linspace(5.0, 30.0, 90 * 12).reshape(90, 12), index=dates, columns=symbols
    )
    pe_ttm.index.name = "datetime"
    pe_ttm.reset_index().to_parquet(market_data_dir / "pe_ttm.parquet", index=False)

    if listed_st is not None or listed_delisted is not None:
        listed = pd.DataFrame("在市", index=dates, columns=symbols)
        if listed_st is not None:
            listed[listed_st] = "ST"
        if listed_delisted is not None:
            # 后半段转为终止上市，用于验证「点时剔除」而不是整段剔除。
            listed.loc[dates[len(dates) // 2]:, listed_delisted] = "终止上市"
        listed.index.name = "datetime"
        listed.reset_index().to_parquet(
            market_data_dir / "listed_status.parquet", index=False
        )
    if market_status is not None:
        status = pd.DataFrame("非涨跌停", index=dates, columns=symbols)
        for symbol, value in market_status.items():
            status[symbol] = value
        status.index.name = "datetime"
        status.reset_index().to_parquet(
            market_data_dir / "up_and_down_status.parquet", index=False
        )
    DataEngine.get_instance().load_parquet(market_data_dir)


def _held_symbols(body) -> set[str]:
    """实际持仓标的（权重 > 0）；未选中的标的会以 0 权重出现在快照里。"""

    return {
        holding["symbol"]
        for snapshot in body["positions"] or []
        for holding in snapshot["holdings"]
        if holding["weight"] > 0.0
    }


def test_vectorized_structured_filter_excludes_without_changing_rebalance_cadence(
    market_data_dir,
):
    """结构化策略在排名前剔除：不合格标的退出 TopN，且不改变调仓节奏。

    这是本改动最易出错处——若把非调仓日的 NaN 一并写成 0，``ffill`` 语义被破坏，
    调仓日集合会从每 15 个交易日一次退化成每个交易日一次。
    """

    _write_structured_extra_fields(market_data_dir, market_status={"S000": "停牌"})

    def run(filter_suspended: bool) -> dict:
        response = client.post(
            "/api/backtest/strategy",
            json={
                "strategy_code": STRUCTURED_STRATEGY_CODE,
                "include_positions": True,
                "include_rebalance_logs": True,
                "filter_suspended": filter_suspended,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        return body

    baseline = run(False)
    filtered = run(True)

    baseline_dates = [log["date"] for log in baseline["rebalance_logs"]]
    assert len(baseline_dates) > 1
    assert [log["date"] for log in filtered["rebalance_logs"]] == baseline_dates
    assert filtered["meta"]["filter_suspended"] is True
    assert filtered["meta"]["filter_status"]["is_suspended"] == "enabled"
    assert "S000" not in _held_symbols(filtered)


def test_vectorized_structured_filter_st_drops_st_from_topn(market_data_dir):
    _write_structured_extra_fields(market_data_dir, listed_st="S000")

    def run(filter_st: bool) -> dict:
        response = client.post(
            "/api/backtest/strategy",
            json={
                "strategy_code": STRUCTURED_STRATEGY_CODE,
                "include_positions": True,
                "filter_st": filter_st,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        return body

    baseline = run(False)
    filtered = run(True)

    assert filtered["meta"]["filter_st"] is True
    assert filtered["meta"]["filter_status"]["is_st"] == "enabled"
    assert "S000" in _held_symbols(baseline)
    assert "S000" not in _held_symbols(filtered)


def _run_structured_strategy() -> dict:
    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": STRUCTURED_STRATEGY_CODE,
            "include_positions": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    return body


def test_vectorized_structured_excludes_delisted_point_in_time(market_data_dir):
    """退市剔除恒开启，且是「点时」而非「整段」剔除。

    用两次回测的权益曲线逐点对比来验证：退市之前必须与「无退市」基线逐点
    一致（历史未被整段丢弃，避免存活偏差），退市之后必须发生变化（剔除真的
    生效了）。这比断言某个标的在不在 TopN 里稳健得多。
    """

    _write_structured_extra_fields(market_data_dir)
    baseline = _run_structured_strategy()

    _write_structured_extra_fields(market_data_dir, listed_delisted="S000")
    filtered = _run_structured_strategy()

    # 不传任何 filter_* 参数，退市剔除仍然生效。
    assert filtered["meta"]["filter_status"]["is_delisted"] == "enabled"
    assert filtered["meta"]["filter_suspended"] is False
    assert filtered["meta"]["filter_st"] is False

    base_curve = {p["date"]: p["value"] for p in baseline["equity_curve"]}
    filt_curve = {p["date"]: p["value"] for p in filtered["equity_curve"]}
    dates = pd.bdate_range("2022-01-03", periods=90)
    transition = dates[len(dates) // 2]

    common = sorted(set(base_curve) & set(filt_curve))
    before = [d for d in common if pd.Timestamp(d) < transition]
    after = [d for d in common if pd.Timestamp(d) > transition]
    assert before and after

    # 退市之前逐点一致 -> 不是整段剔除。
    assert all(
        base_curve[d] == pytest.approx(filt_curve[d]) for d in before
    ), "退市前的历史被整段剔除了，会引入存活偏差"
    # 退市之后确实不同 -> 剔除生效。
    assert any(
        abs(base_curve[d] - filt_curve[d]) > 1e-6 for d in after
    ), "退市后权益应与基线不同，剔除没有生效"

    # 退市后必须退出持仓。默认 next_close 口径下信号要次日收盘才成交，
    # 因此不能断言「转换日次日立刻消失」——只断言到回测结束前它确实已被剔除。
    held_dates = sorted(
        pd.Timestamp(snapshot["date"])
        for snapshot in filtered["positions"]
        if any(
            holding["symbol"] == "S000" and holding["weight"] > 0.0
            for holding in snapshot["holdings"]
        )
    )
    assert held_dates, "基线里 S000 本应被持有过，说明样本构造有问题"
    assert max(held_dates) < max(pd.Timestamp(p["date"]) for p in filtered["equity_curve"]), (
        "退市标的一直被持有到回测结束，退市剔除没有生效"
    )


def test_factor_backtest_excludes_delisted_point_in_time(market_data_dir):
    """因子回测同样强制剔除退市股，且是「点时」而非「整段」剔除。

    与策略侧用同一套权益曲线逐点对比的判据：退市之前必须与「无退市」基线
    逐点一致，退市之后必须发生变化。
    """

    _write_structured_extra_fields(market_data_dir)
    baseline = _wait_factor_task(_request_payload()).json()["result"]
    assert baseline["error"] is None

    _write_structured_extra_fields(market_data_dir, listed_delisted="S000")
    filtered = _wait_factor_task(_request_payload()).json()["result"]
    assert filtered["error"] is None

    assert filtered["meta"]["filter_status"]["is_delisted"] == "enabled"
    # 没有传任何 filter_* 参数，退市剔除仍然生效。
    assert filtered["meta"]["filter_suspended"] is False
    assert filtered["meta"]["filter_st"] is False

    base_curve = {p["date"]: p["value"] for p in baseline["equity_curve"]}
    filt_curve = {p["date"]: p["value"] for p in filtered["equity_curve"]}
    dates = pd.bdate_range("2022-01-03", periods=90)
    transition = dates[len(dates) // 2]

    common = sorted(set(base_curve) & set(filt_curve))
    before = [d for d in common if pd.Timestamp(d) < transition]
    after = [d for d in common if pd.Timestamp(d) > transition]
    assert before and after

    assert all(
        base_curve[d] == pytest.approx(filt_curve[d]) for d in before
    ), "退市前的历史被整段剔除了，会引入存活偏差"
    assert any(
        abs(base_curve[d] - filt_curve[d]) > 1e-9 for d in after
    ), "退市后因子样本应发生变化，剔除没有生效"


def test_vectorized_free_strategy_filter_zeroes_weight_instead_of_nan(market_data_dir):
    """自由策略逐日输出权重，被剔除的标的必须写 0；写 NaN 会被前值填充继续持有。"""

    dates = pd.bdate_range("2022-01-03", periods=90)
    symbols = [f"S{i:03d}" for i in range(12)]
    status = pd.DataFrame("跌停", index=dates, columns=symbols)
    status.index.name = "datetime"
    status.reset_index().to_parquet(
        market_data_dir / "up_and_down_status.parquet", index=False
    )
    DataEngine.get_instance().load_parquet(market_data_dir)

    response = client.post(
        "/api/backtest/strategy",
        json={
            "strategy_code": STRATEGY_CODE,
            "filter_limit_down": True,
            "initial_capital": 1_000_000,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["meta"]["filter_status"]["is_limit_down"] == "enabled"
    # 全线跌停被整体剔除 -> 组合空仓，资金保持不变。
    assert body["final_equity"] == pytest.approx(1_000_000.0)
    assert body["metrics"]["total_return"] == pytest.approx(0.0)


def test_factor_api_reads_current_market_data_layout(market_data_dir):
    import shutil

    current = market_data_dir / "current"
    panels = current / "panels"
    metadata = current / "metadata"
    panels.mkdir(parents=True)
    metadata.mkdir()
    for path in market_data_dir.glob("*.parquet"):
        target_dir = metadata if path.name == "stock_info.parquet" else panels
        shutil.copy2(path, target_dir / path.name)
    shutil.copytree(market_data_dir / "display", current / "display")
    shutil.copytree(market_data_dir / "benchmark", current / "benchmark")

    response = _wait_factor_task(_request_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"


def test_experiment_task_routes_are_not_public():
    assert client.post("/api/experiments/factor", json={}).status_code == 404
    assert client.post("/api/experiments/strategy", json={}).status_code == 404
    assert client.get("/api/experiments/example-run-id").status_code == 404
    assert client.delete("/api/experiments/example-run-id").status_code == 404


def test_factor_backtest_costs_apply_to_main_portfolio_only(market_data_dir):
    """成本真正作用于主组合净值与指标；分层曲线与多空价差保持毛收益。"""

    _write_structured_extra_fields(market_data_dir)
    zero = _wait_factor_task(_request_payload()).json()["result"]
    costly = _wait_factor_task({
        **_request_payload(),
        "commission_rate": 0.01,
        "stamp_tax": 0.01,
        "slippage_rate": 0.01,
    }).json()["result"]

    # 主组合：净值、指标、成本明细都反映成本。
    assert costly["final_equity"] < zero["final_equity"]
    assert zero["metrics"]["total_cost"] == pytest.approx(0.0)
    assert costly["metrics"]["total_cost"] > 0.0
    assert costly["metrics"]["commission_cost"] > 0.0
    assert costly["metrics"]["stamp_tax_cost"] > 0.0
    assert costly["metrics"]["slippage_cost"] > 0.0
    # 换手率与费率无关，两次必须一致。
    assert costly["metrics"]["total_turnover"] == pytest.approx(
        zero["metrics"]["total_turnover"]
    )

    # 结果自证：成本参数与建模口径都回显在 meta 里。
    assert zero["meta"]["commission_rate"] == 0.0
    assert costly["meta"]["commission_rate"] == pytest.approx(0.01)
    assert costly["meta"]["stamp_tax"] == pytest.approx(0.01)
    assert costly["meta"]["slippage_rate"] == pytest.approx(0.01)
    assert costly["meta"]["cost_model"]["main_portfolio"] == (
        "net_of_commission_stamp_tax_and_slippage"
    )
    assert costly["meta"]["cost_model"]["layered_and_long_short"] == "gross"

    # 分层曲线（多空价差也由它派生）是因子排序能力的诊断口径，扣成本前后一致。
    assert zero["factor_analysis"]["layered_returns"] == (
        costly["factor_analysis"]["layered_returns"]
    )


def test_event_strategy_echoes_cost_parameters(market_data_dir):
    """事件模式回显四个成本参数；向量化模式的 min_commission 不适用。"""

    _write_structured_extra_fields(market_data_dir)

    def run(engine_mode: str, **extra) -> dict:
        response = client.post(
            "/api/backtest/strategy",
            json={
                "strategy_code": STRATEGY_CODE if engine_mode == "vectorized" else '''class EventHold:
    fixed_symbols = ("S000",)

    def initialize(self, context):
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        if context.current_dt.date().isoformat() == "2022-01-05":
            context.orders.target_value("S000", 500_000)
''',
                "engine_mode": engine_mode,
                "universe": "CSI300",
                **extra,
            },
        )
        body = response.json()
        assert body["error"] is None, body["error"]
        return body["meta"]

    event_meta = run(
        "event", commission_rate=0.002, stamp_tax=0.001,
        slippage_rate=0.003, min_commission=3.0,
    )
    assert event_meta["commission_rate"] == pytest.approx(0.002)
    assert event_meta["stamp_tax"] == pytest.approx(0.001)
    assert event_meta["slippage_rate"] == pytest.approx(0.003)
    assert event_meta["min_commission"] == pytest.approx(3.0)
    assert event_meta["cost_model"]["slippage"] == "price_adjusted_buy_up_sell_down"

    vectorized_meta = run("vectorized")
    assert vectorized_meta["stamp_tax"] == pytest.approx(0.001)
    assert vectorized_meta["min_commission"] is None
    assert vectorized_meta["cost_model"]["min_commission"] == "not_applicable"


def test_factor_backtest_execution_timing_applies_to_layered_too(market_data_dir):
    """成交时点同时作用于主组合与分层曲线，两者口径一致。"""

    _write_structured_extra_fields(market_data_dir)

    def run(timing: str) -> dict:
        body = _wait_factor_task({
            **_request_payload(), "execution_timing": timing,
        }).json()["result"]
        assert body["error"] is None, body["error"]
        return body

    default = run("next_close")
    opened = run("next_open")
    legacy = run("same_close")

    # 默认就是 next_close，两者口径不同、结果也不同。
    assert default["meta"]["execution_timing"] == "next_close"
    assert opened["meta"]["execution_timing"] == "next_open"
    assert default["final_equity"] != opened["final_equity"]
    assert default["metrics"]["annual_return"] != opened["metrics"]["annual_return"]

    # 回显：信号恒在收盘、成交在 T+1。
    model = opened["meta"]["execution_model"]
    assert model["signal"] == "at_close"
    assert model["fill_price"] == "next_open"
    assert model["fill_session"] == "T+1"
    assert model["layered_and_long_short"] == "matches_main_portfolio_timing_gross"
    assert opened["meta"]["return_price_source"] == "data_engine.open + data_engine.close"
    assert default["meta"]["return_price_source"] == "data_engine.close"

    # 分层曲线与主组合同口径，因此同样随成交时点变化。
    assert default["factor_analysis"]["layered_returns"] != (
        opened["factor_analysis"]["layered_returns"]
    )
    assert legacy["factor_analysis"]["layered_returns"] != (
        default["factor_analysis"]["layered_returns"]
    )

    # same_close 是保留的旧口径（信号日收盘成交），三种取值互不相同。
    assert legacy["meta"]["execution_timing"] == "same_close"
    assert legacy["meta"]["execution_model"]["fill_session"] == "T"
    assert len({
        legacy["final_equity"], default["final_equity"], opened["final_equity"]
    }) == 3


def test_factor_backtest_execution_timing_defaults_to_next_close(market_data_dir):
    _write_structured_extra_fields(market_data_dir)

    omitted = _wait_factor_task(_request_payload()).json()["result"]
    explicit = _wait_factor_task({
        **_request_payload(), "execution_timing": "next_close",
    }).json()["result"]

    assert omitted["meta"]["execution_timing"] == "next_close"
    assert omitted["final_equity"] == pytest.approx(explicit["final_equity"])
    assert omitted["metrics"]["annual_return"] == pytest.approx(
        explicit["metrics"]["annual_return"]
    )


def test_factor_industry_curves_follow_execution_timing(market_data_dir):
    """行业净值曲线与主组合/分层同口径；行业 IC 不受成交时点影响。"""

    _write_structured_extra_fields(market_data_dir)

    def run(timing: str) -> dict:
        body = _wait_factor_task({
            **_request_payload(), "analysis_level": "full",
            "execution_timing": timing,
        }).json()["result"]
        assert body["error"] is None, body["error"]
        return body["factor_analysis"]

    analysis = {t: run(t) for t in ("same_close", "next_close", "next_open")}

    def industry(timing: str):
        curves = analysis[timing]["industry_group_equity_curves"]
        return {curve["industry_name"]: curve["points"] for curve in curves}

    # 三种口径的行业净值曲线互不相同（曲线本身随成交时点平移）。
    closed = industry("same_close")
    default = industry("next_close")
    opened = industry("next_open")
    assert closed != default
    assert closed != opened
    assert default != opened

    # 全部基于远期收益的统计都跟随成交时点：IC / 行业 IC / 行业曲线一致变化。
    ic = analysis["same_close"]["ic_series"]
    assert ic
    for timing in ("next_close", "next_open"):
        assert analysis[timing]["ic_series"] != ic
