"""对账测试共享夹具；文件名不以 test_ 开头，不会被 pytest 收集。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.services.parity.loader import GoldenRun
from app.services.parity.normalizer import CanonicalRun
from app.services.parity.schema import CaseSpec, Tolerances

DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
SYMBOL_A = "000001.SZ"
SYMBOL_B = "600000.SH"


def make_case(price_basis: str = "hfq", **comparison) -> CaseSpec:
    return CaseSpec(
        case_id="synthetic",
        description="合成用例",
        path=Path("."),
        jq={
            "backtest_id": "synthetic",
            "initial_cash": 1_000_000.0,
            "benchmark": "000300.XSHG",
            "options": {
                "commission": 0.0003,
                "min_commission": 5.0,
                "stamp_tax": 0.0005,
                "slippage_rate": 0.0,
            },
        },
        svquant={
            "initial_capital": 1_000_000.0,
            "benchmark": "CSI300",
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "stamp_tax": 0.0005,
            "slippage_rate": 0.0,
        },
        comparison={"price_basis": price_basis, **comparison},
        tolerances=Tolerances(),
    )


def make_runs() -> tuple[GoldenRun, CanonicalRun]:
    """构造一组两侧完全一致的最小结果。"""

    daily = pd.DataFrame({
        "date": DATES,
        "equity": [1_000_000.0, 1_010_000.0, 1_020_000.0, 1_030_000.0],
        "cash": [50_000.0, 50_000.0, 50_000.0, 50_000.0],
        "returns": [0.0, 0.01, 1_020_000.0 / 1_010_000.0 - 1.0, 1_030_000.0 / 1_020_000.0 - 1.0],
    })
    positions = pd.DataFrame({
        "date": [DATES[1], DATES[2], DATES[2], DATES[3], DATES[3]],
        "symbol": [SYMBOL_A, SYMBOL_A, SYMBOL_B, SYMBOL_A, SYMBOL_B],
        "quantity": [1000, 1000, 2000, 1000, 2000],
        "closeable": [1000, 1000, 2000, 1000, 2000],
    })
    fills = pd.DataFrame({
        "date": [DATES[1], DATES[2]],
        "symbol": [SYMBOL_A, SYMBOL_B],
        "side": ["buy", "buy"],
        "quantity": [1000, 2000],
        "price": [10.0, 5.0],
        "commission": [5.0, 5.0],
        "stamp_tax": [0.0, 0.0],
    })
    golden = GoldenRun(daily=daily.copy(), positions=positions.copy(), fills=fills.copy(), meta={"initial_cash": 1_000_000.0})
    actual = CanonicalRun(
        daily=daily.copy(),
        positions=positions.copy(),
        fills=fills.copy(),
        meta={"metrics": {}},
    )
    return golden, actual
