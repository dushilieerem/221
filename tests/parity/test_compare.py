"""比对器的合成数据测试；不依赖真实行情与聚宽金标。"""

from __future__ import annotations

import dataclasses

from app.services.parity.compare import compare_runs
from app.services.parity.report import render_markdown
from support import DATES, SYMBOL_A, SYMBOL_B, make_case, make_runs


def _layer(result, name):
    return next(layer for layer in result.layers if layer.layer == name)


def test_identical_runs_pass():
    jq, svq = make_runs()
    result = compare_runs(jq, svq, make_case())
    assert result.status == "pass"
    assert result.first_divergence is None


def test_daily_return_divergence_is_strict_and_ordered():
    jq, svq = make_runs()
    svq.daily.loc[svq.daily.index[2], "equity"] += 5_000.0

    result = compare_runs(jq, svq, make_case())

    assert result.status == "diverged"
    assert _layer(result, "L4").strict_failures > 0
    assert result.first_divergence is not None
    assert result.first_divergence.layer == "L4"
    assert result.first_divergence.date == "2024-01-04"


def test_missing_fill_event_fails_under_hfq():
    jq, svq = make_runs()
    svq.fills = svq.fills.iloc[1:].reset_index(drop=True)

    result = compare_runs(jq, svq, make_case(price_basis="hfq"))

    assert result.status == "diverged"
    layer = _layer(result, "L2")
    assert any(item.field == "fill_event" and item.strict for item in layer.divergences)


def test_symbol_set_difference_fails():
    jq, svq = make_runs()
    svq.positions = svq.positions.replace(SYMBOL_B, "600036.SH")
    svq.fills = svq.fills.replace(SYMBOL_B, "600036.SH")

    result = compare_runs(jq, svq, make_case())

    assert result.status == "diverged"
    fields = {item.field for item in _layer(result, "L1").divergences}
    assert "symbol_set" in fields


def test_holding_mask_difference_fails():
    jq, svq = make_runs()
    drop = (svq.positions["date"] == DATES[2]) & (svq.positions["symbol"] == SYMBOL_B)
    svq.positions = svq.positions[~drop].reset_index(drop=True)

    result = compare_runs(jq, svq, make_case())

    assert result.status == "diverged"
    layer = _layer(result, "L3")
    assert any(item.field == "holding" for item in layer.divergences)


def test_quantity_difference_is_reference_under_hfq():
    jq, svq = make_runs()
    svq.positions["quantity"] = svq.positions["quantity"] * 2

    hfq = compare_runs(jq, svq, make_case(price_basis="hfq"))
    assert hfq.status == "pass"
    assert _layer(hfq, "L3").reference_failures > 0

    real = compare_runs(jq, svq, make_case(price_basis="real"))
    assert real.status == "diverged"
    assert _layer(real, "L3").strict_failures > 0


def test_calendar_mismatch_fails_l0():
    jq, svq = make_runs()
    svq.daily = svq.daily.iloc[:-1].reset_index(drop=True)

    result = compare_runs(jq, svq, make_case())

    assert result.status == "diverged"
    fields = {item.field for item in _layer(result, "L0").divergences}
    assert "trading_dates.missing_in_svquant" in fields


def test_declared_parameter_mismatch_fails_l0():
    jq, svq = make_runs()
    case = dataclasses.replace(
        make_case(),
        svquant={**make_case().svquant, "commission_rate": 0.001},
    )

    result = compare_runs(jq, svq, case)

    assert result.status == "diverged"
    fields = {item.field for item in _layer(result, "L0").divergences}
    assert "commission" in fields


def test_report_mentions_first_divergence():
    jq, svq = make_runs()
    svq.daily.loc[svq.daily.index[2], "equity"] += 5_000.0
    result = compare_runs(jq, svq, make_case())

    markdown = render_markdown(result, provenance={"jq.backtest_id": "abc"})

    assert "首个严格分歧" in markdown
    assert "2024-01-04" in markdown
    assert "abc" in markdown
    assert "## L4" in markdown


def test_symbol_set_divergence_carries_first_seen_date():
    jq, svq = make_runs()
    svq.positions = svq.positions.replace(SYMBOL_B, "600036.SH")
    svq.fills = svq.fills.replace(SYMBOL_B, "600036.SH")

    result = compare_runs(jq, svq, make_case())

    row = next(
        item for item in _layer(result, "L1").divergences
        if item.field == "symbol_set" and item.symbol == SYMBOL_B
    )
    assert row.date == DATES[2].strftime("%Y-%m-%d")


def test_first_holding_date_divergence_is_dated_for_ordering():
    jq, svq = make_runs()
    drop = (svq.positions["date"] == DATES[1]) & (svq.positions["symbol"] == SYMBOL_A)
    svq.positions = svq.positions[~drop].reset_index(drop=True)

    result = compare_runs(jq, svq, make_case())

    row = next(
        item for item in _layer(result, "L1").divergences
        if item.field == "first_holding_date" and item.symbol == SYMBOL_A
    )
    assert row.date == min(row.expected, row.actual)


def test_returns_only_case_skips_trade_layers():
    """只有收益曲线时，L1–L3 不应被构建，也不能因为空帧而误判通过。"""
    case = dataclasses.replace(
        make_case(),
        comparison={"price_basis": "real", "layers": ["L0", "L4", "L5"]},
    )
    jq, svq = make_runs()

    result = compare_runs(jq, svq, case)

    assert [layer.layer for layer in result.layers] == ["L0", "L4", "L5"]
    assert case.strict_layers == {"L0", "L4", "L5"}
