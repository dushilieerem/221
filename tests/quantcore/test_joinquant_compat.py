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


EXTRAS_PROBE = """
def initialize(context):
    g.st = None
    run_daily(probe, time='09:31')

def probe(context):
    if g.st is not None or context.previous_date is None:
        return
    g.st = get_extras('is_st', ['000001.SZ', '600000.SH'], count=1,
                      end_date=context.previous_date)
"""


def test_get_extras_is_st_reads_listed_status_panel():
    adjusted, unadjusted = _panels()
    adjusted["listed_status"] = pd.DataFrame(
        {
            "000001.SZ": ["ST", "ST", None],
            "600000.SH": [None, None, None],
        },
        index=DATES,
    )
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
        intraday_price=lambda date, time, symbol: 10.1 if time == "09:31" else None,
    )
    strategy = load_joinquant_strategy(EXTRAS_PROBE)
    engine.run(strategy)

    frame = strategy.runtime.g.st
    assert list(frame.columns) == ["000001.SZ", "600000.SH"]
    assert frame.iloc[0].tolist() == [True, False]


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


def test_index_membership_falls_back_to_derived_long_table(tmp_path):
    """iFinD 未覆盖的指数读发布视图的 derived/index_membership 长表。"""
    from app.services.strategy.compat.index import IndexMembership

    data_dir = tmp_path / "ifind"
    data_dir.mkdir()
    derived = tmp_path / "derived" / "index_membership" / "year=2024"
    derived.mkdir(parents=True)
    pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-02"],
            "index_code": ["399317.SZ", "399317.SZ", "399317.SZ", "000300.SH"],
            "symbol": ["000001.SZ", "600000.SH", "000001.SZ", "600000.SH"],
            # 权重为空也是成分，不能按权重过滤。
            "weight": [None, None, None, 0.5],
        }
    ).to_parquet(derived / "part.parquet")

    store = IndexMembership(data_dir)
    assert store.members("399317.XSHE", "2024-01-02") == ["000001.SZ", "600000.SH"]
    assert store.members("399317.SZ", "2024-01-03") == ["000001.SZ"]


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


def test_compat_log_handles_message_without_placeholders(caplog):
    """聚宽策略常见 ``log.warn("文本", a, b)``，不能让 logging 抛格式错误。"""
    import logging

    from app.services.strategy.compat.runtime import _Log

    logger = _Log()
    with caplog.at_level(logging.WARNING, logger="joinquant_compat"):
        logger.warn("skip sell at low limit", "002921.SZ", "联诚精密")
        logger.warn("仓位 %.1f%%", 19.8)
    assert "skip sell at low limit 002921.SZ 联诚精密" in caplog.text
    assert "仓位 19.8%" in caplog.text


def test_history_fundamentals_returns_single_quarter_cashflow(tmp_path):
    """聚宽 get_history_fundamentals 返回单季度流量；源表是年内累计。"""
    from app.services.strategy.compat.financial_cache import clear_cache
    from app.services.strategy.compat.fundamentals import FundamentalStore
    from app.services.strategy.compat.query import cash_flow

    clear_cache()
    root = tmp_path / "3_financial_data" / "cashflow"
    root.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": ["600000.SH"] * 4,
            "m_timetag": ["20240331", "20240630", "20240930", "20241231"],
            "m_anntime": ["20240425", "20240825", "20241025", "20250425"],
            # 年内累计：Q1 1、H1 3、Q3 6、全年 10。
            "net_cash_flows_oper_act": [1.0, 3.0, 6.0, 10.0],
        }
    ).to_parquet(root / "600000.SH.parquet")

    store = FundamentalStore(root=tmp_path)
    frame = store.history(
        [cash_flow.net_operate_cash_flow],
        ["600000.SH"],
        watch_date="2025-04-30",
        count=4,
    )

    assert frame["statDate"].tolist() == [
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    ]
    assert frame["net_operate_cash_flow"].tolist() == [1.0, 2.0, 3.0, 4.0]
    clear_cache()


def test_finance_income_statement_returns_cumulative_fields(tmp_path):
    from app.services.strategy.compat.finance import FinanceNamespace
    from app.services.strategy.compat.financial_cache import clear_cache
    from app.services.strategy.compat.query import Field, Query

    clear_cache()
    income_dir = tmp_path / "3_financial_data" / "income"
    income_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "Symbol": ["600000.SH"],
            "m_timetag": ["20241231"],
            "m_anntime": ["20250425"],
            "revenue": [10.0],
            "total_operating_cost": [7.0],
            "less_taxes_surcharges_ops": [0.5],
            "sale_expense": [0.5],
            "less_gerl_admin_exp": [1.0],
            "research_expenses": [0.2],
            "financial_expense": [0.1],
            "less_impair_loss_assets": [float("nan")],
            "net_profit_excl_min_int_inc": [3.0],
        }
    ).to_parquet(income_dir / "600000.SH.parquet")

    namespace = FinanceNamespace(tmp_path)
    table = "finance.STK_INCOME_STATEMENT"
    q = Query(
        fields=[
            Field(table, "code"),
            Field(table, "total_operating_revenue"),
            Field(table, "operating_cost"),
            Field(table, "np_parent_company_owners"),
        ]
    ).filter(Field(table, "code").in_(["600000.SH"]))

    frame = namespace.run_query(q)

    row = frame.iloc[0]
    assert row["total_operating_revenue"] == pytest.approx(10.0)
    # 营业总成本 7 减去税金/销售/管理/研发/财务（减值缺失按 0）。
    assert row["operating_cost"] == pytest.approx(7.0 - 0.5 - 0.5 - 1.0 - 0.2 - 0.1)
    assert row["np_parent_company_owners"] == pytest.approx(3.0)
    clear_cache()


def test_industry_store_lists_stocks_by_sw_code(tmp_path):
    """get_industry_stocks 按申万一级代码取成分，忽略 .SI 后缀。"""
    from app.services.strategy.compat.industry import IndustryStore

    data_dir = tmp_path / "ifind"
    data_dir.mkdir()
    derived = tmp_path / "derived" / "industry_daily" / "year=2024"
    derived.mkdir(parents=True)
    pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "symbol": ["600519.SH", "000858.SZ", "000001.SZ"],
            "sw_l1_code": ["801120.SI", "801120.SI", "801780.SI"],
            "sw_l1_name": ["食品饮料", "食品饮料", "银行"],
            "ths_code": ["S34", "S34", "S48"],
        }
    ).to_parquet(derived / "part.parquet")

    store = IndustryStore(data_dir)

    assert store.stocks("801120", "2024-01-02") == ["600519.SH", "000858.SZ"]
    assert store.stocks("801780.SI", "2024-01-02") == ["000001.SZ"]
    assert store.stocks("801999", "2024-01-02") == []
    # 非快照日取之前最近一个快照。
    assert store.stocks("801120", "2024-03-01") == ["600519.SH", "000858.SZ"]


def test_strip_imports_keeps_try_block_valid():
    """被移除的导入位于 try 块时，不能留下空块。"""
    from app.services.strategy.compat.loader import strip_joinquant_imports

    source = """
def f():
    try:
        from jqfactor import get_factor_values
    except Exception:
        return None
    return get_factor_values
"""

    cleaned = strip_joinquant_imports(source)

    compile(cleaned, "<strategy>", "exec")
    assert "pass  # 聚宽导入由兼容层注入" in cleaned


PAUSED_PROBE = """
def initialize(context):
    g.paused = None
    run_daily(probe, time='09:31')

def probe(context):
    if g.paused is not None:
        return
    frame = get_price('000001.SZ', start_date='2024-01-02', end_date='2024-01-04',
                      fields=['close', 'paused'], panel=False)
    g.paused = frame['paused'].tolist()
"""


def test_get_price_exposes_paused_flag():
    adjusted, unadjusted = _panels()
    adjusted["up_and_down_status"] = pd.DataFrame(
        {SYMBOL: ["非涨跌停", "停牌", "非涨跌停"]}, index=DATES
    )
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
        intraday_price=lambda date, time, symbol: 10.1 if time == "09:31" else None,
    )
    strategy = load_joinquant_strategy(PAUSED_PROBE)
    engine.run(strategy)

    assert strategy.runtime.g.paused == [0, 1, 0]


def test_query_supports_field_to_field_comparison():
    """聚宽 filter 里允许 valuation.code == balance.code。"""
    from app.services.strategy.compat.query import (
        Field, Query, evaluate,
    )

    q = Query(fields=[Field("valuation", "code")]).filter(
        Field("valuation", "code") == Field("balance", "code")
    )
    frame = pd.DataFrame(
        {
            "valuation.code": ["000001.SZ", "600000.SH"],
            "balance.code": ["000001.SZ", "000002.SZ"],
        }
    )
    filtered = frame[evaluate(frame, q.where)]
    assert filtered["valuation.code"].tolist() == ["000001.SZ"]


def _income_fixture(tmp_path):
    """两只股票、十个季度；revenue 是单季，成本费用是年内累计（与生产一致）。"""
    from app.services.strategy.compat.financial_cache import clear_cache

    clear_cache()
    income_dir = tmp_path / "3_financial_data" / "income"
    income_dir.mkdir(parents=True)
    periods = [
        "20220331", "20220630", "20220930", "20221231",
        "20230331", "20230630", "20230930", "20231231",
        "20240331", "20240630",
    ]
    years = [2022, 2022, 2022, 2022, 2023, 2023, 2023, 2023, 2024, 2024]
    quarters = [1, 2, 3, 4, 1, 2, 3, 4, 1, 2]
    ann = [f"{year}{month}" for year, month in zip(
        [2022, 2022, 2022, 2023, 2023, 2023, 2023, 2024, 2024, 2024],
        ["0425", "0825", "1025", "0425", "0425", "0825", "1025", "0425", "0425", "0825"],
    )]
    rows = []
    expected = {}
    for symbol, base in (("600000.SH", 10.0), ("600001.SH", 20.0)):
        running = {}
        cumulative_cost = []
        for index, period in enumerate(periods):
            single = base * (1 + index * 0.1)
            running[years[index]] = running.get(years[index], 0.0) + single
            cumulative = running[years[index]]
            # 费用占营收 20%，营业成本固定为营收的 60%，毛利率恒为 40%。
            cumulative_cost.append(cumulative * 0.6)
            rows.append(
                {
                    "Symbol": symbol,
                    "m_timetag": period,
                    "m_anntime": ann[index],
                    "revenue": single,
                    "total_operating_cost": running[years[index]] * 0.8,
                    "less_taxes_surcharges_ops": cumulative * 0.1,
                    "sale_expense": cumulative * 0.05,
                    "less_gerl_admin_exp": cumulative * 0.05,
                    "research_expenses": 0.0,
                    "financial_expense": 0.0,
                    "less_impair_loss_assets": float("nan"),
                    "net_profit_excl_min_int_inc": base * 0.2,
                    "deducted_net_profit": base * 0.15,
                }
            )
        # 最新一期 20240630：TTM = 本期累计 + 上年年报 - 上年同期累计。
        expected[symbol] = cumulative_cost[9] + cumulative_cost[7] - cumulative_cost[5]
    for symbol in ("600000.SH", "600001.SH"):
        pd.DataFrame(rows).query("Symbol == @symbol").to_parquet(
            income_dir / f"{symbol}.parquet"
        )
    return tmp_path, expected


def test_vectorized_financial_factors_match_formulas(tmp_path):
    from app.services.strategy.compat.factors import FactorStore
    from app.services.strategy.compat.financial_cache import clear_cache

    root, expected_cost = _income_fixture(tmp_path)
    store = FactorStore(root=root, data_engine=object())

    cost = store.compute("operating_cost_ttm", ["600000.SH"], as_of="2024-08-30")
    assert cost["600000.SH"] == pytest.approx(expected_cost["600000.SH"])

    non_recurring = store.compute(
        "non_recurring_gain_loss", ["600000.SH"], as_of="2024-08-30"
    )
    assert non_recurring["600000.SH"] == pytest.approx(10.0 * 0.05)

    degm = store.compute("DEGM", ["600000.SH"], as_of="2024-08-30")
    # 毛利率恒为 40%，同比为 0。
    assert degm["600000.SH"] == pytest.approx(0.0, abs=1e-12)
    clear_cache()
