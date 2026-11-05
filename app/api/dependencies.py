"""HTTP 路由所需的应用级依赖。"""

from __future__ import annotations

from fastapi import Request

from app.services.data.engine import DataEngine
from app.services.experiment.manager import ExperimentManager


def get_data_engine(request: Request) -> DataEngine:
    """返回 lifespan 装配的行情引擎；模拟盘结算不经过回测队列。"""

    engine = getattr(request.app.state, "data_engine", None)
    if not isinstance(engine, DataEngine):
        raise RuntimeError("DataEngine 尚未由 FastAPI lifespan 初始化")
    return engine


def get_experiment_manager(request: Request) -> ExperimentManager:
    """返回 FastAPI lifespan 持有的唯一 ExperimentManager。"""

    manager = getattr(request.app.state, "experiment_manager", None)
    if not isinstance(manager, ExperimentManager):
        raise RuntimeError("ExperimentManager 尚未由 FastAPI lifespan 初始化")
    return manager


__all__ = [
    "get_data_engine",
    "get_experiment_manager",
]
