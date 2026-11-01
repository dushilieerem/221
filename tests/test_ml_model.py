# -*- coding: utf-8 -*-
"""FEAT-013：ridge walk-forward 预测回测基线。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.factor.ml_model import walk_forward


def _panel(days: int = 80, symbols: int = 30, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        date = (pd.Timestamp("2024-01-01") + pd.Timedelta(days=d)).strftime("%Y-%m-%d")
        for s in range(symbols):
            x1 = rng.normal()
            x2 = rng.normal()
            target = 0.01 * x1 + 0.002 * x2 + rng.normal(0, 0.005)
            rows.append({"date": date, "symbol": f"S{s}", "x1": x1, "x2": x2, "target": target})
    return pd.DataFrame(rows)


def test_walk_forward_learns_signal() -> None:
    out = walk_forward(
        _panel(),
        feature_cols=["x1", "x2"],
        target_col="target",
        train_window=40,
        alpha=1.0,
        top_quantile=0.2,
    )
    assert out["model"] == "ridge"
    assert out["metrics"]["days"] > 0
    assert out["metrics"]["annual_return"] > 0
    assert out["metrics"]["mean_ic"] > 0
    assert len(out["equity"]) == out["metrics"]["days"]


def test_validation() -> None:
    with pytest.raises(ValueError):
        walk_forward(_panel(), feature_cols=[], target_col="target")
    with pytest.raises(ValueError):
        walk_forward(_panel(days=30), feature_cols=["x1"], target_col="target", train_window=40)
    with pytest.raises(ValueError):
        walk_forward(_panel(), feature_cols=["x1"], target_col="target", top_quantile=1.0)


def test_route_contract() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    panel = _panel(days=50, symbols=10)
    res = TestClient(app).post(
        "/api/factor/ml-backtest",
        json={
            "records": panel.to_dict("records"),
            "feature_cols": ["x1", "x2"],
            "target_col": "target",
            "train_window": 30,
            "top_quantile": 0.3,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["model"] == "ridge"
