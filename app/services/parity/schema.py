"""聚宽对账基线的公共契约。

集中定义 schema 版本、列别名、符号映射与容差，供 loader / normalizer /
compare / report 共用。本模块只依赖 pandas，不导入回测引擎。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping

import pandas as pd

#: 金标与报告的 schema 版本；不兼容变更时递增。
GOLDEN_SCHEMA_VERSION = 1

#: 聚宽交易所后缀 -> 本仓库后缀。
JQ_EXCHANGE_SUFFIX = {"XSHG": "SH", "XSHE": "SZ", "XBSE": "BJ"}

#: 统一内部列名 -> 允许的原始列名（按优先级）。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("time", "date", "datetime", "timestamp"),
    "symbol": ("security", "symbol", "code", "ticker"),
    "quantity": ("amount", "quantity", "filled", "total_amount"),
    "closeable": ("closeable_amount", "sellable_amount", "closeable", "sellable_quantity"),
    "cash": ("cash", "available_cash"),
    "equity": ("total_value", "equity", "total_assets", "net_value"),
    "returns": ("returns", "return", "strategy_returns", "daily_returns"),
    "benchmark_returns": ("benchmark_returns", "benchmark_return"),
    "side": ("action", "side"),
    "price": ("price", "avg_price", "match_price"),
    "commission": ("commission", "fee", "total_fee"),
    "stamp_tax": ("stamp_tax", "tax"),
    "status": ("status", "order_status"),
}


def normalize_symbol(value: object) -> str:
    """把聚宽/本仓库的交易代码统一成 ``600519.SH`` 形式。

    无法识别时原样返回，交由调用方在比对阶段暴露差异。
    """

    text = str(value).strip().upper()
    if "." not in text:
        return text
    code, suffix = text.rsplit(".", 1)
    return f"{code}.{JQ_EXCHANGE_SUFFIX.get(suffix, suffix)}"


def normalize_side(value: object) -> str | None:
    """把开平/买卖方向统一成 ``buy`` / ``sell``。"""

    text = str(value).strip().lower()
    if text in {"buy", "open", "long", "b"}:
        return "buy"
    if text in {"sell", "close", "short", "s"}:
        return "sell"
    return None


def normalize_dates(series: pd.Series) -> pd.Series:
    """把任意日期/时间列归一成 ``datetime64[ns]``（只保留日期）。"""

    parsed = pd.to_datetime(series, errors="coerce")
    return parsed.dt.normalize()


def pick_column(frame: pd.DataFrame, canonical: str) -> str | None:
    """按别名表找出实际列名。"""

    for candidate in COLUMN_ALIASES.get(canonical, (canonical,)):
        if candidate in frame.columns:
            return candidate
    return None


@dataclass(frozen=True)
class Tolerances:
    """比对容差。数值型比较统一用绝对差，避免分母为 0。"""

    quantity: int = 0
    price_abs: float = 0.005
    cash_abs: float = 0.01
    equity_rel: float = 1e-6
    return_abs: float = 1e-3
    metric_rel: float = 1e-3
    #: 收益/回撤/波动等比率型指标的绝对下限；与 metric_rel 同时超出才判失败。
    metric_abs: float = 1e-3
    #: Sharpe 等量纲较大的指标单独给绝对下限，0.01 已远小于噪声。
    sharpe_abs: float = 0.01

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "Tolerances":
        if not raw:
            return cls()
        known = {field.name for field in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"未知的容差项: {sorted(unknown)}")
        return cls(**{key: float(value) for key, value in raw.items()})

    def as_dict(self) -> dict[str, float]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True)
class CaseSpec:
    """一个对账用例的声明。"""

    case_id: str
    description: str
    path: Any  # pathlib.Path，保持本模块不依赖 pathlib 具体类型
    jq: Mapping[str, Any]
    svquant: Mapping[str, Any]
    comparison: Mapping[str, Any]
    tolerances: Tolerances

    @property
    def price_basis(self) -> str:
        return str(self.comparison.get("price_basis", "hfq")).lower()

    @property
    def layers(self) -> set[str]:
        """本轮启用的比对层；只有收益曲线的金标只声明 L0/L4/L5。"""

        raw = self.comparison.get("layers")
        if raw is None:
            return {"L0", "L1", "L2", "L3", "L4", "L5"}
        selected = {str(item).upper() for item in raw}
        unknown = selected - {"L0", "L1", "L2", "L3", "L4", "L5"}
        if unknown:
            raise ValueError(f"未知的比对层: {sorted(unknown)}")
        return selected

    @property
    def strict_layers(self) -> set[str]:
        raw = self.comparison.get("strict_layers")
        if raw is None:
            return set(self.layers)
        # 只声明了部分层时，strict 也只能落在启用的层上。
        return {str(item).upper() for item in raw} & self.layers

    def strategy_path(self, side: str, default_name: str):
        """``side`` 为 ``jq`` 或 ``svquant``；相对路径按用例目录解析。"""

        from pathlib import Path

        if side not in {"jq", "svquant"}:
            raise ValueError(f"未知的策略侧: {side!r}")
        config = self.jq if side == "jq" else self.svquant
        candidate = Path(str(config.get("strategy_source") or default_name))
        return candidate if candidate.is_absolute() else Path(self.path) / candidate
