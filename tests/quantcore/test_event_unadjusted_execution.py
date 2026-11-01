"""不复权撮合模式：成交价、整手、公司行为缩放。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.models.event import EventBacktestConfig
from app.services.strategy.event.engine import EventBacktestEngine

DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
SYMBOL = "000001.SZ"


class BuyOnce:
    def initialize(self, context):
        context.state.calls = 0
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        context.state.calls += 1
        if context.state.calls == 2:
            context.orders.target_value(SYMBOL, 100_000.0)


def _panels(adjusted_close, unadjusted_close):
    adjusted = {
        field: pd.DataFrame({SYMBOL: adjusted_close}, index=DATES)
        for field in ("open", "high", "low", "close")
    }
    unadjusted = {
        field: pd.DataFrame({SYMBOL: unadjusted_close}, index=DATES)
        for field in ("open", "close")
    }
    return adjusted, unadjusted


def _config(**overrides):
    values = dict(
        initial_capital=110_000.0,
        commission_rate=0.001,
        min_commission=0.0,
        stamp_tax_rate=0.0,
        slippage_rate=0.0,
        execution_timing="next_open",
        execution_price_source="unadjusted",
    )
    values.update(overrides)
    return EventBacktestConfig(**values)


def test_unadjusted_fill_uses_real_price_and_lot():
    adjusted, unadjusted = _panels(
        [10.0, 10.0, 10.0, 10.0],
        [10.0, 10.0, 9.090909, 9.090909],
    )
    engine = EventBacktestEngine(
        adjusted, _config(), execution_panels=unadjusted
    )
    result = engine.run(BuyOnce())

    fill = result.fills[0]
    assert fill.price == pytest.approx(10.0)
    assert fill.quantity == 10_000
    assert fill.commission == pytest.approx(100.0)
    assert result.position_snapshots[1].positions[SYMBOL] == 10_000


def test_unadjusted_share_scaling_keeps_adjusted_return():
    adjusted, unadjusted = _panels(
        [10.0, 10.0, 10.0, 10.0],
        [10.0, 10.0, 9.090909, 9.090909],
    )
    engine = EventBacktestEngine(
        adjusted, _config(), execution_panels=unadjusted
    )
    result = engine.run(BuyOnce())

    # 除权日股数按 1.1 缩放，持仓市值保持不变。
    assert result.position_snapshots[2].positions[SYMBOL] == 11_000
    equity = result.equity_curve
    assert equity.iloc[1] == pytest.approx(109_900.0)
    assert equity.iloc[2] == pytest.approx(109_900.0, rel=1e-9)
    # 建仓日只承担一笔佣金；除权日及之后收益为 0。
    assert result.returns.iloc[0] == pytest.approx(-100.0 / 110_000.0)
    assert result.returns.iloc[1:].abs().max() < 1e-9

    adjustments = [e for e in result.events if e["type"] == "CORPORATE_ACTION_ADJUSTMENT"]
    assert len(adjustments) == 1
    assert adjustments[0]["ratio"] == pytest.approx(1.1, rel=1e-6)
    assert adjustments[0]["cash_in_lieu"] == pytest.approx(0.0, abs=0.01)


def test_adjusted_mode_keeps_existing_behaviour():
    adjusted, _ = _panels(
        [10.0, 10.0, 10.0, 10.0],
        [10.0, 10.0, 9.090909, 9.090909],
    )
    engine = EventBacktestEngine(
        adjusted,
        _config(execution_price_source="adjusted"),
    )
    result = engine.run(BuyOnce())
    assert result.fills[0].price == pytest.approx(10.0)
    assert result.fills[0].quantity == 10_000


def test_unadjusted_mode_requires_execution_panels():
    adjusted, _ = _panels([10.0] * 4, [10.0] * 4)
    with pytest.raises(ValueError, match="execution_panels"):
        EventBacktestEngine(adjusted, _config())
