# -*- coding: utf-8 -*-
"""比对聚宽「破净 v3」FF8 明细导出与本地兼容层的计算结果。

用法::

    python tools/freedata/compare_ff8.py /home/dev01/download/破净v3_FF8明细导出.csv

逐字段输出：8 个布尔项的一致率、22 个依赖原值的相对偏差，以及前若干个
不一致样例（date / code / 字段 / 聚宽 / 本地）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.strategy.compat.factors import FactorStore  # noqa: E402

DEPENDENCIES = [
    "roe", "roe_4", "roa", "roa_4",
    "total_profit", "total_profit_4", "financial_expense", "financial_expense_4",
    "total_current_assets", "total_current_assets_1",
    "total_current_assets_4", "total_current_assets_5",
    "total_non_current_liability", "total_non_current_liability_4",
    "total_non_current_assets", "total_non_current_assets_4",
    "total_current_liability", "total_current_liability_4",
    "gross_profit_margin", "gross_profit_margin_4",
    "operating_revenue", "operating_revenue_4",
]

COMPONENTS = [
    "DELTA_ROE", "DELTA_ROA1", "ROA2", "DELTA_ROA2",
    "DELTA_LEVER", "DELTA_LIQUID", "DELTA_MARGIN", "DELTA_CATURN",
]


def to_local(code: str) -> str:
    """聚宽代码 000001.XSHE -> 本地 000001.SZ。"""

    return (
        str(code)
        .replace(".XSHG", ".SH")
        .replace(".XSHE", ".SZ")
        .replace(".XBSE", ".BJ")
    )


def compute_components(values: dict[str, pd.Series]) -> pd.DataFrame:
    """按破净 v3 的 FF8Score 公式由依赖原值算出 8 个判断。"""

    def last(name):
        series = values.get(name)
        return series.astype(float) if series is not None else pd.Series(dtype=float)

    def ratio(a, b):
        return a / b.where(b > 0)

    avg_assets = (last("total_current_assets") + last("total_current_assets_1")) / 2.0
    avg_assets_old = (
        (last("total_current_assets_4") + last("total_current_assets_5")) / 2.0
    )
    r2 = ratio(last("total_profit") + last("financial_expense"), avg_assets)
    r2_old = ratio(
        last("total_profit_4") + last("financial_expense_4"), avg_assets_old
    )
    lever = ratio(last("total_non_current_liability"), last("total_non_current_assets"))
    lever_old = ratio(
        last("total_non_current_liability_4"), last("total_non_current_assets_4")
    )
    liquid = ratio(last("total_current_assets"), last("total_current_liability"))
    liquid_old = ratio(
        last("total_current_assets_4"), last("total_current_liability_4")
    )
    turnover = ratio(last("operating_revenue"), avg_assets)
    turnover_old = ratio(last("operating_revenue_4"), avg_assets_old)
    return pd.DataFrame({
        "DELTA_ROE": last("roe") - last("roe_4"),
        "DELTA_ROA1": last("roa") - last("roa_4"),
        "ROA2": r2,
        "DELTA_ROA2": r2 - r2_old,
        "DELTA_LEVER": lever_old - lever,
        "DELTA_LIQUID": liquid - liquid_old,
        "DELTA_MARGIN": last("gross_profit_margin") - last("gross_profit_margin_4"),
        "DELTA_CATURN": turnover - turnover_old,
    }).replace([np.inf, -np.inf], np.nan)


def main(path: str, root: str | None = None) -> int:
    jq = pd.read_csv(path)
    jq["code_local"] = jq["code"].map(to_local)
    store = FactorStore(root) if root else FactorStore()
    rows = []
    for date, part in jq.groupby("date"):
        codes = part["code_local"].tolist()
        values = store.dependency_series_many(DEPENDENCIES, codes, str(date))
        metrics = compute_components(values).reindex(codes)
        flags = metrics.gt(0)
        for code in codes:
            row = {"date": date, "code": code}
            for name in COMPONENTS:
                row[f"jq_{name}"] = part.loc[
                    part["code_local"] == code, name
                ].iloc[0]
                row[f"re_{name}"] = (
                    None if pd.isna(flags.at[code, name]) else int(bool(flags.at[code, name]))
                )
            for dep in DEPENDENCIES:
                if dep in part.columns:
                    row[f"jq_raw_{dep}"] = part.loc[
                        part["code_local"] == code, dep
                    ].iloc[0]
                    row[f"re_raw_{dep}"] = values[dep].get(code)
            rows.append(row)
    merged = pd.DataFrame(rows)
    print(f"样例 {len(merged)} 行，{merged['date'].nunique()} 个日期")

    print("\n布尔项一致率：")
    for name in COMPONENTS:
        pair = merged[[f"jq_{name}", f"re_{name}"]].dropna()
        same = (pair[f"jq_{name}"] == pair[f"re_{name}"]).mean() if len(pair) else float("nan")
        print(f"  {name:<14} n={len(pair):<6} same={same:.3f}")

    if "ff8" in merged.columns:
        recomputed = merged[[f"re_{name}" for name in COMPONENTS]].sum(axis=1, min_count=len(COMPONENTS))
        delta = (recomputed - merged["ff8"]).abs()
        print(f"\nFF8 总分：完全一致 {(delta == 0).mean():.3f}，"
              f"中位差 {delta.median():.2f}")

    print("\n依赖原值偏差（相对，中位/P95）：")
    for dep in DEPENDENCIES:
        left, right = f"jq_raw_{dep}", f"re_raw_{dep}"
        if left not in merged.columns or right not in merged.columns:
            continue
        pair = merged[[left, right]].apply(pd.to_numeric, errors="coerce").dropna()
        if pair.empty:
            continue
        rel = (pair[right] - pair[left]).abs() / pair[left].abs().clip(lower=1e-9)
        print(f"  {dep:<28} n={len(pair):<6} median={rel.median():.4f} p95={rel.quantile(0.95):.4f}")

    print("\n前 20 个布尔项不一致样例：")
    bad = []
    for name in COMPONENTS:
        left, right = f"jq_{name}", f"re_{name}"
        subset = merged.dropna(subset=[left, right])
        subset = subset[subset[left] != subset[right]]
        for _, row in subset.head(5).iterrows():
            bad.append((row["date"], row["code"], name, row[left], row[right]))
    for item in bad[:20]:
        print("  {} {} {} 聚宽={} 本地={}".format(*item))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
