"""模拟盘数据就绪状态。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.schemas.paper_trading import PaperStatusResponse
from app.services.paper_trading.price_view import release_trading_days
from app.services.paper_trading.trading_calendar import (
    calendar_path,
    classify_day,
    next_trading_day,
)


def _iso(value: pd.Timestamp | None) -> str | None:
    return None if value is None else pd.Timestamp(value).date().isoformat()


def paper_data_status(
    data_engine,
    *,
    settlement_date: date | pd.Timestamp | None = None,
) -> PaperStatusResponse:
    """判断指定日期能否结算，并给出下一交易日。"""

    provider = getattr(data_engine, "provider", None)
    if provider is None:
        return PaperStatusResponse(status="no_release")

    days = release_trading_days(data_engine)
    data_version = getattr(provider, "data_version", None)
    release_signature = getattr(data_engine, "release_id", None)
    current_signature = getattr(provider, "current_release_signature", None)
    release_stale = False
    if callable(current_signature):
        try:
            release_stale = bool(current_signature() != release_signature)
        except Exception:  # noqa: BLE001 - 状态探测不应因签名异常失败
            release_stale = False

    target = (
        pd.Timestamp(settlement_date).normalize()
        if settlement_date is not None
        else (pd.Timestamp(days[-1]) if len(days) else None)
    )
    latest_ready = None
    if len(days):
        if target is None:
            latest_ready = pd.Timestamp(days[-1])
        else:
            earlier = days[days <= target]
            if len(earlier):
                latest_ready = pd.Timestamp(earlier[-1])

    base = dict(
        settlement_date=_iso(target),
        latest_ready_trading_day=_iso(latest_ready),
        data_release_id=str(data_version) if data_version else None,
        data_release_signature=str(release_signature) if release_signature else None,
        data_version=str(data_version) if data_version else None,
        release_stale=release_stale,
        calendar_source="calendar" if calendar_path(data_engine.data_dir) else "panel_index",
    )

    if target is None or not len(days):
        return PaperStatusResponse(status="data_not_ready", **base)

    day_class = classify_day(data_engine.data_dir, target)
    if day_class == "not_trading_day":
        return PaperStatusResponse(
            status="not_trading_day",
            next_trading_day=_iso(next_trading_day(data_engine.data_dir, target)),
            **base,
        )

    missing_fields: list[str] = []
    try:
        data_engine.get_task_panels(
            target, target, fields=["close", "amount", "volume"], symbols=[]
        )
    except KeyError as exc:
        missing_fields = [str(exc)]

    if target not in days:
        return PaperStatusResponse(
            status="data_not_ready",
            missing_fields=missing_fields,
            **base,
        )
    if missing_fields:
        return PaperStatusResponse(
            status="data_not_ready",
            missing_fields=missing_fields,
            **base,
        )

    signal_days = days[days < target]
    return PaperStatusResponse(
        status="ready",
        signal_date=_iso(pd.Timestamp(signal_days[-1])) if len(signal_days) else None,
        **base,
    )


__all__ = ["paper_data_status"]
