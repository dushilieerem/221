# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出红利 v3 每个调仓日的选股中间结果。

目的：把「我们和聚宽的红利选股差异」定位到具体阶段（基础池 / 市值 / 流动性 /
ROE / 连续分红 / 股息率 / 波动率 / B 池基本面 / 行业上限），而不是只看月末持仓。

用法
----
1. 把 `dividend_v3_10w/jq_strategy.py`（或 100w 版本，策略逻辑相同）上传到研究环境
   当前目录，文件名与 STRATEGY_PATH 一致。
2. 把本脚本整段粘贴进一个 cell（**从行首开始**），运行。
3. 生成两个文件，下载后发给 RE：
   - `红利v3_选股阶段导出.csv`：date, stage, rank, code, value
   - `红利v3_最终权重导出.csv`：date, code, weight, pool

说明
----
- 只调用策略自身的选股函数（不复制逻辑），研究环境缺 `get_current_data`，
  本脚本用 `get_extras('is_st')` + 当日成交量构造等价视图。
- 不依赖回测上下文，`g` / `log` / `run_daily` 等都是桩。
- 研究环境 Python 版本较旧：不要用 3.9+ 注解与 f-string。
"""
import datetime as dt
import types

import numpy as np
import pandas as pd

from jqdata import *  # noqa: F401,F403

STRATEGY_PATH = "jq_strategy.py"
START = "2016-09-30"
END = "2026-09-30"
#: 每 N 个调仓日取 1 个（1=全部）。研究环境配额紧张时可设 3 或 6。
DATE_STEP = 1
STAGE_OUTPUT = "红利v3_选股阶段导出.csv"
WEIGHT_OUTPUT = "红利v3_最终权重导出.csv"

STAGES = []
WEIGHTS = []


def _log_stub():
    log = types.SimpleNamespace()

    def _noop(*args, **kwargs):
        return None

    log.info = _noop
    log.warn = _noop
    log.warning = _noop
    log.error = _noop
    log.set_level = _noop
    return log


def _current_data_factory(asof, securities):
    """研究环境没有 get_current_data，用收盘后的等价信息拼一个。"""

    codes = list(securities.index)
    names = securities["display_name"] if "display_name" in securities.columns else pd.Series("", index=codes)

    st_map = {}
    try:
        st_frame = get_extras("is_st", codes, end_date=asof, count=1, df=True)
        if st_frame is not None and not st_frame.empty:
            last = st_frame.iloc[-1]
            st_map = {code: bool(last.get(code, False)) for code in codes}
    except Exception:
        st_map = {}

    vol_map = {}
    for start in range(0, len(codes), 800):
        batch = codes[start:start + 800]
        try:
            frame = get_price(batch, end_date=asof, count=1, fields=["volume"],
                              panel=False, skip_paused=False, fq=None)
            if frame is not None and not frame.empty:
                for code, value in zip(frame["code"], frame["volume"]):
                    vol_map[code] = value
        except Exception:
            pass

    class _Item(object):
        pass

    class _View(object):
        def __getitem__(self, code):
            item = _Item()
            item.name = str(names.get(code, ""))
            item.is_st = st_map.get(code, False)
            volume = vol_map.get(code)
            item.paused = (volume is None or pd.isna(volume) or float(volume) <= 0)
            return item

    return _View()


def _build_context(day, previous):
    context = types.SimpleNamespace()
    context.current_dt = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=1)
    context.previous_date = previous
    return context


def _rows_of(date, codes, stage, values=None, start_rank=1):
    for offset, code in enumerate(codes):
        value = None
        if values is not None:
            try:
                value = float(values.loc[code])
            except Exception:
                value = None
        STAGES.append({
            "date": str(date),
            "stage": stage,
            "rank": start_rank + offset,
            "code": code,
            "value": value,
        })


def _top(series, fraction):
    ranked = series.dropna()
    if ranked.empty:
        return ranked
    count = max(1, int(np.ceil(len(ranked) * fraction)))
    return ranked.head(count)


def run_date(ns, day, previous, market_cls):
    """按策略自身的顺序跑一遍选股，逐阶段记录。"""

    g = ns["g"]
    g.today = None  # 强制 _ensure_day 重算
    ns["get_current_data"] = lambda: market_cls
    context = _build_context(day, previous)
    ns["_ensure_day"](context)
    asof = previous

    initial = ns["_build_initial_universe"](context)
    _rows_of(day, initial, "initial")
    if not initial:
        return

    cap = ns["_filter_market_cap_a"](initial, asof)
    _rows_of(day, cap, "a_market_cap")
    if not cap:
        return

    liquid = ns["_filter_liquidity"](cap, asof)
    _rows_of(day, liquid, "a_liquidity")
    if not liquid:
        return

    years = list(range(pd.Timestamp(day).year - ns["ROE_YEARS"], pd.Timestamp(day).year))
    roe = ns["_filter_roe_quality"](liquid, asof, years)
    _rows_of(day, roe, "a_roe")
    if not roe:
        return

    query_start = dt.date(years[0], 1, 1) - dt.timedelta(days=1)
    records = ns["_fetch_dividends"](roe, query_start, asof)
    continuous = ns["_continuous_dividend_codes"](records, roe, years)
    _rows_of(day, continuous, "a_continuous_dividend")
    if not continuous:
        return

    caps = ns["_fetch_market_caps"](continuous, asof)
    if caps.empty:
        return
    yields = ns["_trailing_dividend_yield"](records, caps["market_cap"], asof)
    yields = yields.reindex(continuous).dropna()
    yields = yields[yields > ns["DIVIDEND_YIELD_MIN"]]
    _rows_of(day, list(yields.index), "a_dividend_yield", yields)
    if yields.empty:
        a_rank = []
    else:
        if len(yields) > 10:
            picked = _top(yields, 0.40)
        else:
            picked = yields
        volatility = ns["_weekly_volatility"](list(picked.index), asof)
        _rows_of(day, list(volatility.index), "a_volatility", volatility)
        a_rank = list(volatility.index[: ns["POOL_TARGET_NUM"] * ns["CANDIDATE_MULTIPLIER"]])

    # B 池
    fundamentals = ns["_fetch_value_fundamentals"](initial, asof)
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
            b_records = ns["_fetch_dividends"](b_stocks, start_date, asof)
            b_yields = ns["_trailing_dividend_yield"](b_records, fundamentals["market_cap"], asof)
            b_yields = b_yields[b_yields > ns["DIVIDEND_YIELD_MIN"]]
            _rows_of(day, list(b_yields.index), "b_dividend_yield", b_yields)
            b_rank = list(b_yields.index[: ns["POOL_TARGET_NUM"] * ns["CANDIDATE_MULTIPLIER"]])

    _rows_of(day, a_rank, "a_rank")
    _rows_of(day, b_rank, "b_rank")

    candidates = list(dict.fromkeys(a_rank + b_rank))
    industries = ns["_fetch_industry"](candidates, asof) if candidates else {}
    for code in candidates:
        STAGES.append({
            "date": str(day),
            "stage": "industry",
            "rank": None,
            "code": code,
            "value": (industries.get(code) or ""),
        })

    weights, chosen, remaining = ns["_allocate_pool_slots"](a_rank, b_rank, industries)
    for code, weight in weights.items():
        WEIGHTS.append({
            "date": str(day),
            "code": code,
            "weight": float(weight),
            "pool": "A" if code in chosen[0] else ("B" if code in chosen[1] else "?"),
        })


def main():
    with open(STRATEGY_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    ns = {"__name__": "dividend_stage_export"}
    ns["g"] = types.SimpleNamespace()
    ns["log"] = _log_stub()
    for name in ("set_option", "set_benchmark", "set_slippage", "set_order_cost", "run_daily"):
        ns[name] = lambda *args, **kwargs: None
    ns["PriceRelatedSlippage"] = lambda *args, **kwargs: None
    ns["OrderCost"] = lambda *args, **kwargs: None
    exec(compile(source, STRATEGY_PATH, "exec"), ns)
    ns["initialize"](None)  # 只用来初始化 g 的字段；run_daily 等是桩

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
        securities = get_all_securities(types=["stock"], date=previous)
        market = _current_data_factory(previous, securities)
        try:
            run_date(ns, day, previous, market)
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
