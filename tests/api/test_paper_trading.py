"""模拟盘单日结算接口/服务测试。"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from app.config import configure_runtime_settings, default_runtime_settings
from app.schemas.paper_trading import PaperSettlementRequest
from app.services.data.engine import DataEngine
from app.services.paper_trading.errors import PaperTradingError
from app.services.paper_trading.settle import settle_paper_day


@pytest.fixture()
def paper_data_dir(tmp_path):
    dates = pd.bdate_range("2022-01-03", periods=6)
    symbols = ["A", "B", "C", "D"]
    # D 没有成交量，用来验证 NO_VWAP。
    closes = {
        "A": [10.0, 10.5, 11.0, 11.5, 12.0, 12.5],
        "B": [20.0, 20.5, 21.0, 21.5, 22.0, 22.5],
        "C": [30.0, 30.5, 31.0, 31.5, 32.0, 32.5],
        "D": [5.0, 5.0, 5.0, 5.0, 5.0, 5.0],
    }
    for name in ("open", "high", "low", "close"):
        frame = pd.DataFrame(
            {symbol: closes[symbol] for symbol in symbols}, index=dates
        )
        frame.index.name = "datetime"
        frame.reset_index().to_parquet(tmp_path / f"{name}.parquet", index=False)

    volume = pd.DataFrame(1000.0, index=dates, columns=symbols)
    volume["D"] = 0.0
    amount = pd.DataFrame(
        {symbol: [value * 1000.0 for value in closes[symbol]] for symbol in symbols},
        index=dates,
    )
    amount["D"] = 0.0
    for name, frame in (("volume", volume), ("amount", amount)):
        frame.index.name = "datetime"
        frame.reset_index().to_parquet(tmp_path / f"{name}.parquet", index=False)

    status = pd.DataFrame("非涨跌停", index=dates, columns=symbols)
    status.index.name = "datetime"
    status.reset_index().to_parquet(tmp_path / "up_and_down_status.parquet", index=False)

    listed = pd.DataFrame("在市", index=dates, columns=symbols)
    listed.index.name = "datetime"
    listed.reset_index().to_parquet(tmp_path / "listed_status.parquet", index=False)

    display_dir = tmp_path / "display"
    display_dir.mkdir()
    display_columns = pd.MultiIndex.from_product([symbols, ["open", "high", "low", "close"]])
    display = pd.DataFrame(index=dates, columns=display_columns, dtype=float)
    for symbol in symbols:
        for offset, field in ((1.0, "open"), (2.0, "high"), (0.5, "low"), (1.0, "close")):
            display[(symbol, field)] = [value + offset for value in closes[symbol]]
    display.index.name = "date"
    display.to_parquet(display_dir / "unadjusted_price.parquet")

    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    benchmark = pd.DataFrame(
        {
            "open": np.linspace(4000, 4100, len(dates)),
            "high": np.linspace(4010, 4110, len(dates)),
            "low": np.linspace(3990, 4090, len(dates)),
            "close": np.linspace(4000, 4100, len(dates)),
        },
        index=dates,
    )
    benchmark.to_parquet(benchmark_dir / "index_price_000300.SH.parquet")
    pd.DataFrame(
        {symbol: [0.25] for symbol in symbols}, index=[dates[0]]
    ).to_parquet(benchmark_dir / "index_weights_000300.SH.parquet")

    calendar_dir = tmp_path / "calendar"
    calendar_dir.mkdir()
    pd.DataFrame({"date": [date.isoformat() for date in dates], "is_trade_day": True}).to_parquet(
        calendar_dir / "calendar_all_a.parquet", index=False
    )

    configure_runtime_settings(
        replace(
            default_runtime_settings(),
            data_dir=tmp_path,
            cache_dir=tmp_path / "backtest_cache",
        )
    )
    engine = DataEngine()
    engine.load_parquet(tmp_path)
    yield engine
    engine.close()


def _request(**overrides) -> PaperSettlementRequest:
    payload = dict(
        settlement_date="2022-01-06",
        engine_mode="vectorized",
        strategy_code="",
        universe="CSI300",
        benchmark="CSI300",
        account={"cash": 100_000.0, "positions": []},
        execution={"history_days": 60, "commission_rate": 0.0003, "min_commission": 5.0},
    )
    payload.update(overrides)
    return PaperSettlementRequest(**payload)


VECTORIZED_HOLD_A = """
import pandas as pd

class AlwaysHold:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        weights = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        weights["A"] = 0.5
        return weights
"""


EVENT_STATEFUL = """
class Stateful:
    required_fields = frozenset({"open", "high", "low", "close"})
    required_market_state = frozenset({"up_and_down_status"})

    def initialize(self, context):
        context.state.last_day = None
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        today = context.current_dt.date()
        if today == context.state.last_day:
            return
        context.state.last_day = today
        if context.portfolio.quantity("A") <= 0:
            context.orders.target_quantity("A", 100)
        elif today.isoformat() == "2022-01-07":
            context.orders.close("A")
"""


def test_vectorized_settlement_fills_at_vwap_and_marks_with_unadjusted_close(paper_data_dir):
    response = settle_paper_day(
        _request(strategy_code=VECTORIZED_HOLD_A), paper_data_dir
    )

    assert response.signal_date == "2022-01-05"
    assert len(response.fills) == 1
    fill = response.fills[0]
    assert fill.symbol == "A"
    assert fill.price == pytest.approx(11.5)
    # 50% × 100000 / 11.5 → 4300 股（整手向下取整）
    assert fill.quantity == 4300
    assert response.next_account.cash == pytest.approx(
        100_000 - 4300 * 11.5 - fill.commission
    )
    # 未复权 close 是研究 close + 1。
    assert response.next_account.positions_value == pytest.approx(4300 * 12.5)
    assert response.next_account.total_equity == pytest.approx(
        response.next_account.cash + 4300 * 12.5
    )
    assert response.next_account.positions[0].available_quantity == 4300


def test_event_settlement_round_trips_state_and_sells_after_t_plus_one(paper_data_dir):
    first = settle_paper_day(
        _request(engine_mode="event", strategy_code=EVENT_STATEFUL),
        paper_data_dir,
    )
    assert [fill.symbol for fill in first.fills] == ["A"]
    assert first.next_account.positions[0].available_quantity == 100
    # state 里的 date 被编码成带类型标签的结构。
    assert first.strategy_state["last_day"]["__pt_type__"] == "date"

    second = settle_paper_day(
        _request(
            settlement_date="2022-01-07",
            engine_mode="event",
            strategy_code=EVENT_STATEFUL,
            account={
                "cash": first.next_account.cash,
                "positions": [
                    {
                        "symbol": item.symbol,
                        "quantity": item.quantity,
                        "available_quantity": item.available_quantity,
                        "average_cost": item.average_cost,
                    }
                    for item in first.next_account.positions
                ],
            },
            strategy_state=first.strategy_state,
            strategy_state_code_hash=first.code_hash,
        ),
        paper_data_dir,
    )
    assert [fill.symbol for fill in second.fills] == ["A"]
    assert second.fills[0].side == "sell"
    assert second.fills[0].stamp_tax > 0
    assert second.next_account.positions == []


def test_user_strategy_runtime_error_maps_to_blocking_422(paper_data_dir):
    """策略运行期异常必须转成 422 STRATEGY_CODE_INVALID，不能落成 500 让调用方重试。"""

    code = """
import pandas as pd

class Broken:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        return -self.total_mv
"""
    with pytest.raises(PaperTradingError) as excinfo:
        settle_paper_day(_request(strategy_code=code), paper_data_dir)
    assert excinfo.value.code == "STRATEGY_CODE_INVALID"
    assert excinfo.value.http_status == 422
    assert "AttributeError" in excinfo.value.message


def test_no_vwap_rejects_sell_and_keeps_position(paper_data_dir):
    code = """
import pandas as pd

class HoldNone:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        return pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
"""
    response = settle_paper_day(
        _request(
            strategy_code=code,
            account={
                "cash": 100_000.0,
                "positions": [
                    {
                        "symbol": "D",
                        "quantity": 100,
                        "available_quantity": 100,
                        "average_cost": 5.0,
                    }
                ],
            },
        ),
        paper_data_dir,
    )
    rejected = [order for order in response.orders if order.status == "rejected"]
    assert rejected and rejected[0].reason_code == "NO_VWAP"
    assert response.next_account.positions[0].symbol == "D"
    assert response.next_account.positions[0].quantity == 100
    assert "NO_VWAP:D" in response.warnings


def test_data_not_ready_and_non_trading_day(paper_data_dir):
    with pytest.raises(PaperTradingError) as exc:
        settle_paper_day(
            _request(settlement_date="2022-01-11", strategy_code=VECTORIZED_HOLD_A),
            paper_data_dir,
        )
    assert exc.value.code == "DATA_NOT_READY"

    with pytest.raises(PaperTradingError) as exc:
        settle_paper_day(
            _request(settlement_date="2022-01-08", strategy_code=VECTORIZED_HOLD_A),
            paper_data_dir,
        )
    assert exc.value.code == "NOT_A_TRADING_DAY"


def test_code_hash_and_state_hash_validation(paper_data_dir):
    with pytest.raises(PaperTradingError) as exc:
        settle_paper_day(
            _request(
                strategy_code=VECTORIZED_HOLD_A,
                expected_code_hash="sha256:deadbeef",
            ),
            paper_data_dir,
        )
    assert exc.value.code == "CODE_HASH_MISMATCH"

    with pytest.raises(PaperTradingError) as exc:
        settle_paper_day(
            _request(
                engine_mode="event",
                strategy_code=EVENT_STATEFUL,
                strategy_state={"x": 1},
            ),
            paper_data_dir,
        )
    assert exc.value.code == "STRATEGY_STATE_VERSION_MISMATCH"


def test_vectorized_rejects_negative_and_oversized_weights(paper_data_dir):
    negative = """
import pandas as pd

class Negative:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        weights = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        weights["A"] = -0.1
        return weights
"""
    with pytest.raises(PaperTradingError) as exc:
        settle_paper_day(_request(strategy_code=negative), paper_data_dir)
    assert exc.value.code == "UNSUPPORTED_PORTFOLIO"

    oversized = """
import pandas as pd

class Oversized:
    def __init__(self, close):
        self.close = close

    def generate_signals(self):
        weights = pd.DataFrame(0.0, index=self.close.index, columns=self.close.columns)
        weights["A"] = 0.7
        weights["B"] = 0.4
        return weights
"""
    with pytest.raises(PaperTradingError) as exc:
        settle_paper_day(_request(strategy_code=oversized), paper_data_dir)
    assert exc.value.code == "TARGET_WEIGHT_EXCEEDS_ONE"


def test_settlement_is_deterministic(paper_data_dir):
    first = settle_paper_day(_request(strategy_code=VECTORIZED_HOLD_A), paper_data_dir)
    second = settle_paper_day(_request(strategy_code=VECTORIZED_HOLD_A), paper_data_dir)
    assert first.model_dump() == second.model_dump()
