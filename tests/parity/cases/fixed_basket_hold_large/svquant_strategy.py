"""SVQuant 侧：固定篮子持有，与同目录 jq_strategy.py 对照。

规则：
* 第 2 个交易日开盘等权买入固定 4 只，之后持有不动；
* 预算 95%，给佣金与整手取整留余量；
* 声明 up_and_down_status，让撮合层的停牌/涨跌停拦截生效。
"""


class FixedBasketHold:
    fixed_symbols = ("000001.SZ", "600000.SH", "600036.SH", "601398.SH")
    weights = {
        "000001.SZ": 0.25,
        "600000.SH": 0.25,
        "600036.SH": 0.25,
        "601398.SH": 0.25,
    }
    budget = 0.95
    required_fields = frozenset({"open", "high", "low", "close"})
    required_market_state = frozenset({"up_and_down_status"})

    def initialize(self, context):
        context.state.calls = 0
        context.state.done = False
        context.schedule.daily(self.rebalance)

    def rebalance(self, context):
        if context.state.done:
            return
        context.state.calls += 1
        if context.state.calls < 2:
            return

        total = context.portfolio.cash
        for symbol in self.fixed_symbols:
            context.orders.target_value(
                symbol, total * self.weights[symbol] * self.budget
            )
        context.state.done = True
