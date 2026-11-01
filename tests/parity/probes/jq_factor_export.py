"""在聚宽研究环境导出因子与基本面样本，用于本地逐值校验。

用法：
1. 聚宽研究环境新建 Notebook，粘贴本文件；
2. 运行后下载 OUT_DIR 下的 factor_values.csv 与 fundamentals.csv；
3. 把两个文件路径发给对账负责人。

只读导出，不修改任何聚宽数据。
"""

import os

import pandas as pd
from jqdata import *  # noqa: F401,F403

# 研究环境里 get_factor_values 属于 jqfactor，不在 jqdata 的全局命名空间。
try:
    from jqfactor import get_factor_values
except ImportError:  # pragma: no cover - 兼容不同聚宽环境
    import jqfactor

    get_factor_values = jqfactor.get_factor_values

OUT_DIR = "<在此填写研究环境中可写的目录>"

STOCKS = [
    "600519.XSHG", "000001.XSHE", "300750.XSHE", "600036.XSHG", "000858.XSHE",
    "002415.XSHE", "601318.XSHG", "000333.XSHE", "600276.XSHG", "002594.XSHE",
    "601398.XSHG", "600000.XSHG", "600030.XSHG", "000651.XSHE", "002475.XSHE",
    "300059.XSHE", "600887.XSHG", "601888.XSHG", "000725.XSHE", "002714.XSHE",
]
DATES = ["2019-06-28", "2021-06-30", "2023-12-29", "2025-06-30", "2026-06-30"]

FACTORS = [
    "earnings_growth", "sales_growth", "DEGM",
    "operating_revenue_growth_rate", "total_profit_growth_rate",
    "np_parent_company_owners_growth_rate", "np_parent_company_owners_ttm",
    "non_recurring_gain_loss", "net_operate_cash_flow_ttm",
    "net_invest_cash_flow_ttm", "total_operating_revenue_per_share_ttm",
    "operating_cost_ttm", "operating_profit_ttm", "sale_expense_ttm",
    "EBIT", "market_cap", "sharpe_ratio_60", "DAVOL20",
    "long_term_predicted_earnings_growth",
]


def export_factors():
    rows = []
    for date in DATES:
        for factor in FACTORS:
            # 逐因子调用：某个因子在当前账号不可用时只跳过它，不影响其他因子。
            try:
                data = get_factor_values(STOCKS, [factor], end_date=date, count=1)
            except Exception as exc:  # noqa: BLE001
                print("skip", date, factor, exc)
                continue
            frame = data.get(factor)
            if frame is None or frame.empty:
                print("empty", date, factor)
                continue
            series = frame.iloc[0]
            for stock in STOCKS:
                rows.append({
                    "date": date,
                    "code": stock,
                    "factor": factor,
                    "value": series.get(stock),
                })
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT_DIR, "factor_values.csv"), index=False)
    print("factor_values", df.shape)


def export_fundamentals():
    rows = []
    q = query(
        valuation.code, valuation.pb_ratio, valuation.market_cap,
        valuation.circulating_market_cap, valuation.pe_ratio, valuation.ps_ratio,
        indicator.eps, indicator.roa, indicator.roe,
        indicator.gross_profit_margin, indicator.inc_return,
        indicator.inc_net_profit_year_on_year,
        indicator.inc_total_revenue_year_on_year,
        indicator.ocf_to_revenue, indicator.adjusted_profit_to_profit,
        balance.cash_equivalents, balance.total_assets,
        balance.total_liability, balance.total_owner_equities,
        income.operating_revenue, income.net_profit,
        income.np_parent_company_owners,
        cash_flow.net_operate_cash_flow,
        cash_flow.fix_intan_other_asset_acqui_cash,
    ).filter(valuation.code.in_(STOCKS))
    for date in DATES:
        df = get_fundamentals(q, date=date)
        df = df.copy()
        df.insert(0, "date", date)
        rows.append(df)
    out = pd.concat(rows, ignore_index=True)
    out.to_csv(os.path.join(OUT_DIR, "fundamentals.csv"), index=False)
    print("fundamentals", out.shape)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    export_factors()
    export_fundamentals()
