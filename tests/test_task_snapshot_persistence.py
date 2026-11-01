# -*- coding: utf-8 -*-
"""P0-03：批量任务终态快照跨进程可查。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

from app.services.factor_backtest_batch.coordinator import FactorBacktestBatchCoordinator
from app.models.factor_batch import FactorBacktestBatchProfile
from app.services.factor_backtest_batch.task_registry import FactorBacktestBatchTaskRegistry


def test_terminal_snapshot_survives_new_registry(tmp_path) -> None:
    db = tmp_path / "tasks.db"
    snap = {
        "task_id": "t1",
        "status": "succeeded",
        "created_at": time.time() - 1,
        "started_at": time.time() - 1,
        "finished_at": time.time(),
        "batch_id": "b1",
        "batch_url": "/api/factor-backtest-batches/b1",
        "error": None,
        "result": {"id": "b1"},
    }
    first = FactorBacktestBatchTaskRegistry(snapshot_db=db)
    first._persist_snapshot(snap)
    first.shutdown(wait=False)

    second = FactorBacktestBatchTaskRegistry(snapshot_db=db)
    try:
        loaded = second.snapshot("t1")
        assert loaded is not None
        assert loaded["status"] == "succeeded"
        assert loaded["batch_id"] == "b1"
    finally:
        second.shutdown(wait=False)


def test_running_snapshot_is_reported_as_interrupted(tmp_path) -> None:
    db = tmp_path / "tasks.db"
    snap = {
        "task_id": "t2",
        "status": "running",
        "created_at": time.time() - 2,
        "started_at": time.time() - 1,
        "finished_at": time.time() - 1,
        "batch_id": None,
        "batch_url": None,
        "error": None,
        "result": None,
        "progress_done": 1,
        "progress_total": 5,
    }
    first = FactorBacktestBatchTaskRegistry(snapshot_db=db)
    first._persist_snapshot(snap)
    first.shutdown(wait=False)

    second = FactorBacktestBatchTaskRegistry(snapshot_db=db)
    try:
        loaded = second.snapshot("t2")
        assert loaded is not None
        assert loaded["status"] == "failed"
        assert loaded["interrupted"] is True
        assert "重启" in loaded["error"]
        # 二次读取仍然是中断态（转换已落盘）
        assert second.snapshot("t2")["status"] == "failed"
    finally:
        second.shutdown(wait=False)


def test_running_and_progress_are_persisted(tmp_path) -> None:
    db = tmp_path / "tasks.db"
    started = threading.Event()
    release = threading.Event()

    def run_sync(profile, *, force_retry=False, dry_run=False, progress_callback=None):
        if progress_callback is not None:
            progress_callback(1, 4)
        started.set()
        release.wait(timeout=5)
        return {"id": "b9", "status": "completed"}

    coordinator = FactorBacktestBatchCoordinator(SimpleNamespace())
    coordinator.run_sync = run_sync  # type: ignore[method-assign]
    registry = FactorBacktestBatchTaskRegistry(snapshot_db=db)
    try:
        task = registry.submit(coordinator, FactorBacktestBatchProfile("2026-08-19"))
        assert started.wait(timeout=2)
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT payload FROM async_task_snapshots WHERE task_id = ?",
                (task["task_id"],),
            ).fetchone()
        payload = json.loads(row[0])
        assert payload["status"] == "running"
        assert payload["progress_done"] == 1
        assert payload["progress_total"] == 4

        release.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            snap = registry.get(task["task_id"])
            if snap and snap["status"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("任务没有完成")
    finally:
        release.set()
        registry.shutdown()
