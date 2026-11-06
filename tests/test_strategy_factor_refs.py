# -*- coding: utf-8 -*-
"""策略回测的内置因子引用（factor_refs）契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.data import Panel
from app.schemas.backtest import FactorRef, StrategyBacktestRequest
from app.services.strategy.backtest import _build_factor_refs_strategy


class _CloseFactor:
    name = "close_factor"

    def compute(self, panel):
        return panel.close


def _request(**kwargs):
    base = {
        "factor_refs": [{"library": "主因子库", "factor": "SV01_0001"}],
        "engine_mode": "vectorized",
        "strategy_dialect": "native",
    }
    base.update(kwargs)
    return StrategyBacktestRequest(**base)


def test_factor_refs_are_mutually_exclusive_with_strategy_source():
    with pytest.raises(ValidationError):
        _request(strategy_code="class S: pass")
    with pytest.raises(ValidationError):
        _request(strategy_id="saved")
    with pytest.raises(ValidationError):
        _request(strategy_version="v1")


def test_factor_refs_count_direction_and_duplicates():
    with pytest.raises(ValidationError):
        _request(factor_refs=[])
    with pytest.raises(ValidationError):
        _request(
            factor_refs=[
                {"library": "主因子库", "factor": f"SV01_000{i}"} for i in range(4)
            ]
        )
    with pytest.raises(ValidationError):
        _request(
            factor_refs=[
                {"library": "主因子库", "factor": "SV01_0001"},
                {"library": "主因子库", "factor": "SV01_0001", "direction": "negative"},
            ]
        )
    with pytest.raises(ValidationError):
        _request(
            factor_refs=[{"library": "主因子库", "factor": "SV01_0001", "direction": "up"}]
        )


def test_top_n_and_rebalance_days_only_apply_with_factor_refs():
    request = _request(top_n=3, rebalance_days=5)
    assert request.top_n == 3 and request.rebalance_days == 5
    with pytest.raises(ValidationError):
        StrategyBacktestRequest(
            strategy_code="class S:\n    def generate_signals(self):\n        pass",
            top_n=3,
        )


def test_factor_refs_reject_event_and_joinquant_modes():
    with pytest.raises(ValidationError):
        _request(engine_mode="event")
    with pytest.raises(ValidationError):
        _request(strategy_dialect="joinquant")


def test_composite_signal_uses_direction_and_top_n(monkeypatch):
    pytest.importorskip("pandas")
    import pandas as pd

    monkeypatch.setattr(
        "app.services.factor.loader.load_factor_from_spec",
        lambda **_kwargs: _CloseFactor(),
    )
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    close = pd.DataFrame(
        {"A": [1.0, 1.0, 1.0], "B": [2.0, 2.0, 2.0], "C": [3.0, 3.0, 3.0]},
        index=dates,
    )
    panel = Panel({"close": close}, symbols=list(close.columns))

    positive = _build_factor_refs_strategy(
        [FactorRef(library="主因子库", factor="SV01_0001")], top_n=1, rebalance_days=1
    )()
    signals = positive.generate_signals(panel)
    assert signals.target_weights.loc[dates[0], "C"] == pytest.approx(1.0)
    assert signals.target_weights.loc[dates[0], ["A", "B"]].fillna(0).sum() == 0

    negative = _build_factor_refs_strategy(
        [FactorRef(library="主因子库", factor="SV01_0001", direction="negative")],
        top_n=1,
        rebalance_days=1,
    )()
    signals = negative.generate_signals(panel)
    assert signals.target_weights.loc[dates[0], "A"] == pytest.approx(1.0)
