"""策略回测异步任务 API 契约测试（202 + run_id + poll）。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.backtest import StrategyBacktestResponse
from app.services.experiment import strategy as strategy_experiment
from app.services.experiment.manager import ExperimentManager
from app.services.data.engine import DataEngine


PAYLOAD = {
    "strategy_code": "def generate_signals(self):\n    return self.close",
    "start": "2022-01-04",
    "end": "2022-03-31",
}


@pytest.fixture(autouse=True)
def _restore_global_data_engine():
    previous = DataEngine._instance
    yield
    DataEngine._instance = previous


class _TestDataEngine(SimpleNamespace):
    """API 任务测试只验证调度，不在 lifespan 读取真实 MarketData。"""

    def __init__(self):
        super().__init__(provider=None, release_id="test-release", data_dir=None)

    def load_parquet(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


def _use_app_data_engine(application, *, max_workers: int = 2):
    application.state.experiment_manager.shutdown()
    data_engine = _TestDataEngine()
    application.state.data_engine = data_engine
    DataEngine.configure_instance(data_engine)
    manager = ExperimentManager(max_workers=max_workers, max_queue_size=8)
    application.state.experiment_manager = manager
    return manager


def _poll(client: TestClient, poll_url: str, *, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(poll_url)
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"任务轮询超时: {poll_url}")


def test_submit_returns_202_and_poll_reaches_result(monkeypatch):
    def fake_backtest(_request, **_kwargs):
        return StrategyBacktestResponse(metrics={"annual_return": 0.21})

    monkeypatch.setattr(strategy_experiment, "run_strategy_backtest", fake_backtest)
    application = create_app(testing=True)
    _use_app_data_engine(application)
    try:
        with TestClient(application) as client:
            response = client.post("/api/backtest/strategy/async", json=PAYLOAD)

            assert response.status_code == 202
            body = response.json()
            assert body["status"] in {"queued", "running"}
            assert body["poll_url"] == f"/api/backtest/strategy/async/{body['run_id']}"
            assert response.headers["Location"] == body["poll_url"]

            result = _poll(client, body["poll_url"])
            assert result["status"] == "succeeded"
            assert result["result"]["metrics"]["annual_return"] == 0.21
    finally:
        application.state.experiment_manager.shutdown()


def test_async_task_keeps_result_for_one_day(monkeypatch):
    """异步任务结果保留 24 小时，不能沿用同步接口的 60 秒。"""

    monkeypatch.setattr(
        strategy_experiment,
        "run_strategy_backtest",
        lambda _request, **_kwargs: StrategyBacktestResponse(),
    )
    application = create_app(testing=True)
    manager = _use_app_data_engine(application)
    try:
        with TestClient(application) as client:
            body = client.post("/api/backtest/strategy/async", json=PAYLOAD).json()
            metadata = manager.snapshot(body["run_id"])["metadata"]
            assert metadata["type"] == "strategy"
            assert metadata["retention_seconds"] == 86400.0
    finally:
        manager.shutdown()


def test_unknown_run_id_returns_404():
    application = create_app(testing=True)
    manager = _use_app_data_engine(application)
    try:
        with TestClient(application) as client:
            response = client.get("/api/backtest/strategy/async/not-a-run")
            assert response.status_code == 404
            detail = response.json()["detail"]
            assert detail["status"] == "unknown"
            assert "服务已重启" in detail["message"]
    finally:
        manager.shutdown()


def test_expired_run_reports_expired_status(monkeypatch):
    """保留期回收后返回 status=expired，与从未存在的 run_id 区分。"""

    monkeypatch.setattr(
        strategy_experiment,
        "run_strategy_backtest",
        lambda _request, **_kwargs: StrategyBacktestResponse(),
    )
    application = create_app(testing=True)
    manager = _use_app_data_engine(application)
    try:
        with TestClient(application) as client:
            body = client.post("/api/backtest/strategy/async", json=PAYLOAD).json()
            assert _poll(client, body["poll_url"])["status"] == "succeeded"

            # 把时钟推到保留期之后，触发一次回收。
            manager._prune_finished_locked(now=time.time() + 86_401)

            response = client.get(body["poll_url"])
            assert response.status_code == 404
            detail = response.json()["detail"]
            assert detail["status"] == "expired"
            assert "24 小时" in detail["message"]
    finally:
        manager.shutdown()


def test_cancel_marks_queued_task_cancelled(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_backtest(_request, **_kwargs):
        started.set()
        release.wait(timeout=3)
        return StrategyBacktestResponse()

    monkeypatch.setattr(
        strategy_experiment, "run_strategy_backtest", blocked_backtest
    )
    application = create_app(testing=True)
    # 单 Worker + 先占一个任务，保证第二个提交停在队列里，可被取消。
    manager = _use_app_data_engine(application, max_workers=1)
    try:
        with TestClient(application) as client:
            first = client.post("/api/backtest/strategy/async", json=PAYLOAD).json()
            assert started.wait(timeout=1)
            second = client.post(
                "/api/backtest/strategy/async",
                json={**PAYLOAD, "end": "2022-04-29"},
            ).json()

            response = client.delete(second["poll_url"])
            assert response.status_code == 200
            assert response.json()["status"] == "cancelled"

            release.set()
            assert _poll(client, first["poll_url"])["status"] == "succeeded"
    finally:
        release.set()
        manager.shutdown()


def test_capabilities_endpoint_describes_joinquant_dialect():
    application = create_app(testing=True)
    manager = _use_app_data_engine(application)
    try:
        with TestClient(application) as client:
            response = client.get("/api/strategy-capabilities")

            assert response.status_code == 200
            body = response.json()
            assert body["dialects"]["native"]["engine_modes"] == [
                "vectorized",
                "event",
            ]
            joinquant = body["dialects"]["joinquant"]
            assert joinquant["engine_modes"] == ["event"]
            assert joinquant["timed_execution"] is True
            assert "get_price" in joinquant["supported_apis"]
            unsupported = {item["api"] for item in joinquant["unsupported_apis"]}
            assert "get_call_auction" in unsupported
    finally:
        manager.shutdown()


def test_business_error_is_exposed_on_task_snapshot(monkeypatch):
    """策略业务失败不能显示为 succeeded：顶层 error/error_code 与状态一起给出。"""

    def failed_backtest(_request, **_kwargs):
        return StrategyBacktestResponse(
            error="NotImplementedError: 兼容层不支持 get_call_auction",
            error_code="unsupported_api",
            unsupported_api="兼容层不支持 get_call_auction",
        )

    monkeypatch.setattr(strategy_experiment, "run_strategy_backtest", failed_backtest)
    application = create_app(testing=True)
    manager = _use_app_data_engine(application)
    try:
        with TestClient(application) as client:
            body = client.post("/api/backtest/strategy/async", json=PAYLOAD).json()
            snapshot = _poll(client, body["poll_url"])
            assert snapshot["status"] == "failed"
            assert "get_call_auction" in snapshot["error"]
            assert snapshot["error_code"] == "unsupported_api"
            assert "get_call_auction" in snapshot["unsupported_api"]
            assert snapshot["result"]["error_code"] == "unsupported_api"
    finally:
        manager.shutdown()
