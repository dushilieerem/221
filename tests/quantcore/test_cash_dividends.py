# -*- coding: utf-8 -*-
"""事件引擎现金分红模型（joinquant 方言）。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.models.event import EventBacktestConfig
from app.services.strategy.event.engine import EventBacktestEngine


def _panels():
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    return {
        "open": pd.DataFrame({"A": [10.0, 9.5, 9.6]}, index=dates),
        "high": pd.DataFrame({"A": [10.0, 9.5, 9.6]}, index=dates),
        "low": pd.DataFrame({"A": [10.0, 9.5, 9.6]}, index=dates),
        "close": pd.DataFrame({"A": [10.0, 9.5, 9.6]}, index=dates),
    }


def _config(**overrides):
    overrides.setdefault("execution_timing", "next_open")
    overrides.setdefault("execution_price_source", "unadjusted")
    overrides.setdefault("company_action_model", "cash")
    return EventBacktestConfig(initial_capital=10_000, **overrides)


class _BuyDayOne:
    def initialize(self, context):
        def trade(ctx):
            if ctx.current_dt.date().isoformat() == "2024-01-02":
                ctx.orders.target_quantity("A", 100)

        context.schedule.daily(trade)


def test_cash_dividend_and_stock_bonus_are_credited():
    panels = _panels()
    panels["cash_dividend"] = pd.DataFrame({"A": [0.0, 0.5, 0.0]}, index=panels["close"].index)
    panels["stock_bonus"] = pd.DataFrame({"A": [0.0, 0.1, 0.0]}, index=panels["close"].index)

    result = EventBacktestEngine(
        panels,
        _config(),
        execution_panels={field: panels[field] for field in ("open", "close")},
    ).run(_BuyDayOne())

    snap = result.position_snapshots[1]
    assert snap.positions["A"] == 110
    # 10000 - 100×10 - 5 佣金 + 100×0.5 现金分红
    assert snap.cash == pytest.approx(9045.0)
    events = [e for e in result.events if e.get("type") == "CORPORATE_ACTION_ADJUSTMENT"]
    assert events[0]["cash_dividend"] == pytest.approx(50.0)
    assert events[0]["stock_bonus_shares"] == 10
    assert result.assumptions["corporate_action_adjustment"] == "cash_dividend_gross_no_tax"


def test_cash_dividend_withholds_joinquant_tax():
    panels = _panels()
    panels["cash_dividend"] = pd.DataFrame({"A": [0.0, 0.5, 0.0]}, index=panels["close"].index)

    result = EventBacktestEngine(
        panels,
        _config(dividend_tax_rate=0.2),
        execution_panels={field: panels[field] for field in ("open", "close")},
    ).run(_BuyDayOne())

    snap = result.position_snapshots[1]
    # 10000 - 100×10 - 5 佣金 + 100×0.5×(1-20%) 分红
    assert snap.cash == pytest.approx(9035.0)
    events = [e for e in result.events if e.get("type") == "CORPORATE_ACTION_ADJUSTMENT"]
    assert events[0]["cash_dividend"] == pytest.approx(40.0)
    assert events[0]["cash_dividend_gross"] == pytest.approx(50.0)
    assert events[0]["dividend_tax"] == pytest.approx(10.0)
    assert result.assumptions["corporate_action_adjustment"] == "cash_dividend_net_20pct_tax"


def test_adjusted_price_source_keeps_embedded_actions():
    panels = _panels()
    panels["cash_dividend"] = pd.DataFrame({"A": [0.0, 0.5, 0.0]}, index=panels["close"].index)

    result = EventBacktestEngine(
        panels,
        _config(execution_price_source="adjusted", company_action_model="cash"),
    ).run(_BuyDayOne())

    assert not [
        e for e in result.events if e.get("type") == "CORPORATE_ACTION_ADJUSTMENT"
    ]
    assert result.assumptions["corporate_action_adjustment"] == "embedded_in_adjusted_prices"
