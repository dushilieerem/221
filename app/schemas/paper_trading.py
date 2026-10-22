"""模拟盘内部结算接口的请求/响应模型（pydantic v2）。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PaperPositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str = Field(min_length=1)
    quantity: int = Field(ge=0)
    available_quantity: int = Field(ge=0)
    average_cost: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_available(self):
        if self.available_quantity > self.quantity:
            raise ValueError(
                f"{self.symbol} 的可卖数量 {self.available_quantity} 超过持仓 {self.quantity}"
            )
        if not self.symbol.strip():
            raise ValueError("持仓证券代码不能为空")
        return self


class PaperAccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cash: float = Field(ge=0)
    positions: list[PaperPositionInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_positions(self):
        symbols = [item.symbol for item in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("账户持仓证券不可重复")
        return self


class PaperExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    commission_rate: float = Field(0.0003, ge=0)
    min_commission: float = Field(5.0, ge=0)
    stamp_tax_rate: float = Field(0.0005, ge=0)
    #: 首期不模拟滑点；字段保留用于和账户快照对齐。
    slippage_rate: float = Field(0.0, ge=0)
    lot_size: int = Field(100, ge=1)
    #: 信号回看交易日数量；内置事件策略最长约 757 个交易日。
    history_days: int = Field(900, ge=60, le=3000)
    filter_st: bool = False


class PaperSettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    settlement_date: date
    engine_mode: Literal["event", "vectorized"]
    strategy_code: str = Field(min_length=1)
    expected_code_hash: str | None = None
    expected_data_release_id: str | None = None
    universe: str = Field(default="CSI500", min_length=1)
    benchmark: str = Field(default="CSI300", min_length=1)
    account: PaperAccountInput
    strategy_state: dict | None = None
    strategy_state_code_hash: str | None = None
    expected_state_hash: str | None = None
    execution: PaperExecutionConfig = Field(default_factory=PaperExecutionConfig)


# ---------------- 响应 ----------------


class PaperOrderInfo(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    order_id: str
    symbol: str
    side: str | None = None
    quantity: int | None = None
    target_value: float | None = None
    status: str
    reason_code: str | None = None
    submitted_at: str | None = None


class PaperFillInfo(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    commission: float
    stamp_tax: float
    timestamp: str


class PaperPositionOut(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str
    quantity: int
    available_quantity: int
    average_cost: float
    mark_price: float
    market_value: float


class PaperNextAccount(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    cash: float
    positions: list[PaperPositionOut]
    positions_value: float
    total_equity: float


class PaperSettlementResponse(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    status: Literal["settled"] = "settled"
    settlement_date: str
    signal_date: str
    next_trading_day: str | None = None
    engine_mode: Literal["event", "vectorized"]
    data_release_id: str | None = None
    data_release_signature: str | None = None
    data_version: str | None = None
    code_hash: str
    state_hash: str | None = None
    orders: list[PaperOrderInfo]
    fills: list[PaperFillInfo]
    next_account: PaperNextAccount
    strategy_state: dict
    fees: dict[str, float]
    warnings: list[str]
    assumptions: dict[str, str]


class PaperStatusResponse(BaseModel):
    status: Literal["ready", "data_not_ready", "not_trading_day", "no_release"]
    settlement_date: str | None = None
    latest_ready_trading_day: str | None = None
    signal_date: str | None = None
    next_trading_day: str | None = None
    data_release_id: str | None = None
    data_release_signature: str | None = None
    data_version: str | None = None
    release_stale: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    calendar_source: str = "panel_index"


class PaperTradingDaysResponse(BaseModel):
    trading_days: list[str]
    data_release_id: str | None = None
    data_release_signature: str | None = None


__all__ = [
    "PaperAccountInput",
    "PaperExecutionConfig",
    "PaperFillInfo",
    "PaperNextAccount",
    "PaperOrderInfo",
    "PaperPositionInput",
    "PaperPositionOut",
    "PaperSettlementRequest",
    "PaperSettlementResponse",
    "PaperStatusResponse",
    "PaperTradingDaysResponse",
]
