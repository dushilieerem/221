"""服务间认证的异步因子值查重接口；对照清单由 BFF 按权限提供。"""

from fastapi import APIRouter, Depends, HTTPException, Response
from app.api.dependencies import get_experiment_manager
from app.schemas.factor_novelty import NoveltyRequest
from app.services.experiment.manager import (
    ExperimentQueueFullError,
    ExperimentManagerClosedError,
)
from app.services.novelty.experiment import NoveltyExperiment, digest

router = APIRouter()


def handle_for(manager, run_id):
    handle = manager.get(run_id)
    if handle is None or handle.metadata.get("type") != "factor_novelty":
        raise HTTPException(404, "Novelty task not found")
    return handle


def task(manager, run_id):
    handle = handle_for(manager, run_id)
    snap = handle.snapshot(include_result=True)
    out = {
        k: snap.get(k)
        for k in (
            "run_id",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "cancel_requested",
            "error",
            "result",
        )
    }
    progress = handle.metadata["progress"]
    out.update(
        processed_references=progress["processed_references"],
        total_references=progress["total_references"],
        current_reference=progress.get("current_reference"),
    )
    if out["status"] == "succeeded" and not any(
        r.get("status") == "succeeded"
        for r in (out.get("result") or {}).get("comparisons", [])
    ):
        out.update(
            status="failed",
            error="No valid reference comparisons; see per-reference errors",
        )
    if snap["cancel_requested"] and snap["status"] in (
        "succeeded",
        "failed",
        "cancelled",
    ):
        out.update(status="cancelled", result=None)
    return out


@router.post("/factor/novelty", status_code=202)
def submit(
    req: NoveltyRequest, response: Response, manager=Depends(get_experiment_manager)
):
    progress = {
        "processed_references": 0,
        "total_references": len(req.references),
        "cancelled": False,
    }
    try:
        handle = manager.submit(
            NoveltyExperiment(req, progress),
            key="factor-novelty:" + digest(req.model_dump(mode="json")),
            metadata={"type": "factor_novelty", "progress": progress},
        )
    except ExperimentQueueFullError as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
    except ExperimentManagerClosedError as exc:
        raise HTTPException(503, str(exc)) from exc
    response.headers["Location"] = "/api/factor/novelty/" + handle.run_id
    return {
        "run_id": handle.run_id,
        "status": handle.status,
        "poll_url": response.headers["Location"],
    }


@router.get("/factor/novelty/{run_id}")
def get(run_id: str, manager=Depends(get_experiment_manager)):
    return task(manager, run_id)


@router.delete("/factor/novelty/{run_id}")
def cancel(run_id: str, manager=Depends(get_experiment_manager)):
    handle = handle_for(manager, run_id)
    handle.metadata["progress"]["cancelled"] = True
    handle.cancel()
    return task(manager, run_id)
