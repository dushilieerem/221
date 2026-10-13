"""量价复合选股策略：夏普、量能拥挤度与量价配合三因子合成，等权持有 Top30。

迁移自聚宽平台的中证500 选股策略。原策略在每个调仓日按下列公式打分：

    得分 = rank(60日夏普) - rank(20日成交量拥挤度) + rank(20日量价配合)

取得分最高的 30 只等权买入，每 20 个交易日调仓一次。三个分量均为横截面
百分位，组合语义由 ``selector`` / ``weighting`` / ``rebalance`` 声明。
"""

from app.services.strategy.vectorized.factor_strategy import (
    EqualWeight,
    EveryNTradingDays,
    FactorPortfolioStrategy,
    TopN,
    factor,
    signal,
)


class VolumePriceCompositeStrategy(FactorPortfolioStrategy):
    """量价复合选股：每 20 个交易日等权调仓到得分最高的 30 只。"""

    selector = TopN(30)
    weighting = EqualWeight()
    rebalance = EveryNTradingDays(20)

    # -- 因子 --
    @factor
    def sharpe_60d(self):
        """60 日夏普代理：日收益均值除以标准差后年化，越高越好。"""
        returns = self._returns()
        mean = returns.rolling(60).mean()
        std = returns.rolling(60).std().replace(0.0, float("nan"))
        return mean.div(std) * (252 ** 0.5)

    @factor
    def volume_crowding_20d(self):
        """成交量拥挤度：20 日均量相对 60 日均量的偏离，越高越拥挤。"""
        volume_20 = self.volume.rolling(20).mean()
        volume_60 = self.volume.rolling(60).mean().replace(0.0, float("nan"))
        return volume_20.div(volume_60) - 1.0

    @factor
    def volume_confirmed_momentum_20d(self):
        """量价配合：日收益按相对成交量加权后的 20 日均值，放量上涨得高分。"""
        returns = self._returns()
        average_volume_20 = self.volume.rolling(20, min_periods=10).mean()
        relative_volume = self.volume.div(
            average_volume_20.replace(0.0, float("nan"))
        )
        return (returns * relative_volume).rolling(20).mean()

    @signal
    def score(self):
        """三个分量的横截面百分位合成：高夏普、低拥挤、强量价配合优先。"""
        score = (
            self.rank(self.sharpe_60d())
            - self.rank(self.volume_crowding_20d())
            + self.rank(self.volume_confirmed_momentum_20d())
        )
        score = score.replace([float("inf"), float("-inf")], float("nan"))
        # 剔除不可交易标的后，其得分置 NaN，不会进入 TopN 选择。
        return score.where(self._tradable_mask())

    # -- 内部辅助 --
    def _returns(self):
        """日收益；停牌日沿用前一收盘价，与原策略的 ffill 口径一致。"""
        return self.close.ffill().pct_change(fill_method=None)

    def _tradable_mask(self):
        """剔除 ST、*ST、退市整理和停牌标的；状态缺失按可交易处理。"""
        listed = self.listed_status
        status = self.up_and_down_status
        excluded = listed.isin(["ST", "*ST", "退市整理"]) | status.eq("停牌")
        return ~excluded
