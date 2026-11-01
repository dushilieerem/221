"""加载聚宽模块级策略，并包装成事件引擎可运行的适配器。"""

from __future__ import annotations

import re
from typing import Any

from app.services.strategy.compat.runtime import JoinQuantRuntime

#: 聚宽/因子库的导入行由兼容层提供同名全局，执行前移除。
_IMPORT_PATTERN = re.compile(
    r"^\s*(?:"
    r"from\s+(?:jqdata|jqfactor|jqlib\.[\w.]+)\s+import\s+.*"
    r"|import\s+(?:jqdata|jqfactor)\b.*"
    r")$",
    re.MULTILINE,
)


class JoinQuantStrategyAdapter:
    """把模块级 ``initialize`` 包装成事件引擎需要的对象。"""

    def __init__(self, runtime: JoinQuantRuntime, namespace: dict[str, Any]) -> None:
        self._runtime = runtime
        self._namespace = namespace

    def initialize(self, context) -> None:
        self._runtime.bind(context)
        if getattr(context, "market", None) is not None:
            from app.services.strategy.compat.portfolio import wrap_portfolio

            context.portfolio = wrap_portfolio(context.portfolio, context.market)
        initialize = self._namespace.get("initialize")
        if not callable(initialize):
            raise ValueError("聚宽策略必须定义模块级 initialize(context)")
        initialize(context)

    @property
    def runtime(self) -> JoinQuantRuntime:
        return self._runtime


def strip_joinquant_imports(source: str) -> str:
    """移除 jqdata/jqfactor/jqlib 导入行，便于静态护栏检查。"""

    return _IMPORT_PATTERN.sub("", source)


def load_joinquant_strategy(
    source: str,
    *,
    name: str = "joinquant_strategy",
    fundamentals=None,
    factors=None,
    master=None,
    index=None,
    industry=None,
    index_prices=None,
) -> JoinQuantStrategyAdapter:
    """编译聚宽源码并返回适配器；检测到 BigQuant API 时明确拒绝。"""

    if "bigmodule" in source or "bigtrader" in source:
        raise ValueError(
            "检测到 BigQuant API（bigmodule/bigtrader），该文件不是聚宽策略，"
            "不能走聚宽兼容层"
        )

    cleaned = strip_joinquant_imports(source)
    runtime = JoinQuantRuntime(
        fundamentals=fundamentals,
        factors=factors,
        master=master,
        index=index,
        industry=industry,
        index_prices=index_prices,
    )
    namespace: dict[str, Any] = {"__name__": name, **runtime.globals()}
    exec(compile(cleaned, f"<{name}>", "exec"), namespace)
    if not callable(namespace.get("initialize")):
        raise ValueError("聚宽策略必须定义模块级 initialize(context)")
    return JoinQuantStrategyAdapter(runtime, namespace)
