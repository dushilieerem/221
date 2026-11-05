"""收益曲线金标导入：只有 result_1.csv 时的 expected/ 产物。"""

from __future__ import annotations

import json

import pandas as pd

from app.services.parity.jq_ui_export import import_returns
from app.services.parity.loader import load_golden
from app.services.parity.schema import CaseSpec, Tolerances


def _case(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    return CaseSpec(
        case_id="returns_only",
        description="",
        path=case_dir,
        jq={"backtest_id": "t", "initial_cash": 1_000_000, "start": "2024-01-02"},
        svquant={},
        comparison={"layers": ["L0", "L4", "L5"]},
        tolerances=Tolerances(),
    )


def test_import_returns_writes_daily_and_meta(tmp_path):
    source = tmp_path / "result_1.csv"
    pd.DataFrame(
        {
            "时间": ["2024-01-02 16:00:00", "2024-01-03 16:00:00", "2024-01-04 16:00:00"],
            "基准收益": [0.0, 0.5, 0.4],
            "策略收益": [0.0, 1.0, -1.0],
            "当日盈利": [0.0, 10000.0, 0.0],
            "当日亏损": [0.0, 0.0, 20000.0],
            "当日买入": [0.0, 0.0, 0.0],
            "当日卖出": [0.0, 0.0, 0.0],
            "超额收益(%)": [0.0, 0.5, -1.4],
        }
    ).to_csv(source, index=False, encoding="gbk")
    case = _case(tmp_path)

    summary = import_returns(source, case)

    assert summary["mode"] == "returns_only"
    assert summary["trading_days"] == 3
    daily = pd.read_csv(tmp_path / "case" / "expected" / "daily.csv")
    assert list(daily.columns) == ["time", "returns"]
    # 累计 +1% 后回落到 -1%：第二日 +1%，第三日约 -1.98%。
    assert daily["returns"].iloc[0] == 0.0
    assert abs(daily["returns"].iloc[1] - 0.01) < 1e-9
    assert abs(daily["returns"].iloc[2] - (0.99 / 1.01 - 1)) < 1e-9

    golden = load_golden(case)
    assert len(golden.daily) == 3
    assert golden.positions.empty
    meta = json.loads((tmp_path / "case" / "expected" / "meta.json").read_text("utf-8"))
    assert meta["mode"] == "returns_only"
    assert meta["initial_cash"] == 1_000_000
