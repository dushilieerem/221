# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出边界股的 780 日周波动率，用于对齐 A 池排名。

用途：红利 v3 的 A 池按「780 个交易日的周收益标准差」取前 5。RE 与聚宽的
边界股（第 5/6 名附近）经常互换，需要确认是价格序列、窗口还是计算口径差异。

用法
----
1. 打开聚宽 → 研究环境（Jupyter），新建 notebook。
2. 整段粘进一个 cell（**从行首开始**，不要带缩进），运行。
3. 生成 ``周波动率导出.csv``（汇总）和 ``周收盘导出.csv``（逐周收盘），
   右键下载发给 RE。

计算口径与策略 ``_weekly_volatility`` 完全一致：
``close``（``fq='pre'``）→ 按 ``W-FRI`` 取每周最后收盘 → 丢弃 asof 之后的周
→ 周对数收益 → ``std(ddof=1)``；样本不足 50 周返回空。
"""
import numpy as np
import pandas as pd

from jqdata import *  # noqa: F401,F403

# (股票代码, asof)：asof 是选股日的前一交易日
CASES = [
    ("000876.SZ", "2018-05-31"),  # 2018-06-01 选股，A 池第 6 名
    ("600548.SH", "2019-08-30"),  # 2019-09-02 选股，A 池第 6 名
    ("000651.SZ", "2016-11-30"),  # 2016-12-01 选股，聚宽选中、RE 第 12 名
    ("000002.SZ", "2017-03-31"),  # 2017-04-05 分歧，RE 波动率前 15 之外
]

WINDOW = 780
MIN_WEEKS = 50
SUMMARY = "周波动率导出.csv"
WEEKLY = "周收盘导出.csv"


def weekly_volatility(code, asof):
    """复刻策略的周波动率计算；返回 (vol, 周序列)。"""

    frame = get_price(
        code,
        end_date=asof,
        count=WINDOW,
        fields=["close"],
        fq="pre",
        panel=False,
    )
    if frame is None or frame.empty:
        return None, None
    frame = frame.copy()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    series = frame.dropna(subset=["close", "time"]).set_index("time")["close"]
    series = series[series > 0].sort_index()
    series = series[~series.index.duplicated(keep="last")]
    weekly = series.resample("W-FRI").last().dropna()
    weekly = weekly[weekly.index <= pd.Timestamp(asof)]
    if len(weekly) < MIN_WEEKS:
        return None, weekly
    returns = np.log(weekly / weekly.shift(1))
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    value = float(returns.std(ddof=1))
    if not np.isfinite(value) or value <= 0:
        return None, weekly
    return value, weekly


def main():
    summary = []
    weekly_rows = []
    for code, asof in CASES:
        value, weekly = weekly_volatility(code, asof)
        weeks = 0 if weekly is None else len(weekly)
        first = "" if weekly is None or weekly.empty else str(weekly.index[0].date())
        last = "" if weekly is None or weekly.empty else str(weekly.index[-1].date())
        summary.append({
            "code": code,
            "asof": asof,
            "weekly_vol": value,
            "weeks": weeks,
            "first_week": first,
            "last_week": last,
        })
        if weekly is not None:
            for day, close in weekly.items():
                weekly_rows.append({
                    "code": code,
                    "asof": asof,
                    "week_end": str(pd.Timestamp(day).date()),
                    "close": float(close),
                })
        print("%s %s vol=%s weeks=%d" % (code, asof, value, weeks))

    pd.DataFrame(summary).to_csv(SUMMARY, index=False, encoding="utf-8-sig")
    pd.DataFrame(weekly_rows).to_csv(WEEKLY, index=False, encoding="utf-8-sig")
    print("已生成 %s 和 %s" % (SUMMARY, WEEKLY))


main()
