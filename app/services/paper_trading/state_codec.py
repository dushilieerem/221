"""事件策略 ``context.state`` 的跨日 JSON 编解码。

``context.state`` 是 ``SimpleNamespace``，内置策略会在里面放
``dict[str, float]``、``dict[str, datetime.date]``、tuple、numpy 标量等
值。普通 JSON 往返会把 date 变成字符串、tuple 变成 list，导致下一日策略
里的日期相减或元组比较直接抛异常。这里用带类型标签的 JSON 保留这些类型，
并拒绝无法安全表达的任意对象。

标签键使用 ``__pt_type__``，普通 dict 也统一编码为带标签结构，避免策略
自己的键与标签冲突。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

STATE_SIZE_LIMIT = 1024 * 1024

_TAG = "__pt_type__"
_MISSING = object()


class StateCodecError(ValueError):
    """策略状态无法编码/解码。"""


def code_hash(strategy_code: str) -> str:
    """计算策略源码哈希，统一带 ``sha256:`` 前缀。"""

    return "sha256:" + hashlib.sha256(strategy_code.encode("utf-8")).hexdigest()


def normalize_code_hash(value: str | None) -> str | None:
    """把调用方传入的哈希统一成 ``sha256:<hex>``；空值返回 None。"""

    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    return "sha256:" + text


def normalize_state_hash(value: str | None) -> str | None:
    return normalize_code_hash(value)


def _encode_key(value: Any) -> Any:
    if isinstance(value, str):
        return value
    return {"__key__": _encode(value)}


def _decode_key(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__key__"}:
        return _decode(value["__key__"])
    return value


def _encode(value: Any) -> Any:
    # numpy 标量先转 Python，避免 json 无法序列化
    if isinstance(value, np.generic):
        if isinstance(value, np.datetime64):
            return _encode(pd.Timestamp(value).to_pydatetime())
        return _encode(value.item())
    if isinstance(value, np.ndarray):
        return _encode(value.tolist())
    if isinstance(value, SimpleNamespace):
        return {
            _TAG: "namespace",
            "value": {key: _encode(item) for key, item in vars(value).items()},
        }
    if isinstance(value, dict):
        return {
            _TAG: "dict",
            "items": [[_encode_key(key), _encode(item)] for key, item in value.items()],
        }
    if isinstance(value, tuple):
        return {_TAG: "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, set):
        return {_TAG: "set", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return {_TAG: "datetime", "value": pd.Timestamp(value).isoformat()}
    if isinstance(value, date):
        return {_TAG: "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {_TAG: "time", "value": value.isoformat()}
    if isinstance(value, float):
        if math.isnan(value):
            return {_TAG: "float", "value": "nan"}
        if math.isinf(value):
            return {_TAG: "float", "value": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise StateCodecError(
        f"策略状态包含不支持的类型: {type(value).__name__}；"
        "只支持字典、列表、元组、日期、数字、字符串和 None"
    )


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict) or _TAG not in value:
        if isinstance(value, dict):
            return {key: _decode(item) for key, item in value.items()}
        return value
    kind = value[_TAG]
    if kind == "namespace":
        return SimpleNamespace(**{key: _decode(item) for key, item in value["value"].items()})
    if kind == "dict":
        return {_decode_key(key): _decode(item) for key, item in value["items"]}
    if kind == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if kind == "set":
        return {_decode(item) for item in value["items"]}
    if kind == "datetime":
        return pd.Timestamp(value["value"]).to_pydatetime()
    if kind == "date":
        return date.fromisoformat(value["value"])
    if kind == "time":
        return time.fromisoformat(value["value"])
    if kind == "float":
        return float(value["value"])
    raise StateCodecError(f"未知的策略状态类型标签: {kind!r}")


def encode_state(state: SimpleNamespace | None) -> dict[str, Any]:
    """把 ``context.state`` 编码为可写入 JSONB 的对象。"""

    if state is None:
        return {}
    if not isinstance(state, SimpleNamespace):
        raise StateCodecError("strategy_state 顶层必须是 SimpleNamespace")
    payload = {key: _encode(value) for key, value in vars(state).items()}
    canonical = canonical_state_json(payload)
    if len(canonical.encode("utf-8")) > STATE_SIZE_LIMIT:
        raise StateCodecError("strategy_state 超过 1 MiB 限制")
    return payload


def decode_state(payload: dict[str, Any] | None) -> SimpleNamespace:
    """把持久化状态解码为 ``SimpleNamespace``；空值返回空对象。"""

    if not payload:
        return SimpleNamespace()
    if not isinstance(payload, dict):
        raise StateCodecError("strategy_state 必须是 JSON object")
    decoded = {key: _decode(value) for key, value in payload.items()}
    return SimpleNamespace(**decoded)


def merge_state(target: SimpleNamespace, payload: dict[str, Any] | None) -> None:
    """把持久化状态合并进 initialize 创建的状态对象。

    ``initialize`` 会重建默认值（例如 ``peak_close = {}``），因此持久化值
    必须在 initialize 之后覆盖同名属性；嵌套 ``SimpleNamespace`` 递归合并，
    其余值整体替换。
    """

    if not payload:
        return
    persisted = decode_state(payload)
    _merge_namespace(target, persisted)


def _merge_namespace(target: SimpleNamespace, source: SimpleNamespace) -> None:
    for key, value in vars(source).items():
        current = getattr(target, key, _MISSING)
        if isinstance(current, SimpleNamespace) and isinstance(value, SimpleNamespace):
            _merge_namespace(current, value)
        else:
            setattr(target, key, value)


def canonical_state_json(payload: dict[str, Any]) -> str:
    """稳定序列化状态，用于哈希与大小校验。"""

    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StateCodecError(f"strategy_state 无法序列化: {exc}") from exc


def state_hash(payload: dict[str, Any] | None) -> str:
    """对编码后的状态计算稳定哈希。"""

    canonical = canonical_state_json(payload or {})
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def export_state(state: SimpleNamespace | None) -> tuple[dict[str, Any], str | None]:
    """编码状态并返回 ``(payload, state_hash)``；空状态哈希为 None。"""

    payload = encode_state(state)
    return payload, (state_hash(payload) if payload else None)


__all__ = [
    "STATE_SIZE_LIMIT",
    "StateCodecError",
    "canonical_state_json",
    "code_hash",
    "decode_state",
    "encode_state",
    "export_state",
    "merge_state",
    "normalize_code_hash",
    "normalize_state_hash",
    "state_hash",
]
