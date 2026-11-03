# -*- coding: utf-8 -*-
"""
红利低波 + 红利成长 v1 完善实盘版（聚宽 Python3）

设计原则：
1. 保留 v1 的两套选股逻辑：
   A 池：流通市值、流动性、三年 ROE 质量、连续分红、股息率、周低波。
   B 池：PE、扣非 ROE、营收增长、利润增长、过去一年股息率。
2. 不把 v2 的“流通市值不低于 50 亿元”强加给 B 池，尽量保留 v1 的收益弹性。
3. 所有历史股票池、基本面、行业、分红和价格数据统一截止 previous_date。
4. 数据缺失或接口异常时不绕过筛选、不沿用旧信号，也不误清仓；当月保留原持仓。
5. A/B 各 5 个名额，每名额基础权重 9.9%；候选不足时，仅在合格股票间补分配，
   单股目标权重不超过 19.8%，仍无法分配的资金留作现金。
6. 最多 10 只股票、每个申万一级行业最多 5 只；按真实持仓与未成交买单检查。
7. 按真实 closeable_amount 卖出，支持 T+1、停牌、跌停、部分成交和跨日待卖。
8. 买入只使用真实可用资金，不预支待卖资金；支持首三个交易日和日内多次补单，
   未成交买单会及时撤销，避免重复委托或长期挂单。
9. 单股实际仓位超过 20% 时尝试减至 19.8%；涨跌停、停牌或 T+1 时可能暂时超限，
   后续定时任务会继续处理。
"""

from jqdata import *
import builtins as py
import datetime as dt
import traceback
import numpy as np
import pandas as pd


# ======================== 可调参数 ========================

BENCHMARK_CODE = '000015.XSHG'

STOCK_SLIPPAGE = 0.002
OPEN_COMMISSION = 0.0001
CLOSE_COMMISSION = 0.0001
MIN_COMMISSION = 5
STOCK_CLOSE_TAX = 0.0005

POOL_TARGET_NUM = 5
POOL_BUDGET = 0.50
INVEST_RATIO = 0.99
CASH_RESERVE = 0.01

MAX_POSITIONS = 10
MAX_INDUSTRY = 5
MAX_TARGET_WEIGHT = 0.198
MAX_ACTUAL_WEIGHT = 0.20

MIN_MARKET_CAP_A = 50.0
NEW_STOCK_DAYS = 365
ROE_YEARS = 3
DIVIDEND_YIELD_MIN = 0.03
VOLATILITY_TRADING_DAYS = 780
MIN_COMPLETE_WEEKS = 50

# 为满足组合行业约束，两个池各保留更深的候选名单用于递补。
CANDIDATE_MULTIPLIER = 3

# 月初卖出受阻时，允许在前三个交易日继续使用当月有效信号补买。
BUY_WINDOW_TRADING_DAYS = 3


# ======================== 初始化与每日状态 ========================

def initialize(context):
    set_option('avoid_future_data', True)
    set_option('use_real_price', True)
    set_benchmark(BENCHMARK_CODE)
    set_slippage(PriceRelatedSlippage(STOCK_SLIPPAGE), type='stock')
    set_order_cost(OrderCost(
        open_tax=0,
        close_tax=STOCK_CLOSE_TAX,
        open_commission=OPEN_COMMISSION,
        close_commission=CLOSE_COMMISSION,
        close_today_commission=0,
        min_commission=MIN_COMMISSION
    ), type='stock')

    log.set_level('order', 'error')
    log.set_level('history', 'error')
    log.set_level('system', 'error')

    g.today = None
    g.calendar_ok = False
    g.is_month_first = False

    # 月度信号。只有 signal_month 等于当前月份时才允许使用。
    g.target_weights = {}
    g.target_industry = {}
    g.signal_month = None
    g.selection_date = None
    g.selection_ok = False
    g.buy_dates = set()
    g.rebalance_initialized_month = None

    # 跨日保存；不能在每日初始化时清空。
    g.pending_sells = {}
    g.blocked_buys_month = set()

    # 每日状态。
    g.high_limit_list = set()
    g.hold_data_ok = False
    g.industry_ok = False
    g.industry = {}
    g.tracked_orders = {}
    g.bought_today = set()
    g.sold_today = set()
    g.buy_blocked_today = False

    run_daily(prepare_stock_list, time='09:00')
    run_daily(get_stock_list, time='09:01')
    run_daily(trade, time='09:30')
    run_daily(cancel_buy_remainders, time='09:31')
    run_daily(check_limit_up, time='10:00')
    run_daily(retry_rebalance, time='10:05')
    run_daily(cancel_buy_remainders, time='10:06')
    run_daily(retry_rebalance, time='14:45')
    run_daily(cancel_buy_remainders, time='14:46')
    run_daily(retry_sells, time='14:50')
    run_daily(end_of_day, time='after_close')


def _month_key(day):
    return '%04d-%02d' % (day.year, day.month)


def _as_date(value):
    return pd.Timestamp(value).date()


def _ensure_day(context):
    today = context.current_dt.date()
    if g.today == today:
        return

    g.today = today
    g.calendar_ok = False
    g.is_month_first = False
    g.high_limit_list = set()
    g.hold_data_ok = False
    g.industry_ok = False
    g.industry = {}
    g.tracked_orders = {}
    g.bought_today = set()
    g.sold_today = set()
    g.buy_blocked_today = False

    try:
        days = get_trade_days(start_date=today.replace(day=1), end_date=today)
        if days is None or len(days) == 0:
            raise ValueError('交易日历为空')
        g.is_month_first = (_as_date(days[0]) == today)
        g.calendar_ok = True
    except Exception as e:
        log.error('交易日历异常，今日不新增仓位：%s', e)

    # 新月份到来后，旧月份的信号不能继续买入；待卖任务仍然保留。
    if g.signal_month is not None and g.signal_month != _month_key(today):
        g.selection_ok = False
        g.buy_dates = set()
        g.blocked_buys_month = set()


def _holdings(context):
    return dict((code, pos) for code, pos in context.portfolio.positions.items()
                if pos.total_amount > 0)


def _quote(code):
    try:
        data = get_current_data()[code]
        price = float(data.last_price)
        if not np.isfinite(price) or price <= 0:
            return None
        return data
    except Exception as e:
        log.warn('实时行情不可用 %s：%s', code, e)
        return None


def _stable_rank(series, ascending=False):
    values = pd.to_numeric(series, errors='coerce')
    values = values.replace([np.inf, -np.inf], np.nan).dropna()
    return values.sort_index().sort_values(ascending=ascending, kind='mergesort')


# ======================== 委托、成交与跨日待卖 ========================

def _sync_orders(context):
    try:
        orders = get_orders()
        open_orders = get_open_orders()
        if orders is None or open_orders is None:
            raise ValueError('委托查询返回 None')

        for order_id, meta in list(g.tracked_orders.items()):
            order_obj = orders.get(order_id)
            if order_obj is not None and order_obj.filled > 0:
                if meta['buy']:
                    g.bought_today.add(meta['code'])
                else:
                    g.sold_today.add(meta['code'])

        held = _holdings(context)
        open_codes = set(o.security for o in open_orders.values())

        for code, task in list(g.pending_sells.items()):
            if code in open_codes:
                continue
            position = held.get(code)
            if position is None:
                del g.pending_sells[code]
                continue
            if task['weight'] > 0:
                data = _quote(code)
                if data is not None:
                    actual_value = position.total_amount * data.last_price
                    target_value = context.portfolio.total_value * task['weight']
                    if actual_value <= target_value:
                        del g.pending_sells[code]

        return open_orders
    except Exception as e:
        log.error('委托状态查询异常，暂停新委托：%s', e)
        g.buy_blocked_today = True
        return None


def _track_order(order_obj, code, is_buy):
    if order_obj is None:
        log.warn('委托未受理：%s %s', code, '买入' if is_buy else '卖出')
        return

    g.tracked_orders[order_obj.order_id] = {
        'code': code,
        'buy': bool(is_buy)
    }
    if order_obj.filled > 0:
        if is_buy:
            g.bought_today.add(code)
        else:
            g.sold_today.add(code)

    log.info('委托已提交：%s %s，数量=%s，已成交=%s；提交不代表全部成交',
             code, '买入' if is_buy else '卖出',
             order_obj.amount, order_obj.filled)


def _cancel_open_buys(context, code=None):
    open_orders = _sync_orders(context)
    if open_orders is None:
        return

    for order_obj in list(open_orders.values()):
        if not order_obj.is_buy:
            continue
        if code is not None and order_obj.security != code:
            continue
        try:
            cancel_order(order_obj)
        except Exception as e:
            g.buy_blocked_today = True
            log.error('撤销买单失败 %s：%s', order_obj.security, e)

    _sync_orders(context)


def _queue_sell(context, code, target_weight, reason,
                protect_high_limit=False, block_rebuy=False):
    target_weight = float(py.max(0.0, target_weight))
    old = g.pending_sells.get(code)

    if old is None:
        g.pending_sells[code] = {
            'weight': target_weight,
            'reason': reason,
            'protect': bool(protect_high_limit),
            'block_rebuy': bool(block_rebuy)
        }
    else:
        # 更低的目标仓位优先；风险退出不能被普通任务覆盖。
        if target_weight < old['weight']:
            old['weight'] = target_weight
            old['reason'] = reason
        old['protect'] = old['protect'] and bool(protect_high_limit)
        old['block_rebuy'] = old.get('block_rebuy', False) or bool(block_rebuy)

    if block_rebuy:
        g.blocked_buys_month.add(code)

    _cancel_open_buys(context, code)


def _process_pending_sells(context):
    open_orders = _sync_orders(context)
    if open_orders is None:
        return

    open_codes = set(o.security for o in open_orders.values())
    held = _holdings(context)

    for code, task in list(g.pending_sells.items()):
        position = held.get(code)
        if position is None:
            del g.pending_sells[code]
            continue
        if code in open_codes:
            continue

        data = _quote(code)
        if data is None or data.paused:
            log.info('待卖 %s：无行情或停牌，任务保留', code)
            continue
        if position.closeable_amount <= 0:
            log.info('待卖 %s：T+1 暂无可卖数量，任务保留', code)
            continue
        if data.last_price <= data.low_limit:
            log.info('待卖 %s：跌停无法可靠成交，任务保留', code)
            continue
        if task['protect'] and data.last_price >= data.high_limit:
            log.info('待卖 %s：月度换出但仍封涨停，继续持有并保留任务', code)
            continue

        desired_amount = 0
        if task['weight'] > 0:
            desired_amount = py.int(
                context.portfolio.total_value * task['weight'] /
                data.last_price / 100
            ) * 100

        sell_amount = py.min(
            py.int(position.closeable_amount),
            py.max(0, py.int(position.total_amount) - desired_amount)
        )

        if sell_amount <= 0:
            if task['weight'] > 0:
                del g.pending_sells[code]
            continue

        try:
            _track_order(order(code, -sell_amount), code, False)
            log.info('待卖执行 %s：%s，目标权重 %.2f%%',
                     code, task['reason'], task['weight'] * 100)
        except Exception as e:
            log.error('卖出异常 %s：%s；任务保留', code, e)

    _sync_orders(context)


# ======================== 盘前数据与行业 ========================

def _fetch_industry(codes, asof):
    if not codes:
        return {}
    data = get_industry(security=list(codes), date=asof)
    if data is None:
        raise ValueError('行业数据返回 None')

    result = {}
    for code in codes:
        result[code] = data.get(code, {}).get('sw_l1', {}).get('industry_code')
    return result


def prepare_stock_list(context):
    _ensure_day(context)
    _sync_orders(context)

    held = _holdings(context)
    codes = list(held.keys())
    if not codes:
        g.hold_data_ok = True
        g.industry_ok = True
        return

    try:
        frame = get_price(
            codes,
            end_date=context.previous_date,
            count=1,
            frequency='daily',
            fields=['close', 'high_limit'],
            panel=False,
            skip_paused=False,
            fq=None
        )
        if frame is None or frame.empty:
            raise ValueError('昨日持仓行情为空')
        if 'code' not in frame.columns or frame['code'].nunique() < len(codes):
            raise ValueError('昨日持仓行情不完整')

        close_values = pd.to_numeric(frame['close'], errors='coerce')
        high_values = pd.to_numeric(frame['high_limit'], errors='coerce')
        mask = close_values.notnull() & high_values.notnull() & (close_values == high_values)
        g.high_limit_list = set(frame.loc[mask, 'code'])
        g.hold_data_ok = True
    except Exception as e:
        g.hold_data_ok = False
        log.error('昨日持仓行情异常，今日不新增仓位：%s', e)

    try:
        industries = _fetch_industry(codes, context.previous_date)
        g.industry.update(industries)
        g.industry_ok = py.all(g.industry.get(code) for code in codes)
        if not g.industry_ok:
            raise ValueError('部分持仓缺少申万一级行业')
    except Exception as e:
        g.industry_ok = False
        log.error('持仓行业数据异常，今日不新增仓位：%s', e)


# ======================== 历史数据工具 ========================

def _get_price_batches(codes, asof, count, fields, fq=None, batch_size=200):
    pieces = []
    for start in py.range(0, len(codes), batch_size):
        batch = codes[start:start + batch_size]
        frame = get_price(
            batch,
            end_date=asof,
            count=count,
            frequency='daily',
            fields=fields,
            panel=False,
            skip_paused=False,
            fq=fq
        )
        if frame is None:
            raise ValueError('历史行情返回 None')
        if not frame.empty:
            pieces.append(frame)

    if not pieces:
        return pd.DataFrame()
    return pd.concat(pieces, ignore_index=True)


def _query_dividend_recursive(codes, start_date, end_date):
    query_obj = query(
        finance.STK_XR_XD.code,
        finance.STK_XR_XD.a_registration_date,
        finance.STK_XR_XD.bonus_amount_rmb
    ).filter(
        finance.STK_XR_XD.code.in_(codes),
        finance.STK_XR_XD.a_registration_date > start_date,
        finance.STK_XR_XD.a_registration_date <= end_date
    ).limit(4000)

    frame = finance.run_query(query_obj)
    if frame is None:
        raise ValueError('分红数据返回 None')
    if len(frame) < 4000:
        return frame

    days = (end_date - start_date).days
    if days > 1:
        middle = start_date + dt.timedelta(days=days // 2)
        left = _query_dividend_recursive(codes, start_date, middle)
        right = _query_dividend_recursive(codes, middle, end_date)
        return pd.concat([left, right], ignore_index=True)

    if len(codes) > 1:
        half = len(codes) // 2
        left = _query_dividend_recursive(codes[:half], start_date, end_date)
        right = _query_dividend_recursive(codes[half:], start_date, end_date)
        return pd.concat([left, right], ignore_index=True)

    raise ValueError('单股单日分红记录达到接口上限，拒绝使用截断数据')


def _fetch_dividends(codes, start_date, end_date, batch_size=200):
    pieces = []
    for start in py.range(0, len(codes), batch_size):
        frame = _query_dividend_recursive(
            codes[start:start + batch_size], start_date, end_date
        )
        if frame is not None and not frame.empty:
            pieces.append(frame)

    if not pieces:
        return pd.DataFrame(columns=[
            'code', 'a_registration_date', 'bonus_amount_rmb'
        ])
    return pd.concat(pieces, ignore_index=True)


def _fetch_market_caps(codes, asof):
    pieces = []
    for start in py.range(0, len(codes), 800):
        query_obj = query(
            valuation.code,
            valuation.market_cap,
            valuation.circulating_market_cap
        ).filter(valuation.code.in_(codes[start:start + 800]))
        frame = get_fundamentals(query_obj, date=asof)
        if frame is None:
            raise ValueError('市值数据返回 None')
        if not frame.empty:
            pieces.append(frame)

    if not pieces:
        return pd.DataFrame()
    result = pd.concat(pieces, ignore_index=True)
    return result.drop_duplicates('code').set_index('code')


def _trailing_dividend_yield(records, market_caps, asof):
    if records is None or records.empty:
        return pd.Series(dtype=float)

    frame = records.copy()
    dates = pd.to_datetime(frame['a_registration_date'], errors='coerce')
    cash = pd.to_numeric(frame['bonus_amount_rmb'], errors='coerce')
    age = (pd.Timestamp(asof) - dates).dt.days
    valid = (age >= 0) & (age < 365) & (cash > 0)

    if not valid.any():
        return pd.Series(dtype=float)

    cash_sum = cash[valid].groupby(frame.loc[valid, 'code']).sum()
    caps = pd.to_numeric(market_caps.reindex(cash_sum.index), errors='coerce')
    dividend_yield = (cash_sum / 10000.0) / caps.where(caps > 0)
    return _stable_rank(dividend_yield, ascending=False)


def _continuous_dividend_codes(records, codes, required_years):
    if records is None or records.empty:
        return []

    frame = records.copy()
    frame['year'] = pd.to_datetime(
        frame['a_registration_date'], errors='coerce'
    ).dt.year
    frame['cash'] = pd.to_numeric(
        frame['bonus_amount_rmb'], errors='coerce'
    )

    groups = frame.loc[frame['cash'] > 0].groupby('code')['year'].apply(set)
    required = set(required_years)
    result = []
    for code in codes:
        if code in groups.index and required.issubset(groups.loc[code]):
            result.append(code)
    return result


# ======================== A 池：红利低波 ========================

def _filter_market_cap_a(stock_list, asof):
    data = _fetch_market_caps(stock_list, asof)
    if data.empty:
        return []
    circulating = pd.to_numeric(
        data['circulating_market_cap'], errors='coerce'
    )
    return circulating[circulating >= MIN_MARKET_CAP_A].index.tolist()


def _filter_liquidity(stock_list, asof):
    frame = _get_price_batches(stock_list, asof, 5, ['money'])
    if frame.empty or 'code' not in frame.columns:
        return []

    frame = frame.copy()
    frame['money'] = pd.to_numeric(frame['money'], errors='coerce')
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=['money'])
    summary = frame.groupby('code')['money'].agg(['mean', 'count'])
    summary = summary[(summary['count'] >= 5) & (summary['mean'] > 0)]
    if summary.empty:
        return []

    ranked = _stable_rank(summary['mean'], ascending=False)
    keep_count = py.max(1, py.int(np.ceil(len(ranked) * 0.80)))
    return ranked.head(keep_count).index.tolist()


def _filter_roe_quality(stock_list, asof, required_years):
    snapshots = {}

    for year in required_years:
        snapshot_date = py.min(dt.date(year, 12, 31), asof)
        pieces = []

        for start in py.range(0, len(stock_list), 800):
            query_obj = query(
                indicator.code,
                indicator.inc_return
            ).filter(indicator.code.in_(stock_list[start:start + 800]))
            frame = get_fundamentals(query_obj, date=snapshot_date)
            if frame is None:
                raise ValueError('%s 年 ROE 快照返回 None' % year)
            if not frame.empty:
                pieces.append(frame)

        if pieces:
            combined = pd.concat(pieces, ignore_index=True)
            combined = combined.drop_duplicates('code').set_index('code')
            snapshots[year] = pd.to_numeric(
                combined['inc_return'], errors='coerce'
            )
        else:
            snapshots[year] = pd.Series(dtype=float)

    roe = pd.DataFrame(snapshots).reindex(
        index=stock_list, columns=required_years
    )
    roe = roe.replace([np.inf, -np.inf], np.nan).dropna()
    if roe.empty:
        return []

    score = roe.mean(axis=1) - roe.std(axis=1, ddof=1)
    ranked = _stable_rank(score, ascending=False)
    keep_count = py.max(1, py.int(np.ceil(len(ranked) * 0.70)))
    return ranked.head(keep_count).index.tolist()


def _weekly_volatility(stock_list, asof):
    frame = _get_price_batches(
        stock_list,
        asof,
        VOLATILITY_TRADING_DAYS,
        ['close'],
        fq='pre'
    )
    if frame.empty:
        return pd.Series(dtype=float)

    result = {}
    for code, stock_data in frame.groupby('code'):
        try:
            close_values = pd.to_numeric(
                stock_data['close'], errors='coerce'
            )
            time_values = pd.to_datetime(
                stock_data['time'], errors='coerce'
            )

            # 兼容聚宽旧版 pandas，不使用 Series.to_numpy()。
            closes = np.asarray(close_values.values, dtype=float)
            times = pd.DatetimeIndex(time_values.values)
            series = pd.Series(closes, index=times)
            series = series.dropna().sort_index()
            series = series[(series > 0) &
                            (series.index.normalize() <= pd.Timestamp(asof).normalize())]
            series = series[~series.index.duplicated(keep='last')]

            weekly = series.resample('W-FRI').last().dropna()
            # 周一至周四形成的当周数据会被标记到未来的周五，必须排除。
            weekly = weekly[weekly.index <= pd.Timestamp(asof)]
            if len(weekly) < MIN_COMPLETE_WEEKS:
                continue

            returns = np.log(weekly / weekly.shift(1))
            returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
            value = float(returns.std(ddof=1))
            if np.isfinite(value) and value > 0:
                result[code] = value
        except Exception as e:
            log.warn('周波动率计算失败 %s：%s', code, e)

    return _stable_rank(pd.Series(result, dtype=float), ascending=True)


def select_stocks_enhanced(context, initial_list):
    asof = context.previous_date
    required_years = list(py.range(
        context.current_dt.year - ROE_YEARS,
        context.current_dt.year
    ))

    stocks = _filter_market_cap_a(initial_list, asof)
    if not stocks:
        return []

    stocks = _filter_liquidity(stocks, asof)
    if not stocks:
        return []

    stocks = _filter_roe_quality(stocks, asof, required_years)
    if not stocks:
        return []

    query_start = dt.date(required_years[0], 1, 1) - dt.timedelta(days=1)
    records = _fetch_dividends(stocks, query_start, asof)
    stocks = _continuous_dividend_codes(records, stocks, required_years)
    if not stocks:
        return []

    caps = _fetch_market_caps(stocks, asof)
    if caps.empty:
        return []

    dividend_yield = _trailing_dividend_yield(
        records,
        caps['market_cap'],
        asof
    ).reindex(stocks).dropna()
    dividend_yield = dividend_yield[dividend_yield > DIVIDEND_YIELD_MIN]
    if dividend_yield.empty:
        return []

    if len(dividend_yield) > 10:
        ranked_yield = _stable_rank(dividend_yield, ascending=False)
        keep_count = py.max(1, py.int(np.ceil(len(ranked_yield) * 0.40)))
        stocks = ranked_yield.head(keep_count).index.tolist()
    else:
        stocks = _stable_rank(dividend_yield, ascending=False).index.tolist()

    volatility = _weekly_volatility(stocks, asof)
    candidate_count = POOL_TARGET_NUM * CANDIDATE_MULTIPLIER
    return volatility.index[:candidate_count].tolist()


# ======================== B 池：红利成长/价值 ========================

def _fetch_value_fundamentals(stock_list, asof):
    pieces = []
    for start in py.range(0, len(stock_list), 800):
        query_obj = query(
            valuation.code,
            valuation.market_cap,
            valuation.pe_ratio,
            indicator.inc_return,
            indicator.inc_total_revenue_year_on_year,
            indicator.inc_net_profit_year_on_year
        ).filter(valuation.code.in_(stock_list[start:start + 800]))

        frame = get_fundamentals(query_obj, date=asof)
        if frame is None:
            raise ValueError('B 池基本面数据返回 None')
        if not frame.empty:
            pieces.append(frame)

    if not pieces:
        return pd.DataFrame()

    result = pd.concat(pieces, ignore_index=True)
    result = result.drop_duplicates('code').set_index('code')
    return result.replace([np.inf, -np.inf], np.nan)


def select_stocks_value(context, initial_list):
    asof = context.previous_date
    fundamentals = _fetch_value_fundamentals(initial_list, asof)
    if fundamentals.empty:
        return []

    valid = (
        fundamentals['pe_ratio'].between(5, 50) &
        fundamentals['inc_return'].between(5, 100) &
        fundamentals['inc_total_revenue_year_on_year'].between(5, 100) &
        fundamentals['inc_net_profit_year_on_year'].between(10, 100)
    )
    stocks = fundamentals.index[valid].tolist()
    if not stocks:
        return []

    start_date = asof - dt.timedelta(days=365)
    records = _fetch_dividends(stocks, start_date, asof)
    dividend_yield = _trailing_dividend_yield(
        records,
        fundamentals['market_cap'],
        asof
    )
    dividend_yield = dividend_yield[dividend_yield > DIVIDEND_YIELD_MIN]

    candidate_count = POOL_TARGET_NUM * CANDIDATE_MULTIPLIER
    return dividend_yield.index[:candidate_count].tolist()


# ======================== 合并选股与安全权重 ========================

def _allocate_pool_slots(a_rank, b_rank, industry_map):
    candidates = [
        list(dict.fromkeys(a_rank)),
        list(dict.fromkeys(b_rank))
    ]
    chosen = [[], []]
    pointers = [0, 0]
    selected_unique = set()
    industry_counts = {}

    # A、B 交替取股，避免其中一个池先占满行业名额。
    while True:
        progressed = False
        for pool_index in (0, 1):
            if len(chosen[pool_index]) >= POOL_TARGET_NUM:
                continue

            while pointers[pool_index] < len(candidates[pool_index]):
                code = candidates[pool_index][pointers[pool_index]]
                pointers[pool_index] += 1
                progressed = True

                industry = industry_map.get(code)
                if not industry:
                    continue

                # 同一股票可同时占 A、B 各一个名额，但行业只按唯一股票计数。
                if code not in selected_unique:
                    if len(selected_unique) >= MAX_POSITIONS:
                        continue
                    if industry_counts.get(industry, 0) >= MAX_INDUSTRY:
                        continue

                chosen[pool_index].append(code)
                if code not in selected_unique:
                    selected_unique.add(code)
                    industry_counts[industry] = industry_counts.get(industry, 0) + 1
                break

        if not progressed:
            break
        if (len(chosen[0]) >= POOL_TARGET_NUM and
                len(chosen[1]) >= POOL_TARGET_NUM):
            break

    slot_weight = INVEST_RATIO * POOL_BUDGET / POOL_TARGET_NUM
    weights = {}
    for pool in chosen:
        for code in pool:
            weights[code] = weights.get(code, 0.0) + slot_weight

    # 候选不足时，把尚未使用的仓位平均补给已经合格的股票，但绝不突破 19.8%。
    # 这样比直接留出大量现金更接近 v1 的满仓特征，同时消除 50%/100% 单股仓位。
    target_total = INVEST_RATIO
    remaining = py.max(0.0, target_total - py.sum(weights.values()))
    for _ in py.range(20):
        if remaining <= 1e-10:
            break
        available = [
            code for code in weights
            if weights[code] < MAX_TARGET_WEIGHT - 1e-10
        ]
        if not available:
            break

        equal_add = remaining / len(available)
        distributed = 0.0
        for code in available:
            room = MAX_TARGET_WEIGHT - weights[code]
            add_value = py.min(room, equal_add)
            weights[code] += add_value
            distributed += add_value

        if distributed <= 1e-12:
            break
        remaining -= distributed

    # 浮点保护。
    for code in list(weights.keys()):
        weights[code] = py.min(float(weights[code]), MAX_TARGET_WEIGHT)

    return weights, chosen, remaining


def _build_initial_universe(context):
    asof = context.previous_date
    securities = get_all_securities(types=['stock'], date=asof)
    if securities is None or securities.empty:
        raise ValueError('股票池为空')

    current_data = get_current_data()
    result = []

    for code, row in securities.iterrows():
        if code.startswith(('68', '4', '8', '92')):
            continue
        if pd.isna(row['start_date']):
            continue
        if (pd.Timestamp(asof) - pd.Timestamp(row['start_date'])).days < NEW_STOCK_DAYS:
            continue

        try:
            data = current_data[code]
            if data.paused or data.is_st:
                continue
            if py.any(text in data.name for text in ('ST', '*', '退')):
                continue
            result.append(code)
        except Exception:
            # 当前行情无法核对的股票不进入本月候选，不能视作正常股票放行。
            continue

    return result


def _build_buy_dates(today):
    end_date = today + dt.timedelta(days=10)
    days = get_trade_days(start_date=today, end_date=end_date)
    if days is None or len(days) == 0:
        raise ValueError('无法生成月初补单交易日')
    return set(_as_date(day) for day in days[:BUY_WINDOW_TRADING_DAYS])


def get_stock_list(context):
    _ensure_day(context)
    if not g.calendar_ok or not g.is_month_first:
        return

    current_month = _month_key(context.current_dt.date())
    g.target_weights = {}
    g.target_industry = {}
    g.signal_month = current_month
    g.selection_date = None
    g.selection_ok = False
    g.buy_dates = set()
    g.rebalance_initialized_month = None
    g.blocked_buys_month = set()

    stage = '基础股票池'
    try:
        initial_list = _build_initial_universe(context)
        if not initial_list:
            log.warn('基础股票池无合格股票，本月保留原持仓')
            return
        log.info('基础过滤后：%d 只', len(initial_list))

        stage = 'A 池红利低波'
        enhanced_rank = select_stocks_enhanced(context, initial_list)

        stage = 'B 池红利成长'
        value_rank = select_stocks_value(context, initial_list)

        stage = '候选行业与组合约束'
        candidate_codes = list(dict.fromkeys(enhanced_rank + value_rank))
        if not candidate_codes:
            log.warn('A/B 两池均无合格股票，本月保留原持仓')
            return

        industries = _fetch_industry(candidate_codes, context.previous_date)
        weights, chosen, remaining = _allocate_pool_slots(
            enhanced_rank, value_rank, industries
        )
        if not weights:
            log.warn('组合约束后无可买股票，本月保留原持仓')
            return

        g.target_weights = weights
        g.target_industry = dict(
            (code, industries.get(code)) for code in weights
        )
        g.industry.update(g.target_industry)

        # 上月因停牌、跌停或 T+1 尚未完成的退出任务，若本月重新入选，
        # 改成按本月目标权重校准，避免先错误清仓再买回。
        for code in list(g.pending_sells.keys()):
            if code not in weights:
                continue
            task = g.pending_sells[code]
            task['weight'] = float(weights[code])
            task['reason'] = '新月份重新入选，校准至新目标权重'
            task['protect'] = False
            task['block_rebuy'] = False

        g.selection_date = context.current_dt.date()
        g.selection_ok = True
        g.buy_dates = _build_buy_dates(context.current_dt.date())

        log.info('A 池入选=%s', chosen[0])
        log.info('B 池入选=%s', chosen[1])
        log.info('目标权重=%s；合计=%.2f%%；未分配现金=%.2f%%；补单日=%s',
                 weights, py.sum(weights.values()) * 100,
                 remaining * 100, sorted(g.buy_dates))
    except Exception as e:
        g.target_weights = {}
        g.target_industry = {}
        g.selection_date = None
        g.selection_ok = False
        g.buy_dates = set()
        log.error('月度选股失败，阶段=%s；保留原持仓且不沿用旧信号：%s\n%s',
                  stage, e, traceback.format_exc())


# ======================== 组合风险约束 ========================

def _refresh_held_industries(context):
    held_codes = list(_holdings(context).keys())
    missing = [code for code in held_codes if not g.industry.get(code)]
    if not missing:
        return True
    try:
        g.industry.update(_fetch_industry(missing, context.previous_date))
        return py.all(g.industry.get(code) for code in held_codes)
    except Exception as e:
        log.error('持仓行业刷新失败：%s', e)
        return False


def _enforce_portfolio_limits(context):
    held = _holdings(context)
    if not held:
        return

    _refresh_held_industries(context)
    nav = float(context.portfolio.total_value)

    # 优先退出非目标股票；同类中按代码稳定排序，保证回测可重复。
    priority = lambda code: (code in g.target_weights, code)

    if len(held) > MAX_POSITIONS:
        excess = len(held) - MAX_POSITIONS
        for code in sorted(held.keys(), key=priority)[:excess]:
            _queue_sell(
                context, code, 0.0, '持仓数量超过上限',
                protect_high_limit=False, block_rebuy=True
            )

    industry_codes = {}
    for code, position in held.items():
        industry = g.industry.get(code)
        if industry:
            industry_codes.setdefault(industry, []).append(code)

        data = _quote(code)
        if data is None or nav <= 0:
            continue
        actual_weight = position.total_amount * data.last_price / nav
        if actual_weight > MAX_ACTUAL_WEIGHT:
            _queue_sell(
                context, code, MAX_TARGET_WEIGHT,
                '单股实际仓位超过 20%',
                protect_high_limit=False, block_rebuy=False
            )

    for industry, codes in industry_codes.items():
        if len(codes) <= MAX_INDUSTRY:
            continue
        excess = len(codes) - MAX_INDUSTRY
        for code in sorted(codes, key=priority)[:excess]:
            _queue_sell(
                context, code, 0.0, '行业股票数量超过上限',
                protect_high_limit=False, block_rebuy=True
            )


# ======================== 月度调仓与买入 ========================

def _signal_is_current(context):
    today = context.current_dt.date()
    return (
        g.selection_ok and
        g.signal_month == _month_key(today) and
        bool(g.target_weights)
    )


def _initialize_monthly_rebalance(context):
    current_month = _month_key(context.current_dt.date())
    if g.rebalance_initialized_month == current_month:
        return
    if not _signal_is_current(context):
        return

    held = _holdings(context)
    for code, position in held.items():
        if code not in g.target_weights:
            _queue_sell(
                context, code, 0.0, '月度换出',
                protect_high_limit=True, block_rebuy=False
            )
            continue

        data = _quote(code)
        if data is None:
            continue
        actual_value = position.total_amount * data.last_price
        target_value = context.portfolio.total_value * g.target_weights[code]
        if actual_value > target_value:
            _queue_sell(
                context, code, g.target_weights[code], '月度目标减仓',
                protect_high_limit=True, block_rebuy=False
            )

    g.rebalance_initialized_month = current_month


def _open_buy_codes(open_orders):
    return set(
        order_obj.security for order_obj in open_orders.values()
        if order_obj.is_buy
    )


def _buy_targets(context):
    today = context.current_dt.date()
    if not _signal_is_current(context):
        return
    if today not in g.buy_dates:
        return
    if g.buy_blocked_today or not g.hold_data_ok or not g.industry_ok:
        return

    open_orders = _sync_orders(context)
    if open_orders is None:
        return

    open_buy_codes = _open_buy_codes(open_orders)

    # 权重高者优先，权重相同按代码排序，避免资金不足时结果随机。
    target_order = sorted(
        g.target_weights.keys(),
        key=lambda code: (-g.target_weights[code], code)
    )

    for code in target_order:
        if code in g.blocked_buys_month or code in g.sold_today:
            continue
        if code in g.pending_sells or code in open_buy_codes:
            continue

        data = _quote(code)
        if data is None or data.paused or data.is_st:
            continue
        if py.any(text in data.name for text in ('ST', '*', '退')):
            continue
        if not (data.low_limit < data.last_price < data.high_limit):
            continue

        held = _holdings(context)
        open_orders = _sync_orders(context)
        if open_orders is None:
            return
        open_buy_codes = _open_buy_codes(open_orders)
        occupied = set(held.keys()) | open_buy_codes

        if code not in occupied and len(occupied) >= MAX_POSITIONS:
            continue

        if not _refresh_held_industries(context):
            return
        industry = g.industry.get(code) or g.target_industry.get(code)
        if not industry:
            continue

        # 实际持仓和未成交买单都占用行业名额。
        occupied_industries = []
        industry_missing = False
        for occupied_code in occupied:
            occupied_industry = (
                g.industry.get(occupied_code) or
                g.target_industry.get(occupied_code)
            )
            if not occupied_industry:
                industry_missing = True
                break
            occupied_industries.append(occupied_industry)
        if industry_missing:
            continue
        if (code not in occupied and
                occupied_industries.count(industry) >= MAX_INDUSTRY):
            continue

        nav = float(context.portfolio.total_value)
        target_weight = py.min(
            g.target_weights[code], MAX_TARGET_WEIGHT
        )
        target_value = nav * target_weight

        position = held.get(code)
        current_value = 0.0
        if position is not None:
            current_value = position.total_amount * data.last_price

        gap = py.max(0.0, target_value - current_value)
        available_cash = py.max(
            0.0,
            float(context.portfolio.available_cash) - nav * CASH_RESERVE
        )

        # 不使用待卖股票的预计资金；只用此刻真实可用现金。
        budget = py.min(gap, available_cash * 0.995)
        estimated_buy_price = data.last_price * (1.0 + STOCK_SLIPPAGE)
        shares = py.int(budget / estimated_buy_price / 100) * 100

        if shares <= 0:
            continue
        if budget < shares * estimated_buy_price + MIN_COMMISSION:
            continue

        try:
            _track_order(order(code, shares), code, True)
        except Exception as e:
            log.error('买入异常 %s：%s', code, e)


def trade(context):
    _ensure_day(context)
    _enforce_portfolio_limits(context)
    _process_pending_sells(context)

    if not _signal_is_current(context):
        return

    _initialize_monthly_rebalance(context)
    _process_pending_sells(context)
    _buy_targets(context)


def retry_rebalance(context):
    _ensure_day(context)
    _enforce_portfolio_limits(context)
    _process_pending_sells(context)
    if _signal_is_current(context):
        _initialize_monthly_rebalance(context)
        _buy_targets(context)


def cancel_buy_remainders(context):
    _ensure_day(context)
    _cancel_open_buys(context)


# ======================== 涨停打开与卖出重试 ========================

def check_limit_up(context):
    _ensure_day(context)
    _cancel_open_buys(context)

    held = _holdings(context)
    for code in list(g.high_limit_list):
        if code not in held:
            continue
        data = _quote(code)
        if data is None or data.paused:
            continue
        if data.last_price < data.high_limit:
            # 保留 v1 的“昨日涨停今日开板卖出”规则；本月不再买回。
            _queue_sell(
                context, code, 0.0, '昨日涨停今日开板',
                protect_high_limit=False, block_rebuy=True
            )

    _enforce_portfolio_limits(context)
    _process_pending_sells(context)


def retry_sells(context):
    _ensure_day(context)
    _cancel_open_buys(context)
    _enforce_portfolio_limits(context)
    _process_pending_sells(context)


def end_of_day(context):
    _ensure_day(context)
    _cancel_open_buys(context)
    _sync_orders(context)
    log.info(
        '收盘状态：持仓数=%d，今日买入=%s，今日卖出=%s，待卖=%s，'
        '信号月份=%s，选股成功=%s',
        len(_holdings(context)),
        sorted(g.bought_today),
        sorted(g.sold_today),
        g.pending_sells,
        g.signal_month,
        g.selection_ok
    )

