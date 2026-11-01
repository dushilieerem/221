"""CLI 冒烟测试：不启动服务、不依赖行情。"""

from __future__ import annotations

import json

from app.cli.joinquant_parity import main


def test_list_reports_cases(capsys):
    assert main(["list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(row["case"] == "fixed_basket_hold" for row in payload)


def test_validate_case_without_golden_returns_zero(capsys):
    assert main(["validate", "--case", "fixed_basket_hold", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["case"] == "fixed_basket_hold"
    assert "golden_available" in payload


def test_unknown_case_returns_two(capsys):
    assert main(["validate", "--case", "not_exists"]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert "error" in payload
