"""Donchian-inspired A 股突破趋势策略的 SVQuant 事件策略版本。"""

from __future__ import annotations

import numpy as np
import pandas as pd


class DonchianAShareEventStrategy:
    """日频长仓突破策略；股票池和市场状态须由回测数据面板事先按历史时点过滤。"""

    execution_mode = "event"
    required_fields = frozenset({"open", "high", "low", "close", "amount"})
    required_market_state = frozenset({"up_and_down_status"})

    def __init__(
        self,
        benchmark_symbol: str = "000300.SH",
        universe: list[str] | None = None,
        max_holdings: int = 10,
        single_weight: float = 0.09,
    ) -> None:
        self.benchmark_symbol = benchmark_symbol
        self.universe = universe
        self.max_holdings = max_holdings
        self.single_weight = single_weight

    def initialize(self, context) -> None:
        missing = self.required_fields - context.data.fields
        if missing:
            raise ValueError(f"唐奇安 A 股策略缺少数据字段: {sorted(missing)}")
        context.state.peak_close = {}
        context.schedule.daily(self.rebalance)

    def rebalance(self, context) -> None:
        symbols = self.universe or [symbol for symbol in context.data.symbols if symbol != self.benchmark_symbol]
        history = context.data.history(symbols, ["close", "high", "low", "amount"], 121)
        benchmark_history = context.data.history([self.benchmark_symbol], ["close"], 120)
        if len(history["close"]) < 121 or self.benchmark_symbol not in benchmark_history["close"]:
            return

        metrics = self._metrics(history, symbols)
        holdings = set(context.portfolio.positions)
        survivors = self._exit_positions(context, metrics, holdings)
        if not self._market_allows_entry(benchmark_history["close"]):
            return

        slots = self.max_holdings - len(survivors)
        if slots <= 0:
            return
        candidates = [
            (symbol, item["score"])
            for symbol, item in metrics.items()
            if symbol not in holdings and item["breakout"] and item["money20"] >= 30_000_000 and item["atr_pct"] <= 0.08
        ]
        candidates.sort(key=lambda item: item[1], reverse=True)
        total_value = context.portfolio.cash + sum(
            context.portfolio.quantity(symbol) * metrics[symbol]["close"]
            for symbol in holdings if symbol in metrics
        )
        for symbol, _ in candidates[:slots]:
            context.orders.target_value(symbol, total_value * self.single_weight)

    def _exit_positions(self, context, metrics, holdings):
        survivors = set()
        for symbol in holdings:
            item = metrics.get(symbol)
            if item is None:
                context.orders.close(symbol)
                continue
            peak = max(context.state.peak_close.get(symbol, item["close"]), item["close"])
            context.state.peak_close[symbol] = peak
            if item["close"] < item["lower"] or item["close"] < peak - 3 * item["atr"]:
                context.orders.close(symbol)
                context.state.peak_close.pop(symbol, None)
            else:
                survivors.add(symbol)
        return survivors

    def _metrics(self, history, symbols):
        result = {}
        close_frame = history["close"].reindex(columns=symbols)
        high_frame = history["high"].reindex(columns=symbols)
        low_frame = history["low"].reindex(columns=symbols)
        money_frame = history["amount"].reindex(columns=symbols)

        # 大多数股票在窗口内没有缺失值时，整列计算 ATR/通道，避免对每只
        # 股票重复调用 pandas 的 concat/max。缺失数据的少数股票仍走下面的
        # 压缩序列路径，保持原有点时数据处理语义。
        complete = [
            symbol
            for symbol in symbols
            if close_frame[symbol].notna().all()
            and high_frame[symbol].notna().all()
            and low_frame[symbol].notna().all()
            and money_frame[symbol].notna().all()
        ]
        if complete:
            close_values = close_frame[complete].to_numpy(dtype=float)
            high_values = high_frame[complete].to_numpy(dtype=float)
            low_values = low_frame[complete].to_numpy(dtype=float)
            money_values = money_frame[complete].to_numpy(dtype=float)
            previous_close = np.empty_like(close_values)
            previous_close[0, :] = np.nan
            previous_close[1:, :] = close_values[:-1, :]
            true_range = np.maximum.reduce(
                [
                    high_values - low_values,
                    np.abs(high_values - previous_close),
                    np.abs(low_values - previous_close),
                ]
            )
            atr = true_range[-20:, :].mean(axis=0)
            close_now = close_values[-1, :]
            upper = high_values[-21:-1, :].max(axis=0)
            lower = low_values[-21:-1, :].min(axis=0)
            money20 = money_values[-20:, :].mean(axis=0)
            money60 = money_values[-60:, :].mean(axis=0)
            for index, symbol in enumerate(complete):
                if atr[index] <= 0 or close_now[index] <= 0 or money60[index] <= 0:
                    continue
                score = (
                    0.55 * (close_now[index] / close_values[-61, index] - 1)
                    + 0.30 * ((close_now[index] - upper[index]) / atr[index])
                    + 0.15 * (money20[index] / money60[index] - 1)
                )
                result[symbol] = {
                    "close": float(close_now[index]), "lower": float(lower[index]),
                    "atr": float(atr[index]), "atr_pct": float(atr[index] / close_now[index]),
                    "money20": float(money20[index]), "breakout": bool(close_now[index] > upper[index]),
                    "score": float(score),
                }

        for symbol in symbols:
            if symbol in result:
                continue
            close = history["close"][symbol].dropna()
            high = history["high"][symbol].reindex(close.index)
            low = history["low"][symbol].reindex(close.index)
            money = history["amount"][symbol].reindex(close.index)
            if len(close) < 61 or high.isna().any() or low.isna().any() or money.isna().any():
                continue
            close_values = close.to_numpy(dtype=float)
            high_values = high.to_numpy(dtype=float)
            low_values = low.to_numpy(dtype=float)
            money_values = money.to_numpy(dtype=float)
            previous_close = np.empty_like(close_values)
            previous_close[0] = np.nan
            previous_close[1:] = close_values[:-1]
            true_range = np.maximum.reduce(
                [
                    high_values - low_values,
                    np.abs(high_values - previous_close),
                    np.abs(low_values - previous_close),
                ]
            )
            atr = true_range[-20:].mean()
            close_now = close_values[-1]
            upper = high_values[-21:-1].max()
            lower = low_values[-21:-1].min()
            money20 = money_values[-20:].mean()
            money60 = money_values[-60:].mean()
            if atr <= 0 or close_now <= 0 or money60 <= 0:
                continue
            score = 0.55 * (close_now / close_values[-61] - 1) + 0.30 * ((close_now - upper) / atr) + 0.15 * (money20 / money60 - 1)
            result[symbol] = {
                "close": float(close_now), "lower": float(lower), "atr": float(atr),
                "atr_pct": float(atr / close_now), "money20": float(money20),
                "breakout": bool(close_now > upper), "score": float(score),
            }
        return result

    def _market_allows_entry(self, close):
        benchmark = close[self.benchmark_symbol].dropna()
        return len(benchmark) >= 120 and benchmark.iloc[-1] > benchmark.iloc[-120:].mean()
