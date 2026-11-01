"""聚宽页面导出转换器的合成数据测试。"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd
import pytest

from app.services.parity.jq_ui_export import import_ui_export
from app.services.parity.loader import load_case, load_golden


def _write(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="gbk", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _make_source(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "result_1.csv", [
        ["时间", "基准收益", "策略收益", "当日盈利", "当日亏损", "当日买入", "当日卖出", "超额收益(%)"],
        ["2024-01-02 16:00:00", "0", "0", "0", "0", "0", "0", "0"],
        ["2024-01-03 16:00:00", "0.5", "1.0", "10000", "0", "10000", "0", "0.5"],
    ])
    _write(directory / "transaction.csv", [
        ["日期", "委托时间", "品种", "标的", "交易类型", "下单类型", "成交数量", "成交价",
         "成交额", "委托数量", "委托价格", "平仓盈亏", "手续费", "状态", "最后更新时间"],
        ["2024-01-03", "09:30:00", "股票", "平安银行(000001.XSHE)", "买", "市价单",
         "1000股", "10.0", "10000", "1000股", "-", "0", "5.0", "全部成交", "2024-01-03 09:30:00"],
    ])
    _write(directory / "position.csv", [
        ["日期", "品种", "标的", "多空", "数量", "可用数量", "收盘价/结算价", "市值/价值",
         "盈亏/逐笔浮盈", "开仓均价", "持仓均价（期货）", "保证金", "当日盈亏", "今手数",
         "盈亏占比", "仓位占比"],
        ["2024-01-02", "", "Cash", "", "", "", "", "1000000", "", "", "", "", "", "", "", "0"],
        # 股票行比表头多一列"总资产"，位于仓位占比之前。
        ["2024-01-03", "股票", "平安银行(000001.XSHE)", "多", "1000股", "0股", "10.0",
         "10000", "0", "10.0", "-", "0", "0", "1000股", "1%", "1010000", "1%"],
        ["2024-01-03", "", "Cash", "", "", "", "", "1000000", "", "", "", "", "", "", "", "0"],
    ])


def test_import_ui_export_builds_canonical_golden(tmp_path: Path):
    source = tmp_path / "jq_export"
    _make_source(source)

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "case.yaml").write_text(
        "\n".join([
            "schema_version: 1",
            "name: synthetic_ui",
            "jq:",
            '  backtest_id: "123"',
            "  initial_cash: 1000000",
            '  benchmark: "000300.XSHG"',
            "svquant:",
            "  initial_capital: 1000000",
        ]),
        encoding="utf-8",
    )

    case = load_case(case_dir)
    summary = import_ui_export(source, case)

    assert summary["trading_days"] == 2
    assert summary["position_rows"] == 1
    assert summary["order_rows"] == 1
    assert summary["warnings"] == []

    golden = load_golden(case)
    assert list(golden.daily["date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert golden.daily["equity"].iloc[-1] == pytest.approx(1_010_000.0)
    assert golden.positions.iloc[0]["symbol"] == "000001.SZ"
    assert golden.positions.iloc[0]["quantity"] == 1000
    assert golden.positions.iloc[0]["closeable"] == 0
    assert golden.fills.iloc[0]["side"] == "buy"
    assert golden.fills.iloc[0]["price"] == pytest.approx(10.0)
    assert golden.fills.iloc[0]["commission"] == pytest.approx(5.0)
    assert golden.meta["initial_cash"] == pytest.approx(1_000_000.0)


def test_import_ui_export_warns_on_reported_return_mismatch(tmp_path: Path):
    source = tmp_path / "jq_export"
    _make_source(source)
    # 把收益概述改成 5%，与持仓总资产 1,010,000 不一致。
    _write(source / "result_1.csv", [
        ["时间", "基准收益", "策略收益"],
        ["2024-01-02 16:00:00", "0", "0"],
        ["2024-01-03 16:00:00", "0.5", "5.0"],
    ])

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "case.yaml").write_text(
        "\n".join([
            "schema_version: 1",
            "name: synthetic_ui",
            'jq: {backtest_id: "123", initial_cash: 1000000}',
            "svquant: {initial_capital: 1000000}",
        ]),
        encoding="utf-8",
    )

    summary = import_ui_export(source, load_case(case_dir))

    assert summary["warnings"]
    assert "不一致" in summary["warnings"][0]


def test_import_xlsx_derives_initial_cash_from_first_day_pnl(tmp_path: Path):
    source = tmp_path / "xlsx_export"
    source.mkdir()
    header_position = [
        "日期", "品种", "标的", "多空", "数量", "可用数量", "收盘价/结算价", "市值/价值",
        "盈亏/逐笔浮盈", "开仓均价", "持仓均价（期货）", "保证金", "当日盈亏", "今手数",
        "盈亏占比", "总资产", "仓位占比",
    ]
    cash_row = [
        "2024-01-02", "", "Cash", "", "", "", "", "1000", "", "", "", "", "", "", "",
        "0", "",
    ]
    stock_row = [
        "2024-01-02", "股票", "平安银行(000001.XSHE)", "多", "1000股", "0股",
        "200.0", "200000", "0", "200", "-", "0", "1000", "1000股", "0.5%",
        "201000", "0.995",
    ]
    pd.DataFrame([
        header_position,
        cash_row,
        stock_row,
    ]).to_excel(source / "position.xlsx", header=False, index=False)

    pd.DataFrame([
        ["时间", "基准收益", "策略收益", "当日盈利", "当日亏损"],
        ["2024-01-02 16:00:00", "0.1", "0.5", "1000", "0"],
        ["2024-01-03 16:00:00", "0.2", "1.0", "0", "0"],
    ]).to_excel(source / "result_1.xlsx", header=False, index=False)

    pd.DataFrame([
        ["日期", "委托时间", "品种", "标的", "交易类型", "下单类型", "成交数量", "成交价",
         "成交额", "委托数量", "委托价格", "平仓盈亏", "手续费", "状态", "最后更新时间"],
        ["2024-01-02", "09:30:00", "股票", "平安银行(000001.XSHE)", "买", "市价单",
         "1000股", "200.0", "200000", "1000股", "-", "0", "5.0", "全部成交",
         "2024-01-02 09:30:00"],
    ]).to_excel(source / "transaction.xlsx", header=False, index=False)

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "case.yaml").write_text(
        "\n".join([
            "schema_version: 1",
            "name: synthetic_xlsx",
            'jq: {backtest_id: "1", initial_cash: 1}',
            "svquant: {initial_capital: 1}",
        ]),
        encoding="utf-8",
    )

    case = load_case(case_dir)
    summary = import_ui_export(source, case)

    # 首日总资产 201,000 已含当日盈利 1,000，建仓前本金应为 200,000。
    assert summary["initial_cash"] == pytest.approx(200_000.0)
    assert summary["trading_days"] == 2
    golden = load_golden(case)
    assert golden.daily["equity"].iloc[0] == pytest.approx(201_000.0)
    assert golden.positions.iloc[0]["quantity"] == 1000
    assert golden.fills.iloc[0]["price"] == pytest.approx(200.0)
