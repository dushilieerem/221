"""聚宽 finance.run_query：分红事件的 PIT 股本换算。"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.services.strategy.compat.finance import FinanceNamespace
from app.services.strategy.compat.financial_cache import clear_cache
from app.services.strategy.compat.query import query

SYMBOL = "600000.SH"


@pytest.fixture()
def data_root(tmp_path):
    clear_cache()
    dividend_dir = tmp_path / "3_financial_data" / "dividend_factors"
    dividend_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": [SYMBOL, SYMBOL],
            "time": ["2020-06-01", "2021-06-01"],
            "type": ["1", "1"],
            "interest": [0.0, 10.0],
            "allotPrice": [0.0, 0.0],
            "stockBonus": [0.0, 0.0],
            "allotment": [0.0, 0.0],
        }
    ).to_parquet(dividend_dir / f"{SYMBOL}.parquet")

    capital_dir = tmp_path / "3_financial_data" / "capital"
    capital_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": [SYMBOL],
            "m_timetag": ["20210331"],
            "m_anntime": ["20210401"],
            "total_capital": [1_000_000_000.0],
        }
    ).to_parquet(capital_dir / f"{SYMBOL}.parquet")
    yield tmp_path
    clear_cache()


def test_run_query_converts_cash_dividend_to_ten_thousand_yuan(data_root):
    finance = FinanceNamespace(root=data_root)
    q = (
        query(
            finance.STK_XR_XD.code,
            finance.STK_XR_XD.a_registration_date,
            finance.STK_XR_XD.bonus_amount_rmb,
        )
        .filter(
            finance.STK_XR_XD.code.in_([SYMBOL]),
            finance.STK_XR_XD.a_registration_date > dt.date(2021, 1, 1),
            finance.STK_XR_XD.a_registration_date <= dt.date(2021, 12, 31),
        )
        .limit(10)
    )

    frame = finance.run_query(q)

    assert len(frame) == 1
    assert frame.iloc[0]["code"] == SYMBOL
    assert frame.iloc[0]["bonus_amount_rmb"] == pytest.approx(1e5)
