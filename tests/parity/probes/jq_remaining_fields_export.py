# -*- coding: utf-8 -*-
"""在聚宽研究环境导出剩余对账字段（估值/财务/分红 + 周波动率）。

用途：红利 v3 还有几个字段差异无法从 RE 侧判定，需要聚宽原值：
- 000651 @2016-11-30 的营收同比（RE 单季 3.683%，但聚宽把它选进 B 池）；
- 000338 @2018-05-31 的市值口径（总市值 698 亿 vs 流通 377 亿）；
- 600660 / 601098 @2019-04-30 的 A 池指标与周波动率（第 5 名边界互换）；
- 000848 / 000338 @2018-05-31 的分红记录（B 池排序）。

用法
----
1. 聚宽 → 研究环境（Jupyter），新建 notebook；
2. 整段粘进一个 cell（**从行首开始**，不要带缩进）运行；
3. 下载 ``剩余字段导出.csv``。

输出 CSV 都是长表：``case, code, asof, field, value``。
"""
import numpy as np
import pandas as pd

from jqdata import *  # noqa: F401,F403

# (RE 代码, asof)
CASES = [
    ("000651.SZ", "2016-11-30"),
    ("000338.SZ", "2018-05-31"),
    ("000848.SZ", "2018-05-31"),
    ("600660.SH", "2019-04-30"),
    ("601098.SH", "2019-04-30"),
]

WINDOW = 780
MIN_WEEKS = 50
OUTPUT = "剩余字段导出.csv"


def jq_code(code):
    """RE 代码（.SZ/.SH）转聚宽代码（.XSHE/.XSHG）。"""

    if code.endswith(".SZ"):
        return code[:-3] + ".XSHE"
    if code.endswith(".SH"):
        return code[:-3] + ".XSHG"
    return code


def weekly_volatility(code, asof):
    """复刻策略的 780 日周波动率。"""

    frame = get_price(
        jq_code(code),
        end_date=asof,
        count=WINDOW,
        fields=["close"],
        fq="pre",
        panel=False,
    )
    if frame is None or frame.empty:
        return None, 0
    frame = frame.copy().reset_index()
    if "time" not in frame.columns:
        frame = frame.rename(columns={frame.columns[0]: "time"})
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    series = frame.dropna(subset=["close", "time"]).set_index("time")["close"]
    series = series[series > 0].sort_index()
    series = series[~series.index.duplicated(keep="last")]
    weekly = series.resample("W-FRI").last().dropna()
    weekly = weekly[weekly.index <= pd.Timestamp(asof)]
    if len(weekly) < MIN_WEEKS:
        return None, len(weekly)
    returns = np.log(weekly / weekly.shift(1))
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    value = float(returns.std(ddof=1))
    return (value if np.isfinite(value) and value > 0 else None), len(weekly)


def fundamentals_rows(code, asof):
    jq = jq_code(code)
    rows = []

    q_val = query(
        valuation.code, valuation.market_cap, valuation.circulating_market_cap
    ).filter(valuation.code.in_([jq]))
    val = get_fundamentals(q_val, date=asof)
    if val is not None and not val.empty:
        row = val.iloc[0]
        for field in ("market_cap", "circulating_market_cap"):
            rows.append((code, asof, field, row.get(field)))

    q_ind = query(
        indicator.code,
        indicator.inc_return,
        indicator.inc_total_revenue_year_on_year,
        indicator.inc_net_profit_year_on_year,
    ).filter(indicator.code.in_([jq]))
    ind = get_fundamentals(q_ind, date=asof)
    if ind is not None and not ind.empty:
        row = ind.iloc[0]
        for field in (
            "inc_return",
            "inc_total_revenue_year_on_year",
            "inc_net_profit_year_on_year",
        ):
            rows.append((code, asof, field, row.get(field)))
    return rows


def dividend_rows(code, asof):
    jq = jq_code(code)
    start = str(pd.Timestamp(asof) - pd.Timedelta(days=365 * 2))[:10]
    q = query(
        finance.STK_XR_XD.code,
        finance.STK_XR_XD.a_registration_date,
        finance.STK_XR_XD.bonus_amount_rmb,
    ).filter(
        finance.STK_XR_XD.code.in_([jq]),
        finance.STK_XR_XD.a_registration_date > start,
        finance.STK_XR_XD.a_registration_date <= asof,
    )
    frame = finance.run_query(q)
    rows = []
    if frame is not None and not frame.empty:
        for _, item in frame.iterrows():
            date = str(item["a_registration_date"])[:10]
            rows.append((code, asof, "dividend:" + date, item["bonus_amount_rmb"]))
    return rows


def main():
    detail = []
    for code, asof in CASES:
        detail.extend(fundamentals_rows(code, asof))
        detail.extend(dividend_rows(code, asof))
        value, weeks = weekly_volatility(code, asof)
        detail.append((code, asof, "weekly_vol", value))
        detail.append((code, asof, "weekly_weeks", weeks))
        print("%s %s weekly_vol=%s weeks=%d" % (code, asof, value, weeks))

    frame = pd.DataFrame(
        detail, columns=["code", "asof", "field", "value"]
    )
    frame.insert(0, "case", frame["code"] + "@" + frame["asof"])
    frame.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print("已生成 %s" % OUTPUT)


main()
