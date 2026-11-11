"""聚宽兼容层的能力清单（供 WebApp 预检与文档展示）。

清单是人工维护的静态契约；``tests/quantcore/test_joinquant_compat.py`` 会
断言 ``SUPPORTED_APIS`` 都能在真实运行时命名空间里找到，避免与实现漂移。
"""

from __future__ import annotations

CAPABILITY_VERSION = "2026-11-11"

SUPPORTED_APIS = (
    "g",
    "log",
    "run_daily",
    "run_weekly",
    "run_monthly",
    "set_option",
    "set_benchmark",
    "set_order_cost",
    "set_slippage",
    "set_universe",
    "order",
    "order_value",
    "order_target",
    "order_target_value",
    "order_target_percent",
    "get_current_data",
    "get_price",
    "attribute_history",
    "history",
    "get_fundamentals",
    "get_history_fundamentals",
    "get_all_securities",
    "get_security_info",
    "get_trade_days",
    "get_all_trade_days",
    "get_industry",
    "get_industry_stocks",
    "get_index_stocks",
    "get_extras",
    "get_orders",
    "get_open_orders",
    "cancel_order",
    "get_trades",
    "query",
    "valuation",
    "indicator",
    "balance",
    "income",
    "cash_flow",
    "finance",
    "get_factor_values",
    "calc_factors",
)

UNSUPPORTED_APIS = (
    {"api": "get_call_auction", "reason": "集合竞价数据未发布"},
    {"api": "get_price(frequency='1m')", "reason": "分钟数据仅供 timed_execution 内部使用"},
    {"api": "finance.STK_*（STK_XR_XD、STK_INCOME_STATEMENT 除外）", "reason": "finance 表仅实现两张"},
    {"api": "inout_cash", "reason": "不支持资金划转"},
    {"api": "get_fundamentals(statDate=...)", "reason": "暂只支持 date/as_of 口径"},
    {"api": "get_history_fundamentals(stat_by_year=True)", "reason": "暂未支持"},
    {"api": "get_extras（is_st 除外）", "reason": "暂只支持 is_st"},
)


def capability_manifest(*, data: dict[str, object] | None = None) -> dict[str, object]:
    """返回给调用方的能力清单；``data`` 由路由补充当前视图信息。"""

    return {
        "version": CAPABILITY_VERSION,
        "dialects": {
            "native": {"engine_modes": ["vectorized", "event"]},
            "joinquant": {
                "engine_modes": ["event"],
                "frequency": ["1d"],
                "timed_execution": True,
                "price_sources": ["unadjusted"],
                "supported_apis": list(SUPPORTED_APIS),
                "unsupported_apis": list(UNSUPPORTED_APIS),
            },
        },
        "data": data or {},
    }


__all__ = [
    "CAPABILITY_VERSION",
    "SUPPORTED_APIS",
    "UNSUPPORTED_APIS",
    "capability_manifest",
]
