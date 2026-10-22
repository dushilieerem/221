"""模拟盘内部结算路由（仅信任 SVQuant 内网调用）。"""

from __future__ import annotations

from datetime import date
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_data_engine
from app.schemas.paper_trading import (
    PaperSettlementRequest,
    PaperSettlementResponse,
    PaperStatusResponse,
    PaperTradingDaysResponse,
)
from app.services.paper_trading.errors import PaperTradingError
from app.services.paper_trading.price_view import release_trading_days
from app.services.paper_trading.settle import settle_paper_day
from app.services.paper_trading.status import paper_data_status

router = APIRouter(tags=["paper-trading"])

#: 结算会加载长历史面板；限制并发，避免多账户同时结算打满内存。
_SETTLE_SLOTS = BoundedSemaphore(2)


@router.get(
    "/internal/paper-trading/status",
    response_model=PaperStatusResponse,
)
def paper_trading_status(
    settlement_date: date | None = Query(None, alias="date"),
    data_engine=Depends(get_data_engine),
) -> PaperStatusResponse:
    return paper_data_status(data_engine, settlement_date=settlement_date)


@router.get(
    "/internal/paper-trading/trading-days",
    response_model=PaperTradingDaysResponse,
)
def paper_trading_days(
    start: date | None = None,
    end: date | None = None,
    data_engine=Depends(get_data_engine),
) -> PaperTradingDaysResponse:
    days = release_trading_days(data_engine)
    if start is not None:
        days = days[days >= str(start)]
    if end is not None:
        days = days[days <= str(end)]
    provider = getattr(data_engine, "provider", None)
    return PaperTradingDaysResponse(
        trading_days=[day.date().isoformat() for day in days],
        data_release_id=getattr(provider, "data_version", None),
        data_release_signature=getattr(data_engine, "release_id", None),
    )


@router.post(
    "/internal/paper-trading/settle",
    response_model=PaperSettlementResponse,
)
def paper_trading_settle(
    req: PaperSettlementRequest,
    data_engine=Depends(get_data_engine),
) -> PaperSettlementResponse:
    if not _SETTLE_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail={"code": "SETTLE_BUSY", "message": "结算并发已满，请稍后重试"},
            headers={"Retry-After": "2"},
        )
    try:
        return settle_paper_day(req, data_engine)
    except PaperTradingError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.payload()) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        ) from exc
    finally:
        _SETTLE_SLOTS.release()


__all__ = ["router"]
