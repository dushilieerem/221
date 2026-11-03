# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出破净 v3 的五因子原始值与总分。

用法
----
1. 打开聚宽 → 研究环境（Jupyter），新建一个 notebook。
2. 把本文件内容整段粘进一个 cell（或在研究环境里执行 ``%run 本文件路径``）。
3. 跑完会在当前目录生成 ``破净v3_五因子导出.csv``，右键下载后发给 RE。

导出内容
--------
每个调仓日（每月第一个交易日）× 破净池每只股票一行：

    date, code, pb_ratio, market_cap, roa, gross_margin, ebit, ff8,
    div_yield, roa_s, gross_s, ebit_s, ff_s, div_s, total_score

其中 ``*_s`` 是 rank-pct（0~1），``total_score`` 是五项等权平均。RE 用它和自己的
同名字段逐值比对，定位是哪一项因子导致 Top8 不同。

注意
----
- 与回测一样，所有取值都用 ``date=asof``（前一交易日），不读未来数据。
- 本脚本不依赖策略文件，FF8 与股息率逻辑按 ``破净组合_v3.py`` 原样复刻。
- 研究环境有每日数据配额；如果中途额度用完，改 ``START``/``END`` 分批跑，
  把几段 CSV 拼起来即可。
"""
import datetime as dt

import numpy as np
import pandas as pd

# 聚宽研究环境只需星号导入；务必整段从行首开始粘贴，前面不能有缩进，
# 否则会报 "import * only allowed at module level"。
from jqdata import *  # noqa: F401,F403
from jqfactor import *  # noqa: F401,F403

START = "2016-09-30"
END = "2026-09-30"
OUTPUT = "破净v3_五因子导出.csv"


# ------------------------------------------------------------------ 工具


def rank_pct(series: pd.Series) -> pd.Series:
    """与策略一致的 rank-pct，缺失记 0。"""

    s = pd.to_numeric(series, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    return s.rank(pct=True).fillna(0.0)


class FF8Score(Factor):
    """破净 v3 的自定义 FF8 评分（与策略文件一致）。"""

    name = "PojingFF8"
    max_window = 1
    dependencies = [
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
        score = metrics.gt(0).sum(axis=1).astype(float)
        return score.where(metrics.notna().all(axis=1))


def query_dividends(codes, start, end):
    """与策略一致：4000 行上限时递归拆分。"""

    q = query(
        finance.STK_XR_XD.code,
        finance.STK_XR_XD.a_registration_date,
        finance.STK_XR_XD.bonus_amount_rmb,
    ).filter(
        finance.STK_XR_XD.code.in_(codes),
        finance.STK_XR_XD.a_registration_date > start,
        finance.STK_XR_XD.a_registration_date <= end,
    ).limit(4000)
    frame = finance.run_query(q)
    if frame is None:
        raise ValueError("分红查询失败")
    if len(frame) < 4000:
        return frame
    days = (end - start).days
    if days > 1:
        mid = start + dt.timedelta(days=days // 2)
        return pd.concat(
            [query_dividends(codes, start, mid), query_dividends(codes, mid, end)],
            ignore_index=True,
        )
    if len(codes) > 1:
        half = len(codes) // 2
        return pd.concat(
            [query_dividends(codes[:half], start, end), query_dividends(codes[half:], start, end)],
            ignore_index=True,
        )
    raise ValueError("单股单日分红记录触达查询上限")


def dividend_yields(codes, base, asof):
    """v3 口径：近 1 年派息 / 市值；5 个完整自然年均有正派息才计分。"""

    asof_ts = pd.Timestamp(asof)
    oldest = asof_ts - pd.Timedelta(days=365 * 6)
    pieces = []
    for start in range(0, len(codes), 200):
        frame = query_dividends(codes[start:start + 200], oldest, asof_ts)
        if frame is not None and not frame.empty:
            pieces.append(frame)
    if not pieces:
        return pd.Series(0.0, index=codes)
    d = pd.concat(pieces, ignore_index=True)
    d["a_registration_date"] = pd.to_datetime(d["a_registration_date"], errors="coerce")
    d["bonus_amount_rmb"] = pd.to_numeric(d["bonus_amount_rmb"], errors="coerce")
    d = d.dropna(subset=["a_registration_date", "bonus_amount_rmb"])
    d = d[(d["bonus_amount_rmb"] > 0) & (d["a_registration_date"] <= asof_ts)]
    if d.empty:
        return pd.Series(0.0, index=codes)
    years = list(range(asof_ts.year - 5, asof_ts.year))
    d["cy"] = d["a_registration_date"].dt.year
    sub = d[d["cy"].isin(years)]
    if sub.empty:
        return pd.Series(0.0, index=codes)
    by_year = sub.groupby(["code", "cy"])["bonus_amount_rmb"].sum().unstack(fill_value=0)
    by_year = by_year.reindex(columns=years, fill_value=0)
    stable = by_year.index[(by_year > 0).all(axis=1)]
    recent = d[
        (d["code"].isin(stable))
        & (d["a_registration_date"] > asof_ts - pd.Timedelta(days=365))
    ]
    recent_sum = recent.groupby("code")["bonus_amount_rmb"].sum()
    cap = pd.to_numeric(base["market_cap"].reindex(recent_sum.index), errors="coerce")
    yield_ = (recent_sum / 10000.0) / cap.where(cap > 0)
    out = pd.Series(0.0, index=codes)
    out.loc[yield_.index] = yield_.fillna(0.0).clip(lower=0.0)
    return out


def monthly_rebalance_dates():
    """每个调仓日的「前一交易日」，即策略 select 使用的 asof。

    调仓日 = 区间内第一个交易日 + 之后每月第一个交易日；首日也要算一次，
    与聚宽 ``run_monthly(..., 1)`` 在回测首日触发的行为一致。
    """

    # 聚宽研究环境的 Python 版本低于 3.7，没有 date.fromisoformat。
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
    # 返回每个调仓日的前一交易日
    return [days[index - 1] for index in rebalance if index > 0]


# ------------------------------------------------------------------ 主流程


def main() -> None:
    rows = []
    dates = monthly_rebalance_dates()
    print("调仓日 {} 个：{} ~ {}".format(len(dates), dates[0], dates[-1]))
    for index, asof in enumerate(dates, 1):
        q = query(
            valuation.code,
            valuation.pb_ratio,
            valuation.market_cap,
            indicator.adjusted_profit_to_profit,
            indicator.inc_net_profit_year_on_year,
            indicator.roa,
            indicator.gross_profit_margin,
        ).filter(
            valuation.pb_ratio > 0,
            valuation.pb_ratio < 1,
            indicator.adjusted_profit_to_profit > 80,
            indicator.inc_net_profit_year_on_year > 0,
        )
        base = get_fundamentals(q, date=asof)
        if base is None or base.empty:
            print(asof, "破净池为空")
            continue
        base = base.set_index("code")
        codes = base.index.tolist()

        roa_s = rank_pct(base["roa"].reindex(codes))
        gross_s = rank_pct(base["gross_profit_margin"].reindex(codes))

        raw = get_factor_values(securities=codes, factors=["EBIT"], end_date=asof, count=1)
        ebit_raw = pd.to_numeric(raw["EBIT"].iloc[-1], errors="coerce").reindex(codes)
        ebit_s = rank_pct(ebit_raw.where(ebit_raw > 0))

        ff = calc_factors(codes, [FF8Score()], start_date=asof, end_date=asof)
        ff_raw = pd.to_numeric(ff["PojingFF8"].iloc[-1], errors="coerce").reindex(codes)
        ff_s = rank_pct(ff_raw)

        div_raw = dividend_yields(codes, base, asof)
        div_s = rank_pct(div_raw.where(div_raw > 0))

        scores = pd.DataFrame({
            "roa_s": roa_s, "ebit_s": ebit_s, "gross_s": gross_s,
            "ff_s": ff_s, "div_s": div_s,
        }).fillna(0.0)
        total = scores.mean(axis=1)

        for code in codes:
            rows.append({
                "date": str(asof),
                "code": code,
                "pb_ratio": float(base.at[code, "pb_ratio"]),
                "market_cap": float(base.at[code, "market_cap"]),
                "roa": float(base.at[code, "roa"]),
                "gross_margin": float(base.at[code, "gross_profit_margin"]),
                "ebit": float(ebit_raw.get(code, np.nan)),
                "ff8": float(ff_raw.get(code, np.nan)),
                "div_yield": float(div_raw.get(code, 0.0)),
                "roa_s": float(scores.at[code, "roa_s"]),
                "ebit_s": float(scores.at[code, "ebit_s"]),
                "gross_s": float(scores.at[code, "gross_s"]),
                "ff_s": float(scores.at[code, "ff_s"]),
                "div_s": float(scores.at[code, "div_s"]),
                "total_score": float(total.get(code, 0.0)),
            })
        if index % 10 == 0:
            print("进度 {}/{}，累计 {} 行".format(index, len(dates), len(rows)))

    frame = pd.DataFrame(rows)
    frame.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print("已写出 {}：{} 行".format(OUTPUT, len(frame)))


main()
