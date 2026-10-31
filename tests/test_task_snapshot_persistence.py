# -*- coding: utf-8 -*-
"""P0-03：批量任务终态快照跨进程可查。"""
from __future__ import annotations

import time

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
