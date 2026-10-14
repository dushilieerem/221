"""``NativeEngine`` - 自研向量化回测后端。

统一实现前一日目标权重对应下一交易日收盘收益的向量化语义：收益序列保留首个信号
持有期，展示权益曲线从 ``initial_capital`` 开始按复利累计。``cost > 0`` 时按换手率
``|Δ权重|`` 计入手续费/滑点。
"""

from __future__ import annotations

import pandas as pd

from app.services.strategy.vectorized.interfaces import IBacktestEngine
from app.models.strategy import BacktestConfig, BacktestSignals
from app.models.backtest import BacktestResult
from app.services.portfolio.records import rebalance_logs_from_targets


class NativeEngine(IBacktestEngine):
    """自研向量化回测引擎。"""

    name: str = "native"

    @staticmethod
    def _next_open_returns(
        target: pd.DataFrame,
        close: pd.DataFrame,
        open_prices: pd.DataFrame | None,
    ) -> tuple[pd.Series, pd.DataFrame]:
        """信号次日开盘成交的收益与持仓序列。

        当日收益拆成两段：隔夜跳空由**昨日**持仓承担（开盘才换仓），日内由
        **今日**持仓承担。权重不变时两段相加退化为收盘价对收盘价，只差二阶
        交叉项；建仓当日只赚日内、清仓当日只赚跳空，都与真实成交一致。

        持仓位移与「信号日收盘成交」一致（``shift(1)``），差别只在这条收益
        公式上。
        """

        if open_prices is None:
            raise ValueError(
                "execution_timing='next_open' 需要开盘价，但 BacktestSignals "
                "未提供 open；请在编排层传入行情面板的 open 字段"
            )
        held = target.ffill().shift(1).fillna(0.0)
        overnight_gap = open_prices / close.shift(1) - 1.0
        intraday = close / open_prices.replace(0.0, float("nan")) - 1.0
        port_ret = (held.shift(1).fillna(0.0) * overnight_gap).sum(axis=1) + (
            held * intraday
        ).sum(axis=1)
        return port_ret, held

    def run(
        self,
        signals: BacktestSignals,
        config: BacktestConfig,
    ) -> BacktestResult:
        target = signals.target_weights
        close = signals.close
        timing = config.execution_timing

        if timing == "next_open":
            port_ret, held = self._next_open_returns(target, close, signals.open)
        elif timing in {"same_close", "next_close"}:
            # 持仓生效日：same_close 在信号日收盘成交、次日生效（lag=1）；
            # next_close 在信号次日收盘成交、再次日生效（lag=2）。两者收益
            # 都用收盘价对收盘价，只差这一天的位移。
            lag = 1 if timing == "same_close" else 2
            held = target.ffill().shift(lag).fillna(0.0)
            port_ret = (held * close.pct_change(fill_method=None)).sum(axis=1)
        else:
            raise ValueError(
                f"不支持的 execution_timing: {timing!r}，"
                "可选 same_close | next_close | next_open"
            )

        # 可选成本（默认 0 -> 与 build_factor_portfolio 逐位一致）
        if config.commission_rate or config.slippage_rate or config.stamp_tax:
            weight_change = held.diff().fillna(0.0)
            turnover = weight_change.abs().sum(axis=1)
            sell_turnover = (-weight_change.clip(upper=0.0)).sum(axis=1)
            port_ret = port_ret - turnover * (
                config.commission_rate + config.slippage_rate
            ) - sell_turnover * config.stamp_tax

        # 去掉信号日。首个保留日包含首个信号产生的收益。
        port_ret = port_ret.iloc[1:]
        port_ret = port_ret.dropna()

        if config.initial_capital <= 0:
            raise ValueError("initial_capital 必须大于 0")

        # 展示曲线不计首个信号日的收益：在次日收盘保持初始资金，后续复利。
        display_returns = port_ret.copy()
        display_returns.iloc[0] = 0.0
        equity = float(config.initial_capital) * (1.0 + display_returns).cumprod()
        positions = held.loc[port_ret.index].copy()
        return BacktestResult(
            equity_curve=equity,
            returns=port_ret,
            positions=positions,
            rebalance_logs=rebalance_logs_from_targets(target),
        )
