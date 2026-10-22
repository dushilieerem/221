"""模拟盘结算的机器可读错误。"""

from __future__ import annotations

from typing import Any


class PaperTradingError(Exception):
    """带 HTTP 状态码和稳定错误码的结算异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 422,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def data_not_ready(message: str, **details: Any) -> PaperTradingError:
    return PaperTradingError("DATA_NOT_READY", message, http_status=409, **details)


def not_trading_day(message: str, **details: Any) -> PaperTradingError:
    return PaperTradingError("NOT_A_TRADING_DAY", message, http_status=409, **details)


def conflict(code: str, message: str, **details: Any) -> PaperTradingError:
    return PaperTradingError(code, message, http_status=409, **details)


def invalid(code: str, message: str, **details: Any) -> PaperTradingError:
    return PaperTradingError(code, message, http_status=422, **details)


__all__ = [
    "PaperTradingError",
    "conflict",
    "data_not_ready",
    "invalid",
    "not_trading_day",
]
