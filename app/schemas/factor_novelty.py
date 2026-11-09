"""因子值查重接口的请求/参考合同。候选与参考均需显式、可核验的冻结身份。"""

from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.backtest import FactorBacktestRequest
from app.services.novelty.source import validate_source


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_id: str = Field(min_length=1, max_length=200)
    factor_name: str = Field(min_length=1, max_length=100)
    library_name: str | None = None
    factor_code: str | None = Field(default=None, max_length=30000)
    source_hash: str | None = None
    max_window: int | None = Field(default=None, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def valid(self):
        if bool(self.library_name) == bool(self.factor_code):
            raise ValueError("Choose exactly one reference source")
        if self.factor_code:
            validate_source(self.factor_code, self.factor_name)
        return self


class NoveltyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate: FactorBacktestRequest
    references: list[Reference] = Field(min_length=1, max_length=1000)
    expected_data_release_id: str = Field(min_length=1, max_length=128)
    expected_candidate_source_hash: str = Field(min_length=1, max_length=128)
    expected_candidate_spec_hash: str = Field(min_length=1, max_length=128)
    method: Literal["daily_abs_spearman"] = "daily_abs_spearman"
    min_pairs: int = Field(default=30, ge=3, le=10000, strict=True)
    reference_warmup_sessions: int = Field(default=252, ge=252, le=5000, strict=True)

    @model_validator(mode="after")
    def valid(self):
        c = self.candidate
        if (
            not c.start
            or not c.end
            or date.fromisoformat(c.start) > date.fromisoformat(c.end)
        ):
            raise ValueError("Explicit ordered dates required")
        if c.factor_code:
            validate_source(c.factor_code, c.factor_name)
        if len({r.reference_id for r in self.references}) != len(self.references):
            raise ValueError("Reference IDs must be unique")
        if sum(len(r.factor_code or "") for r in self.references) > 2000000:
            raise ValueError("Reference code payload exceeds limit")
        return self
