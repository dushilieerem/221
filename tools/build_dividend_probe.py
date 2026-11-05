# -*- coding: utf-8 -*-
"""生成自包含的红利 v3 选股阶段导出脚本（从策略源码逐字抽取函数）。

用法：python tools/build_dividend_probe.py
输出：tests/parity/probes/jq_dividend_stage_export.py
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / "tests/parity/cases/value_v3/dividend_v3_10w/jq_strategy.py"
OUTPUT = ROOT / "tests/parity/probes/jq_dividend_stage_export.py"

FUNCTIONS = [
    "_stable_rank",
    "_get_price_batches",
    "_query_dividend_recursive",
    "_fetch_dividends",
    "_fetch_market_caps",
    "_trailing_dividend_yield",
    "_continuous_dividend_codes",
    "_filter_market_cap_a",
    "_filter_liquidity",
    "_filter_roe_quality",
    "_weekly_volatility",
    "_fetch_industry",
    "_build_initial_universe",
    "_fetch_value_fundamentals",
    "_allocate_pool_slots",
]

CONSTANTS = """
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
"""

RUNNER = '''

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
    if series is None or len(series) == 0:
        return series
    count = max(1, int(np.ceil(len(series) * fraction)))
    return series.head(count)


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
'''

HEADER = '''# -*- coding: utf-8 -*-
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
'''


def extract(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines()
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            start = node.lineno - 1
            end = node.end_lineno
            chunks.append("\n".join(lines[start:end]))
    missing = set(FUNCTIONS) - {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    if missing:
        raise SystemExit(f"策略源码缺少函数: {sorted(missing)}")
    return "\n\n\n".join(chunks)


def main() -> None:
    source = STRATEGY.read_text(encoding="utf-8")
    body = extract(source)
    text = (
        HEADER
        + "\n#: 以下常量与函数逐字复制自 dividend_v3_10w/jq_strategy.py\n"
        + CONSTANTS
        + "\n\n"
        + body
        + "\n"
        + RUNNER
    )
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
