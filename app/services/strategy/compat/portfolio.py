"""聚宽兼容层的组合视图：Position 对象语义。

原生 ``PortfolioView.positions`` 是 ``{symbol: int}``；聚宽策略需要
``{symbol: Position}``，并访问 ``total_amount`` / ``avg_cost`` / ``price`` 等属性。
"""

from __future__ import annotations

from typing import Any

from app.services.strategy.compat.runtime import normalize_security


class JQPosition:
    def __init__(self, symbol: str, quantity: int, average_cost: float, price: float | None) -> None:
        self.security = symbol
        self._quantity = int(quantity)
        self.avg_cost = float(average_cost or 0.0)
        self.price = float(price) if price is not None else float("nan")

    @property
    def total_amount(self) -> int:
        return self._quantity

    @property
    def amount(self) -> int:
        return self._quantity

    @property
    def quantity(self) -> int:
        return self._quantity

    @property
    def closeable_amount(self) -> int:
        return self._quantity

    @property
    def value(self) -> float:
        return self._quantity * self.price

    @property
    def acc_avg_cost(self) -> float:
        return self.avg_cost


class JQPortfolioView:
    """包一层原生 PortfolioView，向策略暴露聚宽 Position 对象。"""

    def __init__(self, view: Any, market: Any) -> None:
        self._view = view
        self._market = market
        self._positions_cache: _PositionsView | None = None
        self._positions_key: tuple[Any, ...] | None = None

    @property
    def cash(self) -> float:
        return float(self._view.cash)

    @property
    def available_cash(self) -> float:
        # 兼容层不做冻结资金建模；未成交订单不占用现金。
        return self.cash

    @property
    def portfolio_value(self) -> float:
        """聚宽 ``portfolio.portfolio_value`` 与 ``total_value`` 同义。"""

        return self.total_value

    @property
    def returns(self) -> float:
        engine = getattr(self._market, "_engine", None)
        initial = getattr(getattr(engine, "config", None), "initial_capital", None)
        if not initial:
            return 0.0
        return float(self.total_value) / float(initial) - 1.0

    @property
    def positions(self) -> "_PositionsView":
        """同一日期/时点且持仓未变时复用 Position 对象，避免反复构造。"""

        engine = getattr(self._market, "_engine", None)
        stamp = (
            getattr(engine, "_current_date", None),
            getattr(engine, "_current_time", None),
        )
        snapshot = tuple(
            sorted(
                (str(symbol), int(quantity))
                for symbol, quantity in self._view.positions.items()
                if int(quantity) > 0
            )
        )
        key = (*stamp, snapshot)
        if self._positions_cache is None or self._positions_key != key:
            self._positions_cache = _PositionsView(self._view, self._market)
            self._positions_key = key
        return self._positions_cache

    def quantity(self, symbol: str) -> int:
        return int(self._view.quantity(normalize_security(symbol)))

    @property
    def total_value(self) -> float:
        total = self.cash
        for position in self.positions.values():
            if position.price == position.price:  # not NaN
                total += position.value
        return total


class _PositionsView(dict):
    """聚宽 positions 语义：索引任意标的都返回 Position（未持有为 0）。"""

    def __init__(self, view: Any, market: Any) -> None:
        super().__init__()
        self._market = market
        for symbol, quantity in view.positions.items():
            if int(quantity) <= 0:
                continue
            display = self._display(str(symbol))
            self[display] = JQPosition(
                display,
                int(quantity),
                view.average_cost(str(symbol)) or 0.0,
                market.current_price(str(symbol)),
            )

    def _display(self, symbol: Any) -> str:
        """统一成策略侧写法：去聚宽后缀再按引擎别名回填（ETF 用原写法）。"""

        normalized = normalize_security(symbol)
        display = getattr(self._market, "display_symbol", None)
        return display(normalized) if callable(display) else normalized

    def __getitem__(self, symbol: str) -> JQPosition:
        return dict.__getitem__(self, self._display(symbol))

    def __contains__(self, symbol: object) -> bool:
        return dict.__contains__(self, self._display(symbol))

    def get(self, symbol: str, default: Any = None) -> Any:
        return dict.get(self, self._display(symbol), default)

    def __missing__(self, symbol: str) -> JQPosition:
        return JQPosition(symbol, 0, 0.0, self._market.current_price(normalize_security(symbol)))


def wrap_portfolio(view: Any, market: Any) -> JQPortfolioView:
    return JQPortfolioView(view, market)
