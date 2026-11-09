"""逐日横截面比较对齐后的因子值；不使用收益曲线相关。"""

import numpy as np
import pandas as pd


def compare_panels(candidate, reference, min_pairs):
    """逐日同股票比较因子值；失败时返回可核验的归因，不填 0。"""
    if (
        not candidate.index.is_unique
        or not candidate.columns.is_unique
        or not reference.index.is_unique
        or not reference.columns.is_unique
    ):
        raise ValueError("Duplicate panel axes")
    a = candidate.replace([np.inf, -np.inf], np.nan)
    b = reference.reindex(index=a.index, columns=a.columns).replace(
        [np.inf, -np.inf], np.nan
    )
    paired = a.notna() & b.notna()
    counts = paired.sum(axis=1)
    ar = a.where(paired).rank(axis=1, method="average")
    br = b.where(paired).rank(axis=1, method="average")
    correlations = (
        ar.corrwith(br, axis=1)
        .where(counts >= min_pairs)
        .replace([np.inf, -np.inf], np.nan)
    )
    valid = correlations.notna()
    total = int(valid.sum())
    candidate_counts = a.notna().sum(axis=1)
    denominator = int(candidate_counts[valid].sum())
    pairs_sum = int(counts[valid].sum())
    daily_coverage = counts[valid] / candidate_counts[valid]
    diagnosis = None
    if not total:
        if int(b.notna().to_numpy().sum()) == 0:
            diagnosis = "reference_all_nan"
        elif int((b.notna().sum(axis=1) >= min_pairs).sum()) == 0:
            diagnosis = "reference_finite_values_insufficient"
        elif all(int(row.dropna().nunique()) <= 1 for _, row in b.iterrows()):
            diagnosis = "reference_constant"
        elif int(paired.to_numpy().sum()) == 0:
            diagnosis = "no_paired_observations"
        else:
            diagnosis = "no_valid_cross_sections"
    return {
        "status": "succeeded" if total else "failed",
        "error": (
            None
            if total
            else {
                "reference_all_nan": "参考因子值全部缺失",
                "reference_finite_values_insufficient": "参考因子每日有效值少于 min_pairs 只股票",
                "reference_constant": "参考因子每日无横截面差异（常量）",
                "no_paired_observations": "候选与参考无共同股票日",
                "no_valid_cross_sections": "没有满足最少配对且非常量的有效截面",
            }[diagnosis]
        ),
        "diagnosis": diagnosis,
        "mean_abs_spearman": float(correlations[valid].abs().mean()) if total else None,
        "trading_days_total": len(a.index),
        "valid_days": total,
        "candidate_days": int((candidate_counts >= min_pairs).sum()),
        "min_pairs": int(counts[valid].min()) if total else 0,
        "coverage": pairs_sum / denominator if denominator else 0.0,
        "pairs_total": pairs_sum,
        "coverage_pooled": pairs_sum / denominator if denominator else 0.0,
        "coverage_by_day_quantiles": (
            {str(q): float(daily_coverage.quantile(q)) for q in (0, 0.25, 0.5, 0.75, 1)}
            if total
            else {}
        ),
        "date_coverage": (total / len(a.index)) if len(a.index) else 0.0,
        "coverage_note": "coverage 以有效相关日期为条件；date_coverage = valid_days / 候选交易日数，仅作诊断",
        "paired_stock_days": pairs_sum,
        "candidate_stock_days_on_valid_days": denominator,
        "series": [
            {
                "date": str(pd.Timestamp(day).date()),
                "spearman": float(rho),
                "abs_spearman": abs(float(rho)),
                "pairs": int(counts.loc[day]),
                "candidate_count": int(candidate_counts.loc[day]),
            }
            for day, rho in correlations[valid].items()
        ],
    }
