# -*- coding: utf-8 -*-
"""FEAT-013：机器学习预测回测（ridge / walk-forward）。"""
from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.services.factor.ml_model import walk_forward

router = APIRouter()


@router.post("/factor/ml-backtest")
def ml_backtest(body: dict) -> dict:
    records = body.get("records")
    feature_cols = body.get("feature_cols")
    target_col = body.get("target_col")
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=400, detail="records 不能为空")
    if not isinstance(feature_cols, list) or not feature_cols:
        raise HTTPException(status_code=400, detail="feature_cols 不能为空")
    if not target_col:
        raise HTTPException(status_code=400, detail="target_col 不能为空")
    try:
        out = walk_forward(
            pd.DataFrame(records),
            feature_cols=[str(c) for c in feature_cols],
            target_col=str(target_col),
            train_window=int(body.get("train_window") or 252),
            alpha=float(body.get("alpha") or 1.0),
            top_quantile=float(body.get("top_quantile") or 0.2),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **out}
