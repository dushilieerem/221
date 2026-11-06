"""行情数据 release 状态与手动重载路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import get_runtime_settings
from app.schemas.backtest import DataReleaseStatusResponse
from app.services.data.engine import DataEngine
from app.services.data.manifest import DataManifestError
from app.services.data.release_watcher import reload_data_release
from app.services.data.view_lock import active_view_lock


router = APIRouter()


def _release_status(
    engine: DataEngine,
    *,
    data_version: str | None = None,
) -> DataReleaseStatusResponse:
    if data_version is None:
        data_version = getattr(engine.provider, "data_version", None)
    lock = None
    if engine.data_dir is not None:
        lock = active_view_lock(engine.data_dir)
    return DataReleaseStatusResponse(
        data_version=data_version,
        release_id=engine.release_id,
        data_dir=str(engine.data_dir) if engine.data_dir is not None else None,
        view_id=lock.view_id if lock is not None else None,
        manifest_sha256=lock.manifest_sha256 if lock is not None else None,
    )


@router.get("/data/status", response_model=DataReleaseStatusResponse)
def data_status() -> DataReleaseStatusResponse:
    """返回当前已装载行情 release 的版本、签名和物理目录。"""

    return _release_status(DataEngine.get_instance())


@router.post("/data/reload", response_model=DataReleaseStatusResponse)
def data_reload() -> DataReleaseStatusResponse:
    """重新校验行情清单并强制切换到最新 release。

    发布监视器之外的运维兜底入口；校验失败或数据目录不可用时返回 503，
    当前 release 保持不变。
    """

    engine = DataEngine.get_instance()
    try:
        manifest = reload_data_release(engine, get_runtime_settings())
    except DataManifestError as exc:
        raise HTTPException(
            status_code=503, detail=f"行情数据清单校验失败: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 重载失败时服务必须保持旧 release
        raise HTTPException(
            status_code=503, detail=f"行情数据重载失败: {exc}"
        ) from exc
    return _release_status(engine, data_version=manifest.data_version)


__all__ = ["router"]
