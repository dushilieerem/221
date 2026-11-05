# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出红利 v3 每个调仓日的选股中间结果（自包含版）。

本文件由 tools/build_dividend_probe.py 从
`tests/parity/cases/value_v3/dividend_v3_10w/jq_strategy.py` 逐字抽取函数生成，
不需要上传策略文件，直接整段粘贴运行即可。

输出：
- 红利v3_选股阶段导出.csv：date, stage, rank, code, value
- 红利v3_最终权重导出.csv：date, code, weight, pool

研究环境 Python 版本较旧，本文件不使用 3.9+ 语法。
"""
import builtins as py
import datetime as dt

import numpy as np
import pandas as pd

from jqdata import *  # noqa: F401,F403

START = "2016-09-30"
END = "2026-09-30"
#: 每 N 个调仓日取 1 个（1=全部）。配额紧张时设 3 或 6。
DATE_STEP = 1
STAGE_OUTPUT = "红利v3_选股阶段导出.csv"
WEIGHT_OUTPUT = "红利v3_最终权重导出.csv"


# 研究环境同一个 kernel 里重复运行本 cell 时，globals 里会残留上一次的
# 包装函数；先清掉，否则 _ORIGINAL_GET_INDUSTRY 会捕获到它自己，无限递归。
if "get_industry" in globals():
    _stale = globals().get("get_industry")
    if getattr(_stale, "__module__", None) == "__main__":
        del globals()["get_industry"]

# 研究环境的 get_industry 可能返回 DataFrame（回测里是 dict），统一成 dict。
try:
    _ORIGINAL_GET_INDUSTRY = get_industry
except NameError:
    _ORIGINAL_GET_INDUSTRY = None


def get_industry(security, date=None, **kwargs):
    if _ORIGINAL_GET_INDUSTRY is None:
        raise RuntimeError("研究环境没有 get_industry；请重启内核后重新运行本 cell")
    result = _ORIGINAL_GET_INDUSTRY(security, date=date, **kwargs)
    if isinstance(result, pd.DataFrame):
        mapping = {}
        for code in result.index:
            payload = {}
            for group in ("sw_l1", "sw_l2"):
                try:
                    value = result[(group, "industry_code")].get(code)
                except Exception:
                    value = None
                if value is not None and not pd.isna(value):
                    payload[group] = {"industry_code": value}
            mapping[code] = payload
        return mapping
    return result

#: 以下常量与函数逐字复制自 dividend_v3_10w/jq_strategy.py

POOL_TARGET_NUM = 5
POOL_BUDGET = 0.50
INVEST_RATIO = 0.99
MAX_POSITIONS = 10
MAX_INDUSTRY = 5
MAX_TARGET_WEIGHT = 0.198
MIN_MARKET_CAP_A = 50.0
NEW_STOCK_DAYS = 365
ROE_YEARS = 3
DIVIDEND_YIELD_MIN = 0.03
VOLATILITY_TRADING_DAYS = 780
MIN_COMPLETE_WEEKS = 50
CANDIDATE_MULTIPLIER = 3


def _stable_rank(series, ascending=False):
    values = pd.to_numeric(series, errors='coerce')
    values = values.replace([np.inf, -np.inf], np.nan).dropna()
    return values.sort_index().sort_values(ascending=ascending, kind='mergesort')


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


# ======================== 导出主流程 ========================

STAGES = []
WEIGHTS = []
_STATS = {"printed": False}


class _Log(object):
    def info(self, *args, **kwargs):
        pass

    warn = info
    warning = info
    error = info


log = _Log()


def _current_data_view(asof):
    """研究环境没有 get_current_data，用收盘后的等价信息拼一个。"""

    securities = get_all_securities(types=["stock"], date=asof)
    codes = list(securities.index)
    names = securities["display_name"] if "display_name" in securities.columns else pd.Series("", index=codes)

    st_map = {}
    st_error = None
    try:
        st_frame = get_extras("is_st", codes, end_date=asof, count=1, df=True)
        if st_frame is not None and not st_frame.empty:
            last = st_frame.iloc[-1]
            st_map = dict((code, bool(last.get(code, False))) for code in codes)
    except Exception as exc:
        st_map = {}
        st_error = "%s: %s" % (type(exc).__name__, exc)

    vol_map = {}
    vol_error = None
    for start in range(0, len(codes), 800):
        batch = codes[start:start + 800]
        try:
            frame = get_price(batch, end_date=asof, count=1, fields=["volume"],
                              panel=False, skip_paused=False, fq=None)
            if frame is not None and not frame.empty and "code" in frame.columns:
                for code, value in zip(frame["code"], frame["volume"]):
                    vol_map[code] = value
            elif frame is not None and not frame.empty:
                vol_error = "columns=%s" % list(frame.columns)
        except Exception as exc:
            vol_error = "%s: %s" % (type(exc).__name__, exc)

    if not _STATS["printed"]:
        _STATS["printed"] = True
        print("诊断 %s：证券 %d，is_st %d（%s），volume %d（%s）" % (
            asof, len(codes), len(st_map), st_error or "ok",
            len(vol_map), vol_error or "ok"))

    class _Item(object):
        pass

    class _View(object):
        def __getitem__(self, code):
            item = _Item()
            item.name = str(names.get(code, ""))
            item.is_st = st_map.get(code, False)
            volume = vol_map.get(code)
            # 取不到成交量时按“正常交易”处理，避免接口不可用把整个池清空；
            # 真实停牌仍会被后续价格/成交检查拦截。
            if volume is None:
                item.paused = False
            else:
                item.paused = bool(pd.isna(volume)) or float(volume) <= 0
            return item

    return _View()


_VIEW = {"value": None}


def get_current_data():
    return _VIEW["value"]


def _context(day, previous):
    context = type("Context", (object,), {})()
    context.current_dt = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=1)
    context.previous_date = previous
    return context


def _industry_safe(codes, asof):
    """研究环境 get_industry 的返回形态不稳定，这里统一成 {code: 行业代码}。"""

    if not codes:
        return {}
    raw = None
    error = None
    for attempt in range(2):
        try:
            if attempt == 0:
                raw = get_industry(security=list(codes), date=asof)
            else:
                raw = get_industry(list(codes), asof)
            break
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            raw = None
    if not _STATS.get("industry_raw_printed"):
        _STATS["industry_raw_printed"] = True
        sample = None
        if isinstance(raw, dict) and raw:
            sample = (list(raw.keys())[0], raw[list(raw.keys())[0]])
        elif isinstance(raw, pd.DataFrame):
            sample = ("DataFrame", list(raw.columns)[:4])
        print("诊断 行业原始返回：%s，样例 %s，错误 %s" % (type(raw).__name__, sample, error))
    if raw is None:
        return {}

    result = {}
    if isinstance(raw, pd.DataFrame):
        for code in raw.index:
            payload = raw.loc[code]
            code_value = None
            try:
                code_value = payload.get(("sw_l1", "industry_code"))
            except Exception:
                pass
            if code_value is None:
                for column in raw.columns:
                    if isinstance(column, tuple) and column[0] == "sw_l1" and "code" in column[1]:
                        try:
                            code_value = payload[column]
                        except Exception:
                            code_value = None
                        break
            result[code] = code_value
        return dict((code, value) for code, value in result.items() if value is not None)

    for code in codes:
        item = raw.get(code) or {}
        level = item.get("sw_l1") or {}
        value = level.get("industry_code")
        if value is not None:
            result[code] = value
    return result


def _rows_of(day, codes, stage, values=None):
    for offset, code in enumerate(codes):
        value = None
        if values is not None:
            try:
                value = float(values.loc[code])
            except Exception:
                value = None
        STAGES.append({
            "date": str(day), "stage": stage, "rank": offset + 1,
            "code": code, "value": value,
        })


def _top(series, fraction):
    """按市值排序取前 fraction——必须是「按股息率降序」，不能直接用输入顺序。

    策略里这一步是 `_stable_rank(dividend_yield, ascending=False).head(...)`；
    早期版本误用 `series.head()`，导致 A 池后面的波动率/排名用了错误的候选集。
    """

    if series is None or len(series) == 0:
        return series
    count = max(1, int(np.ceil(len(series) * fraction)))
    return _stable_rank(series, ascending=False).head(count)


def run_date(day, previous):
    asof = previous
    _VIEW["value"] = _current_data_view(asof)
    context = _context(day, previous)

    initial = _build_initial_universe(context)
    _rows_of(day, initial, "initial")
    if not initial:
        return

    cap = _filter_market_cap_a(initial, asof)
    _rows_of(day, cap, "a_market_cap")
    if not cap:
        return

    liquid = _filter_liquidity(cap, asof)
    _rows_of(day, liquid, "a_liquidity")
    if not liquid:
        return

    years = list(range(pd.Timestamp(day).year - ROE_YEARS, pd.Timestamp(day).year))
    roe = _filter_roe_quality(liquid, asof, years)
    _rows_of(day, roe, "a_roe")
    if not roe:
        return

    query_start = dt.date(years[0], 1, 1) - dt.timedelta(days=1)
    records = _fetch_dividends(roe, query_start, asof)
    continuous = _continuous_dividend_codes(records, roe, years)
    _rows_of(day, continuous, "a_continuous_dividend")
    if not continuous:
        return

    caps = _fetch_market_caps(continuous, asof)
    if caps.empty:
        return
    yields = _trailing_dividend_yield(records, caps["market_cap"], asof)
    yields = yields.reindex(continuous).dropna()
    yields = yields[yields > DIVIDEND_YIELD_MIN]
    _rows_of(day, list(yields.index), "a_dividend_yield", yields)

    a_rank = []
    if len(yields) > 0:
        picked = _top(yields, 0.40) if len(yields) > 10 else yields
        volatility = _weekly_volatility(list(picked.index), asof)
        _rows_of(day, list(volatility.index), "a_volatility", volatility)
        a_rank = list(volatility.index[: POOL_TARGET_NUM * CANDIDATE_MULTIPLIER])

    fundamentals = _fetch_value_fundamentals(initial, asof)
    b_rank = []
    if not fundamentals.empty:
        valid = (
            fundamentals["pe_ratio"].between(5, 50)
            & fundamentals["inc_return"].between(5, 100)
            & fundamentals["inc_total_revenue_year_on_year"].between(5, 100)
            & fundamentals["inc_net_profit_year_on_year"].between(10, 100)
        )
        b_stocks = list(fundamentals.index[valid])
        _rows_of(day, b_stocks, "b_fundamentals")
        if b_stocks:
            start_date = asof - dt.timedelta(days=365)
            b_records = _fetch_dividends(b_stocks, start_date, asof)
            b_yields = _trailing_dividend_yield(b_records, fundamentals["market_cap"], asof)
            b_yields = b_yields[b_yields > DIVIDEND_YIELD_MIN]
            _rows_of(day, list(b_yields.index), "b_dividend_yield", b_yields)
            b_rank = list(b_yields.index[: POOL_TARGET_NUM * CANDIDATE_MULTIPLIER])

    _rows_of(day, a_rank, "a_rank")
    _rows_of(day, b_rank, "b_rank")

    candidates = list(dict.fromkeys(a_rank + b_rank))
    industries = _industry_safe(candidates, asof)
    if not _STATS.get("industry_printed"):
        _STATS["industry_printed"] = True
        filled = sum(1 for value in industries.values() if value)
        print("诊断 行业 %s：候选 %d，取到行业 %d" % (asof, len(candidates), filled))
    for code in candidates:
        STAGES.append({
            "date": str(day), "stage": "industry", "rank": None,
            "code": code, "value": (industries.get(code) or ""),
        })

    weights, chosen, remaining = _allocate_pool_slots(a_rank, b_rank, industries)
    for code, weight in weights.items():
        WEIGHTS.append({
            "date": str(day), "code": code, "weight": float(weight),
            "pool": "A" if code in chosen[0] else ("B" if code in chosen[1] else "?"),
        })


def main():
    days = list(get_trade_days(START, END))
    starts = []
    for index, day in enumerate(days):
        if index == 0 or days[index - 1].month != day.month:
            starts.append(index)

    for position, index in enumerate(starts):
        if DATE_STEP > 1 and position % DATE_STEP != 0:
            continue
        day = days[index]
        previous = days[index - 1] if index > 0 else day - dt.timedelta(days=1)
        try:
            run_date(day, previous)
        except Exception as exc:
            STAGES.append({
                "date": str(day), "stage": "error", "rank": None,
                "code": "", "value": "%s: %s" % (type(exc).__name__, exc),
            })
        print("进度 %s：阶段行 %d，权重行 %d" % (day, len(STAGES), len(WEIGHTS)))

    pd.DataFrame(STAGES).to_csv(STAGE_OUTPUT, index=False, encoding="utf-8-sig")
    pd.DataFrame(WEIGHTS).to_csv(WEIGHT_OUTPUT, index=False, encoding="utf-8-sig")
    print("已写出 %s（%d 行）与 %s（%d 行）" % (STAGE_OUTPUT, len(STAGES), WEIGHT_OUTPUT, len(WEIGHTS)))


main()
