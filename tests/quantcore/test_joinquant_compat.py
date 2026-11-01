"""聚宽兼容层：模块级策略、调度、当前行情与日线读取。"""

from __future__ import annotations

import pandas as pd
import pytest

from app.models.event import EventBacktestConfig
from app.services.strategy.compat import load_joinquant_strategy
from app.services.strategy.event.engine import EventBacktestEngine

DATES = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
SYMBOL = "000001.SZ"

STRATEGY = """
def initialize(context):
    g.done = False
    run_daily(buy, time='09:31')

def buy(context):
    if g.done:
        return
    data = get_current_data()['000001.SZ']
    if data.last_price == data.last_price:
        g.done = True
        order_target_value('000001.SZ', 10000)
"""

PROBE = """
def initialize(context):
    g.rows = None
    g.last = None
    run_daily(probe, time='09:31')

def probe(context):
    if g.rows is not None:
        return
    frame = get_price('000001.SZ', count=2, end_date=context.current_dt, fields=['close'])
    g.rows = len(frame)
    g.last = float(frame['close'].iloc[-1])
    history = attribute_history('000001.SZ', 1, fields=['close'])
    g.history_rows = len(history)
"""


def _panels():
    adjusted = {
        field: pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0]}, index=DATES)
        for field in ("open", "high", "low", "close")
    }
    adjusted["close"] = pd.DataFrame({SYMBOL: [10.5, 10.5, 10.5]}, index=DATES)
    unadjusted = {
        "open": pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0]}, index=DATES),
        "close": pd.DataFrame({SYMBOL: [10.5, 10.5, 10.5]}, index=DATES),
    }
    return adjusted, unadjusted


def _engine():
    adjusted, unadjusted = _panels()
    return EventBacktestEngine(
        adjusted,
        EventBacktestConfig(
            initial_capital=100_000.0,
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            slippage_rate=0.0,
            execution_price_source="unadjusted",
            timed_execution=True,
        ),
        execution_panels=unadjusted,
        intraday_price=lambda date, time, symbol: 10.1 if time == "09:31" else None,
    )


def test_module_level_strategy_runs_through_timed_engine():
    strategy = load_joinquant_strategy(STRATEGY)
    result = _engine().run(strategy)

    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.timestamp.strftime("%H:%M") == "09:31"
    assert fill.price == pytest.approx(10.1)
    assert fill.quantity == 900


def test_get_price_and_attribute_history():
    strategy = load_joinquant_strategy(PROBE)
    _engine().run(strategy)
    runtime = strategy.runtime

    assert runtime.g.rows == 1
    assert runtime.g.last == pytest.approx(10.5)
    assert runtime.g.history_rows == 1


def test_bigquant_sources_are_rejected():
    with pytest.raises(ValueError, match="BigQuant"):
        load_joinquant_strategy("from bigmodule import M\n")


FUNDAMENTAL_PROBE = """
def initialize(context):
    g.rows = None
    g.as_of = None
    run_daily(probe, time='09:31')

def probe(context):
    if context.previous_date is None or g.rows is not None:
        return
    g.as_of = str(context.previous_date)
    g.rows = get_fundamentals(
        query(income.operating_revenue, income.net_profit),
        date=context.previous_date,
    )
"""


class _StubStore:
    def __init__(self):
        self.as_of = None

    def query(self, q, *, as_of):
        self.as_of = as_of
        return pd.DataFrame({"code": ["000001.SZ"], "operating_revenue": [1.0], "net_profit": [0.5]})


def test_get_fundamentals_uses_previous_date_and_pit_store():
    store = _StubStore()
    strategy = load_joinquant_strategy(FUNDAMENTAL_PROBE, fundamentals=store)
    _engine().run(strategy)

    assert store.as_of == "2024-01-02"
    assert strategy.runtime.g.as_of == "2024-01-02"
    assert list(strategy.runtime.g.rows.columns) == ["code", "operating_revenue", "net_profit"]


def test_query_builder_conditions():
    from app.services.strategy.compat.query import evaluate, query as build_query, valuation

    q = (
        build_query(valuation.code, valuation.pb_ratio)
        .filter(valuation.code.in_(["000001.SZ"]), valuation.pb_ratio > 0)
        .order_by(valuation.pb_ratio.asc())
        .limit(10)
    )
    frame = pd.DataFrame({
        "valuation.code": ["000001.SZ", "600000.SH"],
        "valuation.pb_ratio": [0.5, -1.0],
    })
    filtered = frame[evaluate(frame, q.where)]
    assert filtered["valuation.code"].tolist() == ["000001.SZ"]
    assert q.limit_count == 10


FACTOR_PROBE = """
def initialize(context):
    g.data = None
    run_daily(probe, time='09:31')

def probe(context):
    if g.data is not None or context.previous_date is None:
        return
    g.data = get_factor_values(
        ['000001.SZ', '600000.SH'],
        ['earnings_growth', 'sales_growth'],
        end_date=context.previous_date,
        count=1,
    )
"""


class _StubFactors:
    def compute(self, factor, symbols, as_of):
        return pd.Series({symbol: 0.25 for symbol in symbols})


def test_get_factor_values_returns_date_by_symbol_frame():
    strategy = load_joinquant_strategy(FACTOR_PROBE, factors=_StubFactors())
    _engine().run(strategy)

    data = strategy.runtime.g.data
    assert set(data) == {"earnings_growth", "sales_growth"}
    frame = data["earnings_growth"]
    assert frame.shape == (1, 2)
    assert list(frame.columns) == ["000001.SZ", "600000.SH"]
    assert str(frame.index[0].date()) == "2024-01-02"
    assert frame.iloc[0].tolist() == [0.25, 0.25]


def test_index_membership_uses_point_in_time_snapshot(tmp_path):
    from app.services.strategy.compat.index import IndexMembership

    frame = pd.DataFrame(
        {"000001.SZ": [1.0, None], "600000.SH": [None, 0.5]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    target = tmp_path / "index" / "index_weights"
    target.mkdir(parents=True)
    frame.to_parquet(target / "index_weights_000300.SH.parquet")

    store = IndexMembership(tmp_path)
    assert store.members("000300.XSHG", "2024-01-02") == ["000001.SZ"]
    # 非快照日取之前最近一个快照，不使用未来成分。
    assert store.members("000300.XSHG", "2024-01-05") == ["600000.SH"]


def test_industry_store_reads_ths_field_by_date(tmp_path):
    from app.services.strategy.compat.industry import IndustryStore

    columns = pd.MultiIndex.from_tuples(
        [
            ("000001.SZ", "ths_the_sw_industry_code_stock"),
            ("600000.SH", "ths_the_sw_industry_code_stock"),
        ]
    )
    frame = pd.DataFrame(
        [["S48", "S22"]],
        index=pd.to_datetime(["2024-01-02"]),
        columns=columns,
    )
    frame.index.name = "date"
    target = tmp_path / "date_sequence"
    target.mkdir()
    frame.to_parquet(target / "date_sequence.parquet")

    store = IndustryStore(tmp_path)
    assert store.industries(["000001.SZ"], "2024-01-02") == {
        "000001.SZ": {"sw_l1": {"industry_code": "S48", "industry_name": "S48"}}
    }
    assert store.industries(["000001.SZ"], "2024-01-03") == {}


INDEX_PRICE_PROBE = """
def initialize(context):
    g.last = None
    run_daily(probe, time='09:31')

def probe(context):
    if g.last is not None:
        return
    frame = get_price('000300.XSHG', count=2, fields=['close'])
    g.last = float(frame['close'].iloc[-1])
"""


def test_get_price_routes_index_codes(tmp_path):
    from app.services.strategy.compat.index import IndexPriceStore

    target = tmp_path / "index" / "index_price"
    target.mkdir(parents=True)
    frame = pd.DataFrame(
        {"open": [100.0, 101.0], "high": [101.0, 102.0],
         "low": [99.0, 100.0], "close": [100.5, 101.5]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    frame.index.name = "date"
    frame.to_parquet(target / "index_price_000300.SH.parquet")

    strategy = load_joinquant_strategy(
        INDEX_PRICE_PROBE, index_prices=IndexPriceStore(tmp_path)
    )
    _engine().run(strategy)

    assert strategy.runtime.g.last == pytest.approx(100.5)


def test_industry_store_prefers_derived_industry_daily(tmp_path):
    from app.services.strategy.compat.industry import IndustryStore

    derived = tmp_path / "derived" / "industry_daily" / "year=2024"
    derived.mkdir(parents=True)
    pd.DataFrame(
        {
            "date": ["2024-01-31", "2024-01-31"],
            "symbol": ["000001.SZ", "600000.SH"],
            "sw_l1_code": [pd.NA, "801780"],
            "sw_l1_name": [pd.NA, "银行"],
            "ths_code": ["S48", "S48"],
        }
    ).to_parquet(derived / "part.parquet")
    (tmp_path / "ifind").mkdir()

    store = IndustryStore(tmp_path / "ifind")
    result = store.industries(["000001.SZ", "600000.SH"], "2024-02-01")

    assert result["000001.SZ"]["sw_l1"]["industry_code"] == "S48"
    assert result["600000.SH"]["sw_l1"] == {
        "industry_code": "801780",
        "industry_name": "银行",
    }


def test_calc_factors_runs_factor_with_pit_dependencies(tmp_path):
    from app.services.strategy.compat.factors import FactorStore
    from app.services.strategy.compat.financial_cache import clear_cache
    from app.services.strategy.compat.runtime import Factor, JoinQuantRuntime

    clear_cache()
    directory = tmp_path / "3_financial_data" / "pershare_index"
    directory.mkdir(parents=True)
    periods = ["20231231", "20240331", "20240630", "20240930", "20241231"]
    pd.DataFrame(
        {
            "Symbol": [SYMBOL] * len(periods),
            "m_timetag": periods,
            "m_anntime": ["20240401", "20240430", "20240830", "20241030", "20250401"],
            "equity_roe": [10.0, 11.0, 12.0, 13.0, 14.0],
        }
    ).to_parquet(directory / f"{SYMBOL}.parquet")

    class RoeChange(Factor):
        name = "RoeChange"
        dependencies = ("roe", "roe_4")

        def calc(self, data):
            return data["roe"].iloc[-1] - data["roe_4"].iloc[-1]

    runtime = JoinQuantRuntime(
        factors=FactorStore(root=tmp_path, data_engine=object())
    )
    result = runtime.calc_factors([SYMBOL], [RoeChange()], end_date="2025-06-01")
    clear_cache()

    frame = result["RoeChange"]
    assert frame.shape == (1, 1)
    assert frame.iloc[0, 0] == pytest.approx(4.0)


class _StubIndex:
    def members(self, code, date):
        return ["000001.SZ", "600000.SH"]


WEEKLY_PROBE = """
def initialize(context):
    g.days = []
    run_weekly(mark, weekday=1, time='09:31')

def mark(context):
    g.days.append(str(context.current_dt.date()))
"""


def test_run_weekly_uses_nth_trading_day_of_week():
    from app.models.event import EventBacktestConfig

    dates = pd.to_datetime(
        ["2024-01-02", "2024-01-08", "2024-01-09", "2024-01-15"]
    )
    adjusted = {
        field: pd.DataFrame({SYMBOL: [10.0] * len(dates)}, index=dates)
        for field in ("open", "high", "low", "close")
    }
    unadjusted = {
        "open": pd.DataFrame({SYMBOL: [10.0] * len(dates)}, index=dates),
        "close": pd.DataFrame({SYMBOL: [10.0] * len(dates)}, index=dates),
    }
    engine = EventBacktestEngine(
        adjusted,
        EventBacktestConfig(
            initial_capital=100_000.0,
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            slippage_rate=0.0,
            execution_price_source="unadjusted",
            timed_execution=True,
        ),
        execution_panels=unadjusted,
    )
    strategy = load_joinquant_strategy(WEEKLY_PROBE)
    engine.run(strategy)

    # 起始周（周二开始）按本周第一个交易日触发；周一正常触发，周二不触发。
    assert strategy.runtime.g.days == ["2024-01-02", "2024-01-08", "2024-01-15"]


ORDERS_PROBE = """
def initialize(context):
    g.filled = None
    g.open_count = None
    g.cancel_after = None
    run_daily(buy, time='09:31')
    run_daily(inspect_orders, time='14:50')
    run_daily(cancel_late, time='15:10')

def buy(context):
    if g.filled is not None:
        return
    order_target_value('000001.SZ', 10000)

def inspect_orders(context):
    if g.filled is not None:
        return
    orders = get_orders()
    g.open_count = len(get_open_orders())
    g.filled = sum(order.filled for order in orders.values())

def cancel_late(context):
    if g.cancel_after is not None:
        return
    target = order_target('000001.SZ', 200)
    before = len(get_open_orders())
    cancel_order(target)
    g.cancel_after = (before, len(get_open_orders()))
"""


def test_order_queries_and_cancel():
    strategy = load_joinquant_strategy(ORDERS_PROBE)
    _engine().run(strategy)

    assert strategy.runtime.g.filled == 900
    assert strategy.runtime.g.open_count == 0
    assert strategy.runtime.g.cancel_after == (1, 0)


LIMIT_PROBE = """
def initialize(context):
    g.row = None
    run_daily(probe, time='09:31')

def probe(context):
    if g.row is not None or context.previous_date is None:
        return
    frame = get_price('000001.SZ', count=1, fields=['close', 'high_limit'],
                      panel=False, fq=None)
    g.row = frame.iloc[-1].to_dict()
"""


def test_get_price_long_format_with_high_limit():
    from app.models.event import EventBacktestConfig

    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    adjusted = {
        field: pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0]}, index=dates)
        for field in ("open", "high", "low", "close")
    }
    unadjusted = {
        "open": pd.DataFrame({SYMBOL: [10.0, 10.0, 10.0]}, index=dates),
        "close": pd.DataFrame({SYMBOL: [10.5, 10.5, 10.5]}, index=dates),
    }
    engine = EventBacktestEngine(
        adjusted,
        EventBacktestConfig(
            initial_capital=100_000.0,
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            slippage_rate=0.0,
            execution_price_source="unadjusted",
            timed_execution=True,
        ),
        execution_panels=unadjusted,
    )
    strategy = load_joinquant_strategy(LIMIT_PROBE)
    engine.run(strategy)

    row = strategy.runtime.g.row
    assert row["code"] == SYMBOL
    assert row["close"] == pytest.approx(10.5)
    assert row["high_limit"] == pytest.approx(11.55)


MONTH_END_PROBE = """
def initialize(context):
    g.days = []
    run_monthly(mark, -1, time='14:55')

def mark(context):
    g.days.append(str(context.current_dt.date()))
"""


def test_run_monthly_negative_monthday_uses_last_trading_day():
    from app.models.event import EventBacktestConfig

    dates = pd.to_datetime(["2024-01-29", "2024-01-30", "2024-01-31"])
    adjusted = {
        field: pd.DataFrame({SYMBOL: [10.0] * len(dates)}, index=dates)
        for field in ("open", "high", "low", "close")
    }
    unadjusted = {
        "open": pd.DataFrame({SYMBOL: [10.0] * len(dates)}, index=dates),
        "close": pd.DataFrame({SYMBOL: [10.0] * len(dates)}, index=dates),
    }
    strategy = load_joinquant_strategy(MONTH_END_PROBE)
    EventBacktestEngine(
        adjusted,
        EventBacktestConfig(
            initial_capital=100_000.0,
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
            slippage_rate=0.0,
            execution_price_source="unadjusted",
            timed_execution=True,
        ),
        execution_panels=unadjusted,
    ).run(strategy)

    assert strategy.runtime.g.days == ["2024-01-31"]


def test_get_index_stocks_delegates_to_membership():
    strategy = load_joinquant_strategy(
        "def initialize(context):\n    g.stocks = get_index_stocks('000300.XSHG')\n",
        index=_StubIndex(),
    )
    _engine().run(strategy)
    assert strategy.runtime.g.stocks == ["000001.SZ", "600000.SH"]
