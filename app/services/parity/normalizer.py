"""把 SVQuant 策略回测响应规范化为与金标一致的内部结构。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.parity.loader import ParityError
from app.services.parity.schema import normalize_dates, normalize_side, normalize_symbol

_EMPTY_FILLS = ["date", "symbol", "side", "quantity", "price", "commission", "stamp_tax"]
_EMPTY_POSITIONS = ["date", "symbol", "quantity", "closeable"]
_EMPTY_ORDERS = ["date", "symbol", "side", "status", "reason"]


@dataclass
class CanonicalRun:
    """两侧共用的规范化结果。"""

    daily: pd.DataFrame
    positions: pd.DataFrame
    fills: pd.DataFrame
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    meta: dict[str, Any] = field(default_factory=dict)


def load_response(path: str | Path) -> dict[str, Any]:
    """读取 ``run`` 子命令落盘的响应 JSON。"""

    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ParityError(f"{path}: 响应 JSON 必须是对象")
    if payload.get("error"):
        raise ParityError(f"SVQuant 回测返回错误: {payload['error']}")
    return payload


def from_response(payload: dict[str, Any]) -> CanonicalRun:
    """``StrategyBacktestResponse.model_dump(mode='json')`` -> CanonicalRun。"""

    if payload.get("error"):
        raise ParityError(f"SVQuant 回测返回错误: {payload['error']}")

    equity_curve = payload.get("equity_curve") or []
    snapshots = payload.get("event_positions") or []
    if not equity_curve and not snapshots:
        raise ParityError("SVQuant 响应缺少 equity_curve 与 event_positions")

    daily = _daily_frame(equity_curve, snapshots)
    positions = _positions_frame(snapshots)
    fills = _fills_frame(payload.get("fills") or [])
    orders = _orders_frame(payload.get("orders") or [])
    meta = {
        "strategy": (payload.get("meta") or {}).get("strategy"),
        "engine_mode": (payload.get("meta") or {}).get("engine_mode"),
        "execution_timing": (payload.get("meta") or {}).get("execution_timing"),
        "metrics": payload.get("metrics") or {},
        "assumptions": payload.get("assumptions") or {},
    }
    return CanonicalRun(daily=daily, positions=positions, fills=fills, orders=orders, meta=meta)


def _daily_frame(equity_curve: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> pd.DataFrame:
    if snapshots:
        frame = pd.DataFrame({
            "date": normalize_dates(pd.Series([item.get("timestamp") for item in snapshots])),
            "equity": pd.to_numeric(
                pd.Series([item.get("total_value") for item in snapshots]), errors="coerce"
            ),
            "cash": pd.to_numeric(
                pd.Series([item.get("cash") for item in snapshots]), errors="coerce"
            ),
        })
    else:
        frame = pd.DataFrame({
            "date": normalize_dates(pd.Series([item.get("date") for item in equity_curve])),
            "equity": pd.to_numeric(
                pd.Series([item.get("value") for item in equity_curve]), errors="coerce"
            ),
            "cash": float("nan"),
        })
    return frame.sort_values("date").reset_index(drop=True)


def _positions_frame(snapshots: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in snapshots:
        date = pd.Timestamp(item.get("timestamp")).normalize()
        closeable = item.get("sellable_quantities") or {}
        for symbol, quantity in (item.get("positions") or {}).items():
            if int(quantity) == 0:
                continue
            rows.append({
                "date": date,
                "symbol": normalize_symbol(symbol),
                "quantity": int(quantity),
                "closeable": int(closeable.get(symbol, 0)),
            })
    return pd.DataFrame(rows, columns=_EMPTY_POSITIONS) if rows else pd.DataFrame(columns=_EMPTY_POSITIONS)


def _fills_frame(fills: list[dict[str, Any]]) -> pd.DataFrame:
    if not fills:
        return pd.DataFrame(columns=_EMPTY_FILLS)
    frame = pd.DataFrame({
        "date": normalize_dates(pd.Series([item.get("timestamp") for item in fills])),
        "symbol": [normalize_symbol(item.get("symbol")) for item in fills],
        "side": [normalize_side(item.get("side")) for item in fills],
        "quantity": pd.to_numeric(pd.Series([item.get("quantity") for item in fills]), errors="coerce").fillna(0).astype("int64"),
        "price": pd.to_numeric(pd.Series([item.get("price") for item in fills]), errors="coerce"),
        "commission": pd.to_numeric(pd.Series([item.get("commission") for item in fills]), errors="coerce").fillna(0.0),
        "stamp_tax": pd.to_numeric(pd.Series([item.get("stamp_tax") for item in fills]), errors="coerce").fillna(0.0),
    })
    return frame[frame["quantity"] > 0].reset_index(drop=True)


def _orders_frame(orders: list[dict[str, Any]]) -> pd.DataFrame:
    if not orders:
        return pd.DataFrame(columns=_EMPTY_ORDERS)
    frame = pd.DataFrame({
        "date": normalize_dates(pd.Series([item.get("submitted_at") for item in orders])),
        "symbol": [normalize_symbol(item.get("symbol")) for item in orders],
        "side": [normalize_side(item.get("side")) for item in orders],
        "status": [item.get("status") for item in orders],
        "reason": [item.get("reason_code") for item in orders],
    })
    return frame.reset_index(drop=True)
