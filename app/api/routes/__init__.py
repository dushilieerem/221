"""按职责拆分的 ResearchEngine HTTP 路由集合。"""

from fastapi import APIRouter

from app.api.routes import (
    catalog,
    data,
    factor,
    novelty,
    health,
    ml,
    paper_trading,
    strategy,
)


router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(data.router)
router.include_router(catalog.router)
router.include_router(factor.router)
router.include_router(novelty.router)
router.include_router(ml.router)
router.include_router(strategy.router)
router.include_router(paper_trading.router)

_builtin_factors = catalog._builtin_factors

__all__ = ["router", "_builtin_factors"]
