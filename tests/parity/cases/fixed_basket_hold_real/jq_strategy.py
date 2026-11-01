"""聚宽：真实价格撮合对账用例。

聚宽回测设置：
* 频率：日频
* 区间：2024-01-02 ~ 2024-06-13（避开 000001.SZ 2024-06-14 除息）
* 初始资金：100,000,000
* 基准：沪深300

本文件开启 ``use_real_price=True``，让聚宽用真实价格成交、整手和计费，
与 SVQuant 的 ``execution_price_source=unadjusted`` 对齐。
"""

SYMBOLS = ["000001.XSHE", "600000.XSHG", "600036.XSHG", "601398.XSHG"]
WEIGHTS = {
    "000001.XSHE": 0.25,
    "600000.XSHG": 0.25,
    "600036.XSHG": 0.25,
    "601398.XSHG": 0.25,
}
BUDGET = 0.95


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
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

    # 第 2 个交易日才建仓，与 SVQuant 在 next_open 下首日无可见历史一致。
    g.calls += 1
    if g.calls < 2:
        return

    total = context.portfolio.total_value
    for symbol in SYMBOLS:
        order_target_value(symbol, total * WEIGHTS[symbol] * BUDGET)
    g.done = True
