"""当前数据视图锁 ``views/current.lock`` 的只读解析。

``svquant-data`` 在执行 ``switch-current`` 时写入该文件，记录当前视图的
``view_id``、物理目录、清单签名和各层 release。服务只读取，不写入；文件
缺失或格式不合法时返回 ``None``，状态接口因此保持向后兼容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ViewLock:
    """``views/current.lock`` 的内容；只保留状态接口需要的字段。"""

    view_id: str
    materialized: str
    manifest_sha256: str | None = None
    releases: dict[str, str] | None = None
    switched_at: str | None = None


def view_lock_path(data_dir: str | Path) -> Path | None:
    """按行情目录推导锁文件：``<views>/<view>/materialized/<layer>`` -> ``<views>/current.lock``。

    识别 ``views`` / ``views-root`` 祖先目录；旧布局（如 ``../marketdata``）
    没有锁文件，返回 ``None`` 交给调用方降级。
    """

    for candidate in Path(data_dir).resolve().parents:
        # 生产布局把物化文件放在 ``views-root`` 下，``views`` 只是软链别名；
        # 两种目录名都接受，避免状态接口在切换后丢失 view_id。
        if candidate.name in {"views", "views-root"}:
            return candidate / "current.lock"
    return None


def read_view_lock(data_dir: str | Path) -> ViewLock | None:
    """读取锁文件；缺失、不可读或缺少 ``view_id``/``materialized`` 时返回 ``None``。"""

    path = view_lock_path(data_dir)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    view_id = _text(payload.get("view_id"))
    materialized = _text(payload.get("materialized"))
    if view_id is None or materialized is None:
        return None
    releases: dict[str, str] = {}
    raw_releases = payload.get("releases")
    if isinstance(raw_releases, dict):
        for kind, release in raw_releases.items():
            kind = _text(kind)
            release = _text(release)
            if kind is not None and release is not None:
                releases[kind] = release
    return ViewLock(
        view_id=view_id,
        materialized=materialized,
        manifest_sha256=_text(payload.get("manifest_sha256")),
        releases=releases or None,
        switched_at=_text(payload.get("switched_at")),
    )


def active_view_lock(data_dir: str | Path) -> ViewLock | None:
    """只在锁指向的视图就是已装载目录时返回锁内容。

    切换 ``current`` 后，服务要等下一轮重载才会换目录；这段时间锁已经是新
    视图，直接回显会和新旧目录串台，所以这里做一次目录一致性核对。
    """

    lock = read_view_lock(data_dir)
    if lock is None:
        return None
    resolved = Path(data_dir).resolve()
    materialized = Path(lock.materialized).resolve()
    if resolved != materialized and resolved.parent != materialized:
        return None
    return lock


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


__all__ = ["ViewLock", "active_view_lock", "read_view_lock", "view_lock_path"]
