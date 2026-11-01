"""聚宽侧：固定篮子持有。

把本文件内容粘贴到聚宽回测编辑器，按 case.yaml 设置回测区间与初始资金后运行。
两个平台都按"第 2 个交易日开盘"建仓，避免首日历史数据可用性差异。
"""

import pandas as pd

SYMBOLS = ["000001.XSHE", "600000.XSHG", "600036.XSHG", "601398.XSHG"]
WEIGHTS = {
    "000001.XSHE": 0.25,
    "600000.XSHG": 0.25,
    "600036.XSHG": 0.25,
    "601398.XSHG": 0.25,
}
BUDGET = 0.95  # 留 5% 覆盖佣金与整手取整


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", False)
    set_option("avoid_future_data", True)
    set_order_cost(
        OrderCost(
            open_tax=0.0,
            close_tax=0.0005,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0.0,
            min_commission=5.0,
        ),
        type="stock",
    )
    set_slippage(PriceRelatedSlippage(0.0))
    g.calls = 0
    g.done = False
    run_daily(trade_once, time="09:30")


def trade_once(context):
    if g.done:
        return

    # 第 2 个交易日才建仓：与 SVQuant 在 next_open 下首日无可见历史保持一致。
    g.calls += 1
    if g.calls < 2:
        return

    total = context.portfolio.total_value
    for symbol in SYMBOLS:
        order_target_value(symbol, total * WEIGHTS[symbol] * BUDGET)
    g.done = True
