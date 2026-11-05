# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出红利 v3 对账所需的明细（基本面字段 + 分红原始记录）。

用途：定位红利 v3 两个问题
1. B 池基本面过滤：我方 708 只通过、聚宽 104 只，需要逐股四个字段的原始值；
2. 分红换算：有送股的股票股息率偏高恰好是 (1+送股/10) 倍，需要聚宽派息总额原值。

用法
----
研究环境整段粘贴运行（从行首开始），生成两个 CSV：
- 红利v3_基本面明细_<日期>.csv：code, pe_ratio, inc_return,
  inc_total_revenue_year_on_year, inc_net_profit_year_on_year
- 红利v3_分红原值导出.csv：code, a_registration_date, bonus_amount_rmb

研究环境 Python 较旧：不使用 3.9+ 语法与 f-string。
"""
import datetime as dt

import pandas as pd

from jqdata import *  # noqa: F401,F403

DATE = "2019-01-02"
BASIC_OUTPUT = "红利v3_基本面明细_%s.csv" % DATE.replace("-", "")
DIVIDEND_OUTPUT = "红利v3_分红原值导出.csv"

#: 有送股、且股息率差异恰好等于 (1+送股/10) 的样本；另加两只对照。
DIAG_CODES = [
    "002440.XSHE", "002042.XSHE", "600183.XSHG", "002108.XSHE",
    "600742.XSHG", "600519.XSHG", "601398.XSHG",
]
DIVIDEND_START = "2015-01-01"
DIVIDEND_END = "2026-09-30"


def export_fundamentals():
    securities = get_all_securities(types=["stock"], date=DATE)
    codes = list(securities.index)
    pieces = []
    for start in range(0, len(codes), 800):
        batch = codes[start:start + 800]
        q = query(
            valuation.code,
            valuation.pe_ratio,
            indicator.inc_return,
            indicator.inc_total_revenue_year_on_year,
            indicator.inc_net_profit_year_on_year,
        ).filter(valuation.code.in_(batch))
        frame = get_fundamentals(q, date=DATE)
        if frame is not None and not frame.empty:
            pieces.append(frame)
    result = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    result.to_csv(BASIC_OUTPUT, index=False, encoding="utf-8-sig")
    print("已写出 %s：%d 行" % (BASIC_OUTPUT, len(result)))


def query_dividends(codes, start_date, end_date):
    q = query(
        finance.STK_XR_XD.code,
        finance.STK_XR_XD.a_registration_date,
        finance.STK_XR_XD.bonus_amount_rmb,
    ).filter(
        finance.STK_XR_XD.code.in_(codes),
        finance.STK_XR_XD.a_registration_date > start_date,
        finance.STK_XR_XD.a_registration_date <= end_date,
    ).limit(4000)
    frame = finance.run_query(q)
    if frame is None:
        raise ValueError("分红查询返回 None")
    if len(frame) < 4000:
        return frame
    days = (end_date - start_date).days
    if days > 1:
        middle = start_date + dt.timedelta(days=days // 2)
        return pd.concat(
            [query_dividends(codes, start_date, middle),
             query_dividends(codes, middle, end_date)],
            ignore_index=True,
        )
    raise ValueError("单日分红记录触达查询上限")


def export_dividends():
    start = dt.datetime.strptime(DIVIDEND_START, "%Y-%m-%d").date()
    end = dt.datetime.strptime(DIVIDEND_END, "%Y-%m-%d").date()
    pieces = []
    for index in range(0, len(DIAG_CODES), 200):
        frame = query_dividends(DIAG_CODES[index:index + 200], start, end)
        if frame is not None and not frame.empty:
            pieces.append(frame)
    result = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    result.to_csv(DIVIDEND_OUTPUT, index=False, encoding="utf-8-sig")
    print("已写出 %s：%d 行" % (DIVIDEND_OUTPUT, len(result)))


def main():
    export_fundamentals()
    export_dividends()


main()
