# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出破净 v3 的 FF8 明细（8 个布尔项 + 22 个依赖原值）。

用途：RE 侧 FF8 分数只有约 57% 与聚宽一致，需要逐字段定位是哪几项判断不同。

用法
----
1. 打开聚宽 → 研究环境（Jupyter），新建 notebook。
2. 整段粘进一个 cell（**从行首开始**，不要带缩进），运行。
3. 生成 ``破净v3_FF8明细导出.csv``，右键下载发给 RE。

输出列
------
``date, code, <8 个布尔项>, ff8, <22 个依赖原值>``

布尔项与策略 ``FF8Score`` 完全一致：
DELTA_ROE / DELTA_ROA1 / ROA2 / DELTA_ROA2 / DELTA_LEVER / DELTA_LIQUID /
DELTA_MARGIN / DELTA_CATURN。
"""
import datetime as dt

import numpy as np
import pandas as pd

# 聚宽研究环境的标准导入方式；必须位于行首。
from jqdata import *  # noqa: F401,F403
from jqfactor import *  # noqa: F401,F403

START = "2016-09-30"
END = "2026-09-30"
OUTPUT = "破净v3_FF8明细导出.csv"

COMPONENTS: list[dict] = []
_CURRENT_DATE = {"value": None}

DEPENDENCIES = [
    "roe", "roe_4", "roa", "roa_4",
    "total_profit", "total_profit_4", "financial_expense", "financial_expense_4",
    "total_current_assets", "total_current_assets_1",
    "total_current_assets_4", "total_current_assets_5",
    "total_non_current_liability", "total_non_current_liability_4",
    "total_non_current_assets", "total_non_current_assets_4",
    "total_current_liability", "total_current_liability_4",
    "gross_profit_margin", "gross_profit_margin_4",
    "operating_revenue", "operating_revenue_4",
]


class FF8Score(Factor):
    name = "PojingFF8"
    max_window = 1
    dependencies = DEPENDENCIES

    def calc(self, data):
        def last(name):
            return pd.to_numeric(data[name].iloc[-1], errors="coerce")

        def ratio(a, b):
            return a / b.where(b > 0)

        avg_assets = (last("total_current_assets") + last("total_current_assets_1")) / 2.0
        avg_assets_old = (last("total_current_assets_4") + last("total_current_assets_5")) / 2.0
        r2 = ratio(last("total_profit") + last("financial_expense"), avg_assets)
        r2_old = ratio(last("total_profit_4") + last("financial_expense_4"), avg_assets_old)
        lever = ratio(last("total_non_current_liability"), last("total_non_current_assets"))
        lever_old = ratio(
            last("total_non_current_liability_4"), last("total_non_current_assets_4")
        )
        liquid = ratio(last("total_current_assets"), last("total_current_liability"))
        liquid_old = ratio(
            last("total_current_assets_4"), last("total_current_liability_4")
        )
        turnover = ratio(last("operating_revenue"), avg_assets)
        turnover_old = ratio(last("operating_revenue_4"), avg_assets_old)
        metrics = pd.DataFrame({
            "DELTA_ROE": last("roe") - last("roe_4"),
            "DELTA_ROA1": last("roa") - last("roa_4"),
            "ROA2": r2,
            "DELTA_ROA2": r2 - r2_old,
            "DELTA_LEVER": lever_old - lever,
            "DELTA_LIQUID": liquid - liquid_old,
            "DELTA_MARGIN": last("gross_profit_margin") - last("gross_profit_margin_4"),
            "DELTA_CATURN": turnover - turnover_old,
        }).replace([np.inf, -np.inf], np.nan)
        flags = metrics.gt(0)
        score = flags.sum(axis=1).astype(float).where(metrics.notna().all(axis=1))

        # 逐股记录布尔项与依赖原值，供 RE 逐字段比对。
        codes = list(metrics.index)
        raw = {name: last(name).reindex(codes) for name in DEPENDENCIES}
        for code in codes:
            row = {
                "date": str(_CURRENT_DATE["value"]),
                "code": code,
                "ff8": None if pd.isna(score.get(code)) else float(score.get(code)),
            }
            for name in metrics.columns:
                value = flags.at[code, name]
                row[name] = None if pd.isna(value) else int(bool(value))
            for name in DEPENDENCIES:
                value = raw[name].get(code)
                row[name] = None if pd.isna(value) else float(value)
            COMPONENTS.append(row)
        return score


def monthly_rebalance_dates():
    start = dt.datetime.strptime(START, "%Y-%m-%d").date()
    warmup_start = start - dt.timedelta(days=30)
    days = list(get_trade_days(warmup_start, END))
    in_range = [index for index, day in enumerate(days) if day >= start]
    if not in_range:
        return []
    rebalance = [in_range[0]]
    for position in range(1, len(in_range)):
        index = in_range[position]
        if days[index].month != days[index - 1].month:
            rebalance.append(index)
    return [days[index - 1] for index in rebalance if index > 0]


def main() -> None:
    dates = monthly_rebalance_dates()
    print("调仓日 {} 个：{} ~ {}".format(len(dates), dates[0], dates[-1]))
    for index, asof in enumerate(dates, 1):
        q = query(
            valuation.code,
            valuation.pb_ratio,
        ).filter(
            valuation.pb_ratio > 0,
            valuation.pb_ratio < 1,
            indicator.adjusted_profit_to_profit > 80,
            indicator.inc_net_profit_year_on_year > 0,
        )
        base = get_fundamentals(q, date=asof)
        if base is None or base.empty:
            continue
        codes = base["code"].tolist()
        _CURRENT_DATE["value"] = asof
        calc_factors(codes, [FF8Score()], start_date=asof, end_date=asof)
        if index % 10 == 0:
            print("进度 {}/{}，累计 {} 行".format(index, len(dates), len(COMPONENTS)))

    frame = pd.DataFrame(COMPONENTS)
    frame.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print("已写出 {}：{} 行".format(OUTPUT, len(frame)))


main()
