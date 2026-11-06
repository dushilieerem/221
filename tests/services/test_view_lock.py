"""``views/current.lock`` 解析与状态接口回显的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.api.routes.data import _release_status
from app.services.data.view_lock import (
    active_view_lock,
    read_view_lock,
    view_lock_path,
)


def _layout(tmp_path: Path) -> Path:
    """构造 ``<tmp>/views/<view>/materialized/ifind`` 并返回行情目录。"""

    data_dir = tmp_path / "views" / "rv-1" / "materialized" / "ifind"
    data_dir.mkdir(parents=True)
    return data_dir


def _write_lock(tmp_path: Path, **overrides: object) -> Path:
    payload = {
        "view_id": "research-rv-20261106-2",
        "materialized": str(tmp_path / "views" / "rv-1" / "materialized"),
        "manifest_sha256": "abc123",
        "releases": {"ifind": "ifind-sync-x", "derived": "derived-x"},
        "switched_at": "2026-11-06T20:00:00+08:00",
    }
    payload.update(overrides)
    path = tmp_path / "views" / "current.lock"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _Provider:
    data_version = "ifind-sync-x"


class _Engine:
    release_id = "release-signature"

    def __init__(self, data_dir: Path | None) -> None:
        self.data_dir = data_dir
        self.provider = _Provider()


def test_view_lock_path_walks_up_to_views_dir(tmp_path):
    data_dir = _layout(tmp_path)

    assert view_lock_path(data_dir) == tmp_path / "views" / "current.lock"


def test_view_lock_path_returns_none_outside_views_layout(tmp_path):
    data_dir = tmp_path / "marketdata"
    data_dir.mkdir()

    assert view_lock_path(data_dir) is None


def test_read_view_lock_parses_fields(tmp_path):
    data_dir = _layout(tmp_path)
    _write_lock(tmp_path)

    lock = read_view_lock(data_dir)

    assert lock is not None
    assert lock.view_id == "research-rv-20261106-2"
    assert lock.manifest_sha256 == "abc123"
    assert lock.releases == {"ifind": "ifind-sync-x", "derived": "derived-x"}
    assert lock.switched_at == "2026-11-06T20:00:00+08:00"


def test_read_view_lock_tolerates_missing_or_invalid_file(tmp_path):
    data_dir = _layout(tmp_path)
    assert read_view_lock(data_dir) is None

    (tmp_path / "views" / "current.lock").write_text("not json", encoding="utf-8")
    assert read_view_lock(data_dir) is None

    _write_lock(tmp_path, view_id="")
    assert read_view_lock(data_dir) is None


def test_active_view_lock_ignores_stale_lock(tmp_path):
    """切换 current 后、服务重载前，锁指向新视图时不得回显。"""

    data_dir = _layout(tmp_path)
    _write_lock(tmp_path, materialized=str(tmp_path / "views" / "rv-2" / "materialized"))

    assert read_view_lock(data_dir) is not None
    assert active_view_lock(data_dir) is None


def test_release_status_includes_view_lock_only_when_matching(tmp_path):
    data_dir = _layout(tmp_path)
    _write_lock(tmp_path)

    status = _release_status(_Engine(data_dir))

    assert status.view_id == "research-rv-20261106-2"
    assert status.manifest_sha256 == "abc123"

    _write_lock(tmp_path, materialized=str(tmp_path / "views" / "rv-2" / "materialized"))
    stale = _release_status(_Engine(data_dir))
    assert stale.view_id is None
    assert stale.manifest_sha256 is None


def test_release_status_omits_lock_without_data_dir(tmp_path):
    status = _release_status(_Engine(None))

    assert status.view_id is None
    assert status.manifest_sha256 is None
