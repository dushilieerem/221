"""财务表缓存：PIT 过滤、最新报告期与聚宽字段表达式。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.strategy.compat.factors import FactorStore
from app.services.strategy.compat.financial_cache import clear_cache
from app.services.strategy.compat.fundamentals import FundamentalStore
from app.services.strategy.compat.query import income, query, valuation

SYMBOL = "600000.SH"


@pytest.fixture()
def data_root(tmp_path):
    clear_cache()
    income_dir = tmp_path / "3_financial_data" / "income"
    income_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": [SYMBOL, SYMBOL, SYMBOL],
            "m_timetag": ["20231231", "20240630", "20241231"],
            "m_anntime": ["20240401", "20240820", "20250330"],
            "revenue": [100.0, 60.0, 120.0],
            "tot_profit": [20.0, 12.0, 24.0],
            "net_profit_incl_min_int_inc": [10.0, 6.0, 12.0],
            "deducted_net_profit": [8.0, 5.0, 10.0],
        }
    ).to_parquet(income_dir / f"{SYMBOL}.parquet")

    valuation_dir = tmp_path / "5_technical_derived" / "valuation" / "dt=20240628"
    valuation_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "symbol": [SYMBOL],
            "total_mv": [1000.0],
            "float_mv": [800.0],
            "pe_ttm": [12.0],
            "pb": [1.2],
            "ps_ttm": [2.0],
        }
    ).to_parquet(valuation_dir / "data.parquet")
    yield tmp_path
    clear_cache()


def test_latest_financial_respects_announcement_date(data_root):
    store = FactorStore(root=data_root, data_engine=object())

    before = store._latest_financial("income", ["revenue"], "2024-07-01")
    after = store._latest_financial("income", ["revenue"], "2024-09-01")

    assert before.set_index("Symbol").loc[SYMBOL, "revenue"] == 100.0
    assert after.set_index("Symbol").loc[SYMBOL, "revenue"] == 60.0


def test_fundamentals_expression_and_valuation_merge(data_root):
    store = FundamentalStore(root=data_root)
    q = query(
        income.operating_revenue,
        income.adjusted_profit_to_profit,
        valuation.pb_ratio,
    )

    frame = store.query(q, as_of="2024-06-30")

    assert frame.loc[0, "code"] == SYMBOL
    assert frame.loc[0, "operating_revenue"] == 100.0
    assert frame.loc[0, "adjusted_profit_to_profit"] == pytest.approx(80.0)
    assert frame.loc[0, "pb_ratio"] == pytest.approx(1.2)


def test_growth_from_annual_matches_polyfit(data_root):
    rng = np.random.default_rng(7)
    rows = [
        {"Symbol": f"{index:06d}.SZ", "year": str(year), "value": rng.normal()}
        for index in range(50)
        for year in range(2015, 2025)
    ]
    frame = pd.DataFrame(rows)
    frame.loc[frame.index[::13], "value"] = np.nan

    store = FactorStore(root=data_root, data_engine=object())
    actual = store._growth_from_annual(frame)

    expected = {}
    for symbol, group in frame.groupby("Symbol"):
        values = group.sort_values("year")["value"].astype(float).dropna()
        if len(values) < 4:
            expected[symbol] = np.nan
            continue
        values = values.tail(5).to_numpy()
        slope = float(np.polyfit(np.arange(len(values)), values, 1)[0])
        denominator = float(np.mean(np.abs(values)))
        expected[symbol] = slope / denominator if denominator > 0 else np.nan

    assert actual.to_dict() == pytest.approx(expected, nan_ok=True)
