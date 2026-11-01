"""RE 对 svquant-data 发布视图的消费契约。

默认跳过：只有显式设置 ``RE_DATA_VIEW_ROOT``（物化视图目录）时才校验，
避免测试依赖本机 config.yaml 或真实数据。切换视图前用它做门禁：

    RE_DATA_VIEW_ROOT=/path/to/views/<id>/materialized \
        python -m pytest tests/data/test_data_contract.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

REQUIRED_DATASETS = {
    "daily_bars",
    "daily_bars_unadjusted",
    "minute_bars",
    "security_status_daily",
    "industry_daily",
    "valuation_daily",
    "universe",
    "index_membership",
    "index_price",
    "financial_income",
    "financial_balance",
    "financial_cashflow",
    "pershare_index",
    "per_share",
    "capital",
    "dividend_actions",
    "security_master",
    "calendar",
}

REQUIRED_FIELDS = {
    "daily_bars": {"open", "high", "low", "close", "volume", "amount", "turnover"},
    "daily_bars_unadjusted": {"open", "high", "low", "close"},
    "security_status_daily": {"listed_status", "up_and_down_status"},
    "industry_daily": {"sw_l1_code", "sw_l1_name", "ths_code"},
    "valuation_daily": {
        "total_mv",
        "float_mv",
        "pe_ttm",
        "pb_mrq",
        "ps_ttm",
        "dividend_yield_ttm",
    },
    "pershare_index": {
        "s_fa_eps_basic",
        "roa",
        "equity_roe",
        "gross_margin",
        "inc_net_profit_rate",
        "inc_revenue_rate",
        "operating_cf_to_revenue",
    },
}


def _view_root() -> Path:
    value = os.environ.get("RE_DATA_VIEW_ROOT")
    if not value:
        pytest.skip("未设置 RE_DATA_VIEW_ROOT；跳过发布视图契约校验")
    root = Path(value)
    if not root.is_dir():
        pytest.fail(f"RE_DATA_VIEW_ROOT 不存在: {root}")
    return root


def _manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.is_file():
        path = root.parent / "manifest.json"
    if not path.is_file():
        pytest.fail(f"物化视图缺少 manifest.json: {root}")
    return json.loads(path.read_text(encoding="utf-8"))


def test_view_exposes_contract_version_and_all_required_datasets():
    root = _view_root()
    manifest = _manifest(root)

    assert manifest.get("contract_version")
    datasets = manifest.get("datasets") or {}
    missing = REQUIRED_DATASETS - set(datasets)
    assert not missing, f"视图缺少 dataset: {sorted(missing)}"
    for name in REQUIRED_DATASETS:
        specs = datasets[name]
        assert specs.get("sha256"), f"{name} 缺少 sha256"
        path = specs.get("path")
        assert path and (root / path).exists(), f"{name} 路径不存在: {path}"


def test_view_fields_match_re_requirements():
    root = _view_root()
    contract_path = root / "data_contract.yaml"
    if not contract_path.is_file():
        contract_path = root.parent / "data_contract.yaml"
    if contract_path.is_file():
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        datasets = contract.get("datasets") or {}
    else:
        datasets = (_manifest(root).get("datasets") or {})
    for name, required in REQUIRED_FIELDS.items():
        spec = datasets.get(name) or {}
        fields = set(spec.get("fields") or spec.get("columns") or ())
        missing = required - fields
        assert not missing, f"{name} 缺少字段: {sorted(missing)}"


def test_compat_tree_is_present_for_joinquant_dialect():
    root = _view_root()
    free = root / "free"
    if not free.is_dir():
        free = root.parent / "free"
    compat = free / "compat"
    assert (compat / "3_financial_data" / "income").is_dir()
    assert (compat / "3_financial_data" / "pershare_index").is_dir()
    assert (
        compat / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
    ).is_file()
