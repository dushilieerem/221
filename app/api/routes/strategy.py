"""策略回测路由。"""

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.dependencies import get_experiment_manager
from app.schemas.backtest import StrategyBacktestRequest, StrategyBacktestResponse
from app.services.experiment.manager import ExperimentManagerClosedError, ExperimentQueueFullError
from app.services.factor.runner import run_strategy_backtest_async, submit_strategy_backtest
from app.services.strategy.compat.capabilities import capability_manifest


router = APIRouter()

#: 异步策略任务结束后保留结果的秒数；与同步接口的 60 秒不同。
ASYNC_RETENTION_SECONDS = 86400.0
_ASYNC_FIELDS = (
    "run_id",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "cancel_requested",
    "error",
    "error_code",
    "unsupported_api",
    "result",
)


@router.post("/backtest/strategy", response_model=StrategyBacktestResponse)
async def backtest_strategy(
    req: StrategyBacktestRequest,
    manager=Depends(get_experiment_manager),
) -> StrategyBacktestResponse:
    try:
        return await run_strategy_backtest_async(req, manager=manager)
    except ExperimentQueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "1"}) from exc
    except ExperimentManagerClosedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/strategy-capabilities")
def strategy_capabilities() -> dict:
    """返回聚宽兼容层支持的能力清单，供 WebApp 预检与缓存。"""

    data: dict[str, object] = {}
    try:
        from app.services.data.engine import DataEngine

        engine = DataEngine.get_instance()
        data = {
            "release_id": engine.release_id,
            "data_version": getattr(engine.provider, "data_version", None),
            "data_dir": str(engine.data_dir) if engine.data_dir else None,
        }
    except Exception:  # noqa: BLE001 - 未启动数据引擎时仍返回静态清单
        pass
    return capability_manifest(data=data)


def _async_snapshot(handle, *, include_result: bool = True) -> dict:
    """任务快照；业务错误同样投影到顶层，避免「succeeded + 空结果」误判。"""

    snapshot = handle.snapshot(include_result=include_result)
    payload = {field: snapshot.get(field) for field in _ASYNC_FIELDS}
    result = payload.get("result") if include_result else None
    if result is None:
        return payload
    business_error = (
        result.get("error") if isinstance(result, dict) else getattr(result, "error", None)
    )
    if not business_error:
        return payload
    payload["error"] = payload.get("error") or business_error
    payload["error_code"] = (
        result.get("error_code")
        if isinstance(result, dict)
        else getattr(result, "error_code", None)
    )
    payload["unsupported_api"] = (
        result.get("unsupported_api")
        if isinstance(result, dict)
        else getattr(result, "unsupported_api", None)
    )
    if payload["status"] == "succeeded":
        # 计算已完成但业务失败（策略代码/数据/不支持的 API），按失败呈现。
        payload["status"] = "failed"
    return payload


def _async_handle(manager, run_id: str):
    """取策略任务句柄；区分「已过期」与「不存在/已随重启丢失」。"""

    handle = manager.get(run_id)
    if handle is not None:
        try:
            metadata = handle.metadata
        except KeyError:  # 并发回收：取到句柄后恰好被保留期清掉
            metadata = None
        if metadata is not None and metadata.get("type") == "strategy":
            return handle
    if manager.was_pruned(run_id):
        raise HTTPException(
            status_code=404,
            detail={
                "status": "expired",
                "message": "策略任务结果已过期（异步结果保留 24 小时）",
            },
        )
    raise HTTPException(
        status_code=404,
        detail={
            "status": "unknown",
            "message": "策略任务不存在：run_id 无效或服务已重启",
        },
    )


@router.post("/backtest/strategy/async", status_code=202)
async def submit_strategy_async(
    req: StrategyBacktestRequest,
    response: Response,
    manager=Depends(get_experiment_manager),
) -> dict:
    """异步提交策略回测，立即返回 run_id；结果保留 24 小时。"""

    try:
        handle = submit_strategy_backtest(
            req, manager, retention_seconds=ASYNC_RETENTION_SECONDS
        )
    except ExperimentQueueFullError as exc:
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": "1"}
        ) from exc
    except ExperimentManagerClosedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    location = f"/api/backtest/strategy/async/{handle.run_id}"
    response.headers["Location"] = location
    return {"run_id": handle.run_id, "status": handle.status, "poll_url": location}


@router.get("/backtest/strategy/async/{run_id}")
def get_strategy_async(run_id: str, manager=Depends(get_experiment_manager)) -> dict:
    """查询异步策略回测的状态与结果。"""

    return _async_snapshot(_async_handle(manager, run_id))


@router.delete("/backtest/strategy/async/{run_id}")
def cancel_strategy_async(run_id: str, manager=Depends(get_experiment_manager)) -> dict:
    """请求取消异步策略回测；已完成的取消只标记，不改变结果。"""

    handle = _async_handle(manager, run_id)
    handle.cancel()
    return _async_snapshot(handle, include_result=False)


__all__ = ["router"]
