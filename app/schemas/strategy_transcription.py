"""AI 策略转写（聚宽 → SVQuant）的请求、报告与响应模型。

本模块只定义 HTTP 契约；代码生成、能力核对和静态校验由
``app.api.ai_strategy_transcription`` 负责。
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


EngineHint = Literal["auto", "vectorized", "event"]
TargetEngine = Literal["vectorized", "event"]
IssueSeverity = Literal["blocker", "degraded", "info"]
IssueImpact = Literal["high", "medium", "low"]
IssueConfidence = Literal["certain", "likely", "needs_check"]
CheckStatus = Literal["pass", "warn", "fail", "skipped"]


class StrategyTranscriptionTarget(BaseModel):
    """转写目标回测配置；不包含任何账号或供应商凭据。"""

    model_config = ConfigDict(extra="forbid")

    engine_mode: EngineHint = "auto"
    universe: Optional[str] = None
    benchmark: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    initial_capital: Optional[float] = Field(None, gt=0)


class StrategyTranscriptionContext(BaseModel):
    """一次转写的非敏感上下文。"""

    model_config = ConfigDict(extra="forbid")

    target: StrategyTranscriptionTarget = Field(default_factory=StrategyTranscriptionTarget)
    accepted_degradations: list[str] = Field(default_factory=list)
    report_only: bool = False
    source_name: Optional[str] = None


class StrategyTranscriptionRequest(BaseModel):
    """``POST /api/ai/strategy-transcriptions`` 请求。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=200_000)
    context: StrategyTranscriptionContext = Field(default_factory=StrategyTranscriptionContext)
    idempotency_key: UUID


class TranscriptionOption(BaseModel):
    """一条差异的可执行降级选项。"""

    id: str
    label: str
    risk: IssueImpact
    note: str
    recommended: bool = False


class TranscriptionIssue(BaseModel):
    """报告中的一条差异；前端按 severity 与 impact 排序。"""

    code: str
    severity: IssueSeverity
    impact: IssueImpact
    line: int = 0
    title: str
    why: str
    effect: str
    options: list[TranscriptionOption] = Field(default_factory=list)
    evidence: str
    confidence: IssueConfidence = "certain"
    accepted: Optional[bool] = None


class TranscriptionMapping(BaseModel):
    """已完成的聚宽 → SVQuant 映射。"""

    jq: str
    svq: str
    status: Literal["equivalent", "degraded", "blocked", "config", "needs_check"]
    line: int = 0
    note: Optional[str] = None


class TranscriptionCheck(BaseModel):
    """生成代码的静态检查结果。"""

    id: Literal["syntax", "contract", "security", "future_data", "fields"]
    label: str
    status: CheckStatus
    detail: Optional[str] = None
    line: Optional[int] = None


class TranscribedStrategy(BaseModel):
    """通过静态校验的策略草稿；接口不会执行它。"""

    strategy_name: str
    strategy_code: str
    description: str
    used_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StrategyTranscriptionMeta(BaseModel):
    model_alias: str
    prompt_version: str
    capability_catalog_version: str
    data_version: str
    latency_ms: int


class StrategyTranscriptionResponse(BaseModel):
    """AI 策略转写结果。

    ``needs_input`` 表示状态上等待用户确认；此时 verdict 为 ``blocked``，
    ``report_only`` 表示用户只要报告，不返回可运行代码。
    """

    transcription_id: str
    status: Literal["ready", "rejected", "needs_input", "report_only"]
    verdict: Literal["equivalent", "degraded", "blocked"]
    target_engine: TargetEngine
    runnable: bool
    requires_ack: bool
    summary: str
    issues: list[TranscriptionIssue] = Field(default_factory=list)
    mappings: list[TranscriptionMapping] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    checks: list[TranscriptionCheck] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    strategy: Optional[TranscribedStrategy] = None
    meta: StrategyTranscriptionMeta
