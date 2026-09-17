"""ResearchEngine FastAPI 唯一正式入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import (
    DEFAULT_CONFIG_PATH,
    default_runtime_settings,
    load_runtime_settings,
)
from app.logging import configure_logging
from app.services.factor_backtest_batch.task_registry import FactorBacktestBatchTaskRegistry
from app.services.experiment.manager import ExperimentManager
from app.services.data.engine import DataEngine
from app.services.data.manifest import validate_data_manifest


logger = logging.getLogger(__name__)


def _settings_for_import():
    """允许测试导入 app；真正启动时仍由 lifespan 强制加载配置。"""

    try:
        return load_runtime_settings(DEFAULT_CONFIG_PATH)
    except FileNotFoundError:
        return default_runtime_settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """统一管理配置、数据 release、Manager 与批量任务生命周期。"""

    testing = bool(getattr(application.state, "testing", False))
    settings = (
        application.state.settings
        if testing
        else load_runtime_settings(DEFAULT_CONFIG_PATH)
    )
    application.state.settings = settings
    configure_logging(settings.log_level)

    manager: ExperimentManager = application.state.experiment_manager
    if not testing and (
        manager.max_workers != settings.max_workers
        or manager.max_queue_size != settings.max_queue_size
    ):
        # create_app 可能在 config.yaml 被修改前构造；以启动时配置为准。
        manager.shutdown()
        manager = ExperimentManager(
            max_workers=settings.max_workers,
            max_queue_size=settings.max_queue_size,
        )
        application.state.experiment_manager = manager

    task_registry: FactorBacktestBatchTaskRegistry = (
        application.state.factor_backtest_batch_task_registry
    )
    data_engine: DataEngine = application.state.data_engine

    if not testing:
        manifest = validate_data_manifest(
            settings.data_dir,
            manifest_path=settings.data_manifest,
            expected_data_version=settings.expected_data_version,
            expected_manifest_sha256=settings.expected_manifest_sha256,
            verify_hashes=settings.verify_data_hashes,
        )
        application.state.data_manifest = manifest
        logger.info(
            "loaded market data release: version=%s assets=%d path=%s",
            manifest.data_version,
            manifest.asset_count,
            manifest.data_dir,
        )

        # 只装载数据来源；字段在因子任务按 spec 日期请求时读取，不做全量预热。
        data_engine.load_parquet(manifest.data_dir)
    manager.start()
    try:
        yield
    finally:
        # 协调线程可能正在等待 Manager 的因子 worker；必须先等它退出。
        task_registry.shutdown(wait=True)
        manager.shutdown()
        data_engine.close()


def create_app(*, testing: bool = False) -> FastAPI:
    settings = _settings_for_import()
    application = FastAPI(
        title="ResearchEngine API",
        version="2.0.0",
        description="因子/策略回测与已发布市场数据查询 API。",
        lifespan=lifespan,
    )
    data_engine = DataEngine()
    DataEngine.configure_instance(data_engine)
    manager = ExperimentManager(
        max_workers=settings.max_workers,
        max_queue_size=settings.max_queue_size,
    )
    application.state.settings = settings
    application.state.testing = testing
    application.state.data_engine = data_engine
    application.state.experiment_manager = manager
    application.state.factor_backtest_batch_task_registry = FactorBacktestBatchTaskRegistry()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )
    application.include_router(router)
    return application


app = create_app()


def main() -> None:
    import uvicorn

    settings = load_runtime_settings(DEFAULT_CONFIG_PATH)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=False,
    )


if __name__ == "__main__":
    main()
