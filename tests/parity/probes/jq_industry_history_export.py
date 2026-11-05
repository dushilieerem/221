# -*- coding: utf-8 -*-
"""在聚宽「研究环境」导出申万一级/二级行业的历史归属（按调仓日快照）。

用途：RE 的 ``industry_daily`` 只有 latest_only 映射，2016–2020 的「行业上限」
用的是今天的行业归属。这个脚本按月导出聚宽当时的 PIT 行业，用来做对账覆盖层，
并给数据侧重建历史分类提供参照。

用法
----
1. 聚宽 → 研究环境 → 新建 notebook。
2. 整段粘贴进一个 cell（**从行首开始**，不要带缩进），运行。
3. 生成 ``聚宽行业历史导出.csv``，下载后发给 RE。

输出列：``date, code, sw_l1_code, sw_l1_name, sw_l2_code, sw_l2_name``
（另外附带 ``jq_l1_code/jq_l2_code`` 便于核对聚宽自己的分类）。

注意
----
- 每个日期调用一次 ``get_industry``，共约 121 次，避免逐股查询耗尽配额。
- 研究环境 Python 版本较旧：不要用 ``list[...]`` 这类 3.9+ 注解。
"""
import datetime as dt

import pandas as pd

# 聚宽研究环境的标准导入方式；必须位于行首。
from jqdata import *  # noqa: F401,F403

START = "2016-09-30"
END = "2026-09-30"
OUTPUT = "聚宽行业历史导出.csv"


def month_end_dates():
    """区间内每个自然月的最后一个交易日。"""

    days = list(get_trade_days(START, END))
    out = []
    for index, day in enumerate(days):
        if index + 1 == len(days) or days[index + 1].month != day.month:
            out.append(day)
    return out


def _pick(frame, group, field):
    """兼容 MultiIndex 列（('sw_l1','industry_code')）与平铺列两种返回。"""

    try:
        value = frame[(group, field)]
    except (KeyError, IndexError):
        return None
    return value


def export_date(asof):
    stocks = get_all_securities(['stock'], date=asof).index.tolist()
    result = get_industry(stocks, date=asof)
    rows = []
    if isinstance(result, pd.DataFrame):
        frame = result
        for code in frame.index:
            row = {'date': str(asof), 'code': code}
            for group, prefix in (('sw_l1', 'sw_l1'), ('sw_l2', 'sw_l2'),
                                  ('jq_l1', 'jq_l1'), ('jq_l2', 'jq_l2')):
                code_value = _pick(frame, group, 'industry_code')
                name_value = _pick(frame, group, 'industry_name')
                row['%s_code' % prefix] = (
                    code_value.get(code) if code_value is not None else None
                )
                if prefix.startswith('sw'):
                    row['%s_name' % prefix] = (
                        name_value.get(code) if name_value is not None else None
                    )
            rows.append(row)
        return rows
    for code, payload in (result or {}).items():
        row = {'date': str(asof), 'code': code}
        for group, prefix in (('sw_l1', 'sw_l1'), ('sw_l2', 'sw_l2'),
                              ('jq_l1', 'jq_l1'), ('jq_l2', 'jq_l2')):
            item = (payload or {}).get(group) or {}
            row['%s_code' % prefix] = item.get('industry_code')
            if prefix.startswith('sw'):
                row['%s_name' % prefix] = item.get('industry_name')
        rows.append(row)
    return rows


def main():
    dates = month_end_dates()
    print("快照日期 {} 个：{} ~ {}".format(len(dates), dates[0], dates[-1]))
    rows = []
    for index, asof in enumerate(dates, 1):
        try:
            rows.extend(export_date(asof))
        except Exception as exc:  # 单日失败不中断
            print("跳过 {}：{}".format(asof, exc))
        if index % 10 == 0:
            print("进度 {}/{}，累计 {} 行".format(index, len(dates), len(rows)))
    frame = pd.DataFrame(rows)
    if "code" in frame.columns:
        frame = frame.rename(columns={"code": "symbol"})
    for column in ("sw_l1_code", "sw_l2_code", "jq_l1_code", "jq_l2_code"):
        if column in frame.columns:
            frame[column] = frame[column].map(
                lambda value: "" if value is None or pd.isna(value) else str(int(value))
                if isinstance(value, float) and float(value).is_integer()
                else str(value)
            )
    frame.to_csv(OUTPUT, index=False, encoding="utf-8-sig")
    print("已写出 {}：{} 行".format(OUTPUT, len(frame)))


main()
