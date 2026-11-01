"""聚宽 ``query()`` 构造器的最小实现。

只覆盖 13 个策略实际用到的写法：``query(field, ...).filter(...).order_by(...).limit(n)``，
字段比较（``>`` / ``<`` / ``==`` / ``in_``）以及 ``&`` / ``|`` 组合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Field:
    __slots__ = ("table", "name")

    def __init__(self, table: str, name: str) -> None:
        self.table = table
        self.name = name

    def __repr__(self) -> str:
        return f"{self.table}.{self.name}"

    def __hash__(self) -> int:
        return hash((self.table, self.name))

    def _cmp(self, op: str, other: Any) -> "Condition":
        return Condition(self, op, other)

    def __eq__(self, other: Any) -> "Condition":  # type: ignore[override]
        return self._cmp("==", other)

    def __ne__(self, other: Any) -> "Condition":  # type: ignore[override]
        return self._cmp("!=", other)

    def __gt__(self, other: Any) -> "Condition":
        return self._cmp(">", other)

    def __ge__(self, other: Any) -> "Condition":
        return self._cmp(">=", other)

    def __lt__(self, other: Any) -> "Condition":
        return self._cmp("<", other)

    def __le__(self, other: Any) -> "Condition":
        return self._cmp("<=", other)

    def in_(self, values: Any) -> "Condition":
        return self._cmp("in", values)

    def not_in(self, values: Any) -> "Condition":
        return self._cmp("not in", values)

    def asc(self) -> "Order":
        return Order(self, ascending=True)

    def desc(self) -> "Order":
        return Order(self, ascending=False)


@dataclass(frozen=True)
class Order:
    field: Field
    ascending: bool = True


@dataclass(frozen=True)
class Condition:
    left: Any
    op: str
    right: Any = None

    def __and__(self, other: "Condition") -> "Condition":
        return Condition(self, "and", other)

    def __or__(self, other: "Condition") -> "Condition":
        return Condition(self, "or", other)


@dataclass
class Query:
    fields: tuple[Field, ...]
    where: Condition | None = None
    orders: tuple[Order, ...] = ()
    limit_count: int | None = None

    def filter(self, *conditions: Condition) -> "Query":
        merged = self.where
        for condition in conditions:
            merged = condition if merged is None else (merged & condition)
        return Query(self.fields, merged, self.orders, self.limit_count)

    def order_by(self, *orders: Order) -> "Query":
        return Query(self.fields, self.where, tuple(orders), self.limit_count)

    def limit(self, count: int) -> "Query":
        return Query(self.fields, self.where, self.orders, int(count))


class Entity:
    def __init__(self, table: str) -> None:
        self.table = table

    def __getattr__(self, name: str) -> Field:
        return Field(self.table, name)


valuation = Entity("valuation")
indicator = Entity("indicator")
balance = Entity("balance")
income = Entity("income")
cash_flow = Entity("cash_flow")


def query(*fields: Field) -> Query:
    return Query(tuple(fields))


def evaluate(frame, condition: Condition | None):
    """在 pandas DataFrame 上执行条件；列名为 ``<table>.<field>``。"""

    if condition is None:
        return frame.index == frame.index  # all True
    if condition.op == "and":
        return evaluate(frame, condition.left) & evaluate(frame, condition.right)
    if condition.op == "or":
        return evaluate(frame, condition.left) | evaluate(frame, condition.right)

    column = f"{condition.left.table}.{condition.left.name}"
    if column not in frame.columns:
        raise KeyError(f"查询字段不存在: {column}")
    series = frame[column]
    op = condition.op
    if op == "==":
        return series == condition.right
    if op == "!=":
        return series != condition.right
    if op == ">":
        return series > condition.right
    if op == ">=":
        return series >= condition.right
    if op == "<":
        return series < condition.right
    if op == "<=":
        return series <= condition.right
    if op == "in":
        return series.isin(list(condition.right))
    if op == "not in":
        return ~series.isin(list(condition.right))
    raise ValueError(f"不支持的条件: {op}")
