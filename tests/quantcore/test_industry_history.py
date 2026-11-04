"""行业 PIT 覆盖层的回归测试。"""

from __future__ import annotations

import pandas as pd

from app.services.strategy.compat.industry import IndustryStore


def _history(tmp_path):
    path = tmp_path / "industry_history.csv"
    pd.DataFrame({
        "date": ["2019-12-31", "2019-12-31", "2021-12-31", "2021-12-31"],
        "symbol": ["000001.SZ", "600519.SH", "000001.SZ", "600519.SH"],
        "sw_l1_code": ["801780.SI", "801120.SI", "801780.SI", "801120.SI"],
        "sw_l1_name": ["银行", "食品饮料", "银行", "食品饮料"],
        "sw_l2_code": ["801781.SI", "801125.SI", "801782.SI", "801125.SI"],
        "sw_l2_name": ["银行Ⅱ", "白酒Ⅱ", "股份制银行Ⅱ", "白酒Ⅱ"],
    }).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def test_history_overrides_latest_only(tmp_path):
    store = IndustryStore(tmp_path / "ifind", history_path=_history(tmp_path))

    old = store.industries(["000001.SZ"], "2020-06-30")["000001.SZ"]
    new = store.industries(["000001.SZ"], "2022-06-30")["000001.SZ"]

    assert old["sw_l1"]["industry_code"] == "801780.SI"
    assert old["sw_l2"]["industry_code"] == "801781.SI"
    assert new["sw_l2"]["industry_code"] == "801782.SI"


def test_history_snapshot_is_forward_filled_within_range(tmp_path):
    store = IndustryStore(tmp_path / "ifind", history_path=_history(tmp_path))

    # 2020-06-30 落在 2019-12-31 与 2021-12-31 之间，取较早的快照。
    result = store.industries(["600519.SH"], "2020-06-30")
    assert result["600519.SH"]["sw_l1"]["industry_name"] == "食品饮料"


def test_history_before_first_snapshot_returns_empty(tmp_path):
    store = IndustryStore(tmp_path / "ifind", history_path=_history(tmp_path))
    assert store.industries(["000001.SZ"], "2018-06-30") == {}


def test_history_stocks_filters_by_level1(tmp_path):
    store = IndustryStore(tmp_path / "ifind", history_path=_history(tmp_path))

    assert store.stocks("801780", "2020-06-30") == ["000001.SZ"]
    assert store.stocks("801120", "2020-06-30") == ["600519.SH"]


def test_history_file_must_have_required_columns(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame({"date": ["2020-01-01"], "symbol": ["000001.SZ"]}).to_csv(
        path, index=False
    )
    try:
        IndustryStore(tmp_path / "ifind", history_path=path)
    except ValueError as exc:
        assert "缺少列" in str(exc)
    else:  # pragma: no cover - 明确失败
        raise AssertionError("缺列时应抛 ValueError")
