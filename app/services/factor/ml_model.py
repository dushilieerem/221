# -*- coding: utf-8 -*-
"""FEAT-013：机器学习模型接入与预测回测（确定性基线）。

默认实现 ridge 回归 + walk-forward 截面预测 + Top 分位多头组合；
不依赖 sklearn，避免引入额外运行时依赖。
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    n, p = X.shape
    reg = float(alpha) * np.eye(p)
    reg[0, 0] = 0.0  # 不惩罚截距
    return np.linalg.solve(X.T @ X + reg, X.T @ y)


def _design(X: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(X)), X])


def walk_forward(
    df: pd.DataFrame,
    *,
    feature_cols: Sequence[str],
    target_col: str,
    train_window: int = 252,
    alpha: float = 1.0,
    top_quantile: float = 0.2,
) -> dict[str, Any]:
    """按日截面 walk-forward：只用当日之前 train_window 个交易日训练。"""
    if not feature_cols:
        raise ValueError("feature_cols 不能为空")
    if top_quantile <= 0 or top_quantile >= 1:
        raise ValueError("top_quantile 必须在 (0,1)")
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
    data = data.dropna(subset=list(feature_cols) + [target_col, "date"])
    if data.empty:
        raise ValueError("没有可用样本")
    dates = sorted(data["date"].unique())
    if len(dates) <= train_window:
        raise ValueError(f"样本不足：需要 > {train_window} 个交易日")
    records: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    for i in range(train_window, len(dates)):
        day = dates[i]
        train_dates = dates[max(0, i - train_window) : i]
        train = data[data["date"].isin(train_dates)]
        test = data[data["date"] == day]
        if len(train) < len(feature_cols) + 2 or test.empty:
            continue
        X = _design(train[list(feature_cols)].to_numpy(dtype=float))
        y = train[target_col].to_numpy(dtype=float)
        beta = _ridge_fit(X, y, alpha)
        pred = _design(test[list(feature_cols)].to_numpy(dtype=float)) @ beta
        test = test.assign(pred=pred)
        q = max(1, int(len(test) * top_quantile))
        top = test.nlargest(q, "pred")
        port = float(top[target_col].mean())
        records.append({"date": day, "ret": port})
        for _, row in top.iterrows():
            predictions.append({"date": day, "symbol": row.get("symbol"), "pred": float(row["pred"]), "target": float(row[target_col])})
    if not records:
        raise ValueError("没有可回测的预测")
    rets = np.array([r["ret"] for r in records], dtype=float)
    equity = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(equity)
    drawdown = float(((equity / peak) - 1.0).min())
    ann = float((equity[-1] ** (252.0 / len(rets))) - 1.0) if len(rets) else 0.0
    vol = float(rets.std(ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0.0
    sharpe = float(ann / vol) if vol > 0 else 0.0
    hit = float((rets > 0).mean())
    preds = pd.DataFrame(predictions)
    ic = float(preds.groupby("date").apply(lambda g: g["pred"].corr(g["target"]) if len(g) > 2 else np.nan).mean()) if not preds.empty else 0.0
    return {
        "model": "ridge",
        "params": {"train_window": train_window, "alpha": alpha, "top_quantile": top_quantile},
        "daily_returns": records,
        "equity": [float(x) for x in equity],
        "metrics": {
            "annual_return": ann,
            "volatility": vol,
            "sharpe": sharpe,
            "max_drawdown": drawdown,
            "win_rate": hit,
            "mean_ic": ic,
            "days": len(rets),
            "picks": len(predictions),
        },
    }


__all__ = ["walk_forward"]
