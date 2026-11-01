"""读取对账用例与聚宽金标文件，并做最小限度的 schema 校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from app.services.parity.schema import (
    GOLDEN_SCHEMA_VERSION,
    CaseSpec,
    Tolerances,
    normalize_dates,
    normalize_side,
    normalize_symbol,
    pick_column,
)


class ParityError(ValueError):
    """对账输入或配置错误；CLI 将其映射为退出码 2。"""


@dataclass
class GoldenRun:
    """聚宽侧规范化后的结果。"""

    daily: pd.DataFrame  # date, equity, cash, returns
    positions: pd.DataFrame  # date, symbol, quantity, closeable
    fills: pd.DataFrame  # date, symbol, side, quantity, price, commission, stamp_tax
    meta: dict[str, Any] = field(default_factory=dict)


def load_case(case_dir: str | Path) -> CaseSpec:
    """读取 ``<case_dir>/case.yaml``。"""

    path = Path(case_dir)
    case_file = path / "case.yaml"
    if not case_file.is_file():
        raise ParityError(f"用例配置不存在: {case_file}")

    with case_file.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ParityError(f"用例配置必须是映射: {case_file}")

    version = raw.get("schema_version", GOLDEN_SCHEMA_VERSION)
    if version != GOLDEN_SCHEMA_VERSION:
        raise ParityError(
            f"{case_file}: 不支持的 schema_version={version!r}，"
            f"当前为 {GOLDEN_SCHEMA_VERSION}"
        )

    for key in ("jq", "svquant"):
        if not isinstance(raw.get(key), dict):
            raise ParityError(f"{case_file}: 缺少 {key} 配置")
    if not raw["jq"].get("backtest_id"):
        raise ParityError(f"{case_file}: jq.backtest_id 未填写")

    case_id = str(raw.get("name") or path.name)
    return CaseSpec(
        case_id=case_id,
        description=str(raw.get("description") or ""),
        path=path,
        jq=raw["jq"],
        svquant=raw["svquant"],
        comparison=raw.get("comparison") or {},
        tolerances=Tolerances.from_mapping(raw.get("tolerances")),
    )


def golden_dir(case: CaseSpec) -> Path:
    """金标目录固定在用例下；该目录整体不入库。"""

    return Path(case.path) / "expected"


def golden_available(case: CaseSpec) -> bool:
    directory = golden_dir(case)
    return (directory / "daily.csv").is_file() and (directory / "positions.csv").is_file()


def load_golden(case: CaseSpec) -> GoldenRun:
    """读取并规范化聚宽导出。

    必需：``daily.csv``（每日收益）、``positions.csv``（每日持仓）、
    ``orders.csv``（成交）。``balances.csv`` 与 ``meta.json`` 可选。
    """

    directory = golden_dir(case)
    if not golden_available(case):
        raise ParityError(f"金标未导出或缺少 daily.csv/positions.csv: {directory}")

    meta_path = directory / "meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        with meta_path.open("r", encoding="utf-8") as handle:
            meta = json.load(handle) or {}
        if not isinstance(meta, dict):
            raise ParityError(f"{meta_path}: meta.json 必须是对象")

    daily = _normalize_daily(_read_csv(directory / "daily.csv"), case, meta)
    positions = _normalize_positions(_read_csv(directory / "positions.csv"))
    fills = _normalize_orders(_read_optional(directory / "orders.csv"))

    balances_path = directory / "balances.csv"
    if balances_path.is_file():
        daily = _attach_balances(daily, _read_csv(balances_path))

    return GoldenRun(daily=daily, positions=positions, fills=fills, meta=meta)


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise ParityError(f"缺少金标文件: {path}")
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ParityError(f"{path}: 无法识别文件编码")


def _read_optional(path: Path) -> pd.DataFrame:
    return _read_csv(path) if path.is_file() else pd.DataFrame()


def _require(frame: pd.DataFrame, path: Path, canonical: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    missing: list[str] = []
    for name in canonical:
        actual = pick_column(frame, name)
        if actual is None:
            missing.append(name)
        else:
            mapping[name] = actual
    if missing:
        raise ParityError(
            f"{path.name}: 缺少列 {missing}；实际列 {list(frame.columns)}"
        )
    return mapping


def _normalize_daily(frame: pd.DataFrame, case: CaseSpec, meta: dict[str, Any]) -> pd.DataFrame:
    path = golden_dir(case) / "daily.csv"
    columns = _require(frame, path, ["date", "returns"])
    result = pd.DataFrame({
        "date": normalize_dates(frame[columns["date"]]),
        "returns": pd.to_numeric(frame[columns["returns"]], errors="coerce").astype(float),
    })
    if result["date"].isna().any():
        raise ParityError(f"{path.name}: 存在无法解析的日期")

    initial = meta.get("initial_cash")
    if initial is None:
        initial = (meta.get("params") or {}).get("initial_cash")
    if initial is None:
        initial = case.jq.get("initial_cash")
    if initial is None:
        raise ParityError("缺少初始资金：meta.json.initial_cash 与 case.yaml jq.initial_cash 均未提供")

    # 聚宽首日收益通常为 0/NaN；权益基准恒为初始资金。
    result = result.sort_values("date").reset_index(drop=True)
    returns = result["returns"].fillna(0.0)
    returns.iloc[0] = 0.0
    result["equity"] = float(initial) * (1.0 + returns).cumprod()
    result["cash"] = float("nan")
    return result


def _attach_balances(daily: pd.DataFrame, balances: pd.DataFrame) -> pd.DataFrame:
    path_columns = _require(balances, Path("balances.csv"), ["date", "equity"])
    normalized = pd.DataFrame({
        "date": normalize_dates(balances[path_columns["date"]]),
        "equity": pd.to_numeric(balances[path_columns["equity"]], errors="coerce"),
    })
    cash_column = pick_column(balances, "cash")
    if cash_column is not None:
        normalized["cash"] = pd.to_numeric(balances[cash_column], errors="coerce")
    else:
        normalized["cash"] = float("nan")
    # daily 里的 equity/cash 只是占位，必须丢弃后再合并，否则会得到
    # cash_x/cash_y 两个列，后续现金比较会被静默跳过。
    base = daily.drop(columns=["equity", "cash"], errors="ignore")
    merged = base.merge(normalized, on="date", how="left")
    # 余额表缺日时回退到按收益推算的权益，避免整段丢失。
    merged["equity"] = merged["equity"].fillna(daily["equity"])
    return merged.sort_values("date").reset_index(drop=True)


def _normalize_positions(frame: pd.DataFrame) -> pd.DataFrame:
    path = Path("positions.csv")
    columns = _require(frame, path, ["date", "symbol", "quantity"])
    result = pd.DataFrame({
        "date": normalize_dates(frame[columns["date"]]),
        "symbol": frame[columns["symbol"]].map(normalize_symbol),
        "quantity": pd.to_numeric(frame[columns["quantity"]], errors="coerce").fillna(0).astype("int64"),
    })
    closeable = pick_column(frame, "closeable")
    result["closeable"] = (
        pd.to_numeric(frame[closeable], errors="coerce").fillna(0).astype("int64")
        if closeable is not None
        else result["quantity"]
    )
    return result[result["quantity"] != 0].reset_index(drop=True)


def _normalize_orders(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["date", "symbol", "side", "quantity", "price", "commission", "stamp_tax"])

    path = Path("orders.csv")
    columns = _require(frame, path, ["date", "symbol", "side", "quantity"])
    price_column = pick_column(frame, "price")
    commission_column = pick_column(frame, "commission")
    stamp_column = pick_column(frame, "stamp_tax")
    result = pd.DataFrame({
        "date": normalize_dates(frame[columns["date"]]),
        "symbol": frame[columns["symbol"]].map(normalize_symbol),
        "side": frame[columns["side"]].map(normalize_side),
        "quantity": pd.to_numeric(frame[columns["quantity"]], errors="coerce").fillna(0).astype("int64"),
        "price": pd.to_numeric(frame[price_column], errors="coerce") if price_column else float("nan"),
        "commission": pd.to_numeric(frame[commission_column], errors="coerce").fillna(0.0) if commission_column else 0.0,
        "stamp_tax": pd.to_numeric(frame[stamp_column], errors="coerce").fillna(0.0) if stamp_column else 0.0,
    })
    result = result[result["quantity"] > 0]
    return result.dropna(subset=["date", "side"]).reset_index(drop=True)
