"""对账 schema、符号映射与聚宽导出加载的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.services.parity.loader import ParityError, load_case, load_golden
from app.services.parity.schema import (
    Tolerances,
    normalize_side,
    normalize_symbol,
    pick_column,
)

CASE_DIR = Path(__file__).parent / "cases" / "fixed_basket_hold"


def test_normalize_symbol_converts_joinquant_suffix():
    assert normalize_symbol("600519.XSHG") == "600519.SH"
    assert normalize_symbol("000001.XSHE") == "000001.SZ"
    assert normalize_symbol("430047.XBSE") == "430047.BJ"
    assert normalize_symbol("600519.SH") == "600519.SH"
    assert normalize_symbol(" 600519.xshg ") == "600519.SH"


def test_normalize_side_maps_open_close():
    assert normalize_side("open") == "buy"
    assert normalize_side("close") == "sell"
    assert normalize_side("BUY") == "buy"
    assert normalize_side("hold") is None


def test_pick_column_uses_aliases():
    frame = pd.DataFrame(columns=["time", "security", "amount"])
    assert pick_column(frame, "date") == "time"
    assert pick_column(frame, "symbol") == "security"
    assert pick_column(frame, "quantity") == "amount"
    assert pick_column(frame, "cash") is None


def test_tolerances_reject_unknown_keys():
    with pytest.raises(ValueError, match="未知的容差项"):
        Tolerances.from_mapping({"quantity": 0, "unknown": 1})


def test_real_case_declares_matching_parameters():
    case = load_case(CASE_DIR)
    assert case.case_id == "fixed_basket_hold"
    assert case.jq["options"]["commission"] == case.svquant["commission_rate"]
    assert case.jq["options"]["min_commission"] == case.svquant["min_commission"]
    assert case.jq["options"]["stamp_tax"] == case.svquant["stamp_tax"]
    assert case.price_basis == "hfq"


def test_load_golden_normalizes_joinquant_exports(tmp_path: Path):
    case_dir = tmp_path / "case"
    expected = case_dir / "expected"
    expected.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(
        "\n".join([
            "schema_version: 1",
            "name: synthetic_case",
            "jq:",
            '  backtest_id: "123"',
            "  initial_cash: 1000000",
            '  benchmark: "000300.XSHG"',
            "svquant:",
            "  initial_capital: 1000000",
        ]),
        encoding="utf-8",
    )
    pd.DataFrame({
        "time": ["2024-01-02", "2024-01-03"],
        "returns": [0.0, 0.01],
    }).to_csv(expected / "daily.csv", index=False)
    pd.DataFrame({
        "time": ["2024-01-02 09:30:00", "2024-01-03 09:30:00"],
        "security": ["000001.XSHE", "000001.XSHE"],
        "amount": [1000, 1000],
        "closeable_amount": [0, 1000],
    }).to_csv(expected / "positions.csv", index=False)
    pd.DataFrame({
        "time": ["2024-01-02 09:30:00"],
        "security": ["000001.XSHE"],
        "action": ["open"],
        "filled": [1000],
        "price": [10.0],
        "commission": [5.0],
    }).to_csv(expected / "orders.csv", index=False)
    pd.DataFrame({
        "time": ["2024-01-02", "2024-01-03"],
        "cash": [1_000_000.0, 900_000.0],
        "total_value": [1_000_000.0, 1_010_000.0],
    }).to_csv(expected / "balances.csv", index=False)

    case = load_case(case_dir)
    golden = load_golden(case)

    assert list(golden.daily["date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert golden.daily["equity"].iloc[0] == pytest.approx(1_000_000.0)
    assert golden.daily["equity"].iloc[-1] == pytest.approx(1_010_000.0)
    # balances 必须合并成单一 cash 列，而不是 cash_x/cash_y。
    assert "cash" in golden.daily.columns
    assert golden.daily["cash"].iloc[-1] == pytest.approx(900_000.0)
    assert set(golden.positions["symbol"]) == {"000001.SZ"}
    assert golden.fills.iloc[0]["side"] == "buy"
    assert golden.fills.iloc[0]["quantity"] == 1000


def test_load_golden_reports_missing_columns(tmp_path: Path):
    case_dir = tmp_path / "case"
    expected = case_dir / "expected"
    expected.mkdir(parents=True)
    (case_dir / "case.yaml").write_text(
        "\n".join([
            "schema_version: 1",
            "name: bad",
            'jq: {backtest_id: "1", initial_cash: 1000000}',
            "svquant: {initial_capital: 1000000}",
        ]),
        encoding="utf-8",
    )
    pd.DataFrame({"time": ["2024-01-02"], "foo": [1]}).to_csv(expected / "daily.csv", index=False)
    pd.DataFrame({
        "time": ["2024-01-02"], "security": ["000001.XSHE"], "amount": [1000],
    }).to_csv(expected / "positions.csv", index=False)

    with pytest.raises(ParityError, match="缺少列"):
        load_golden(load_case(case_dir))
