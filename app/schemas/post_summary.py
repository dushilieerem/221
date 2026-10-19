"""帖子摘要只接收待归纳内容，不接收供应商地址、密钥或任意系统提示。"""
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PostSummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field("", max_length=512)
    body: str = Field("", max_length=20000)
    code_snippet: str = Field("", max_length=20000)
    grounding: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False

    @model_validator(mode="after")
    def validate_content(self):
        if not self.body.strip() and not self.code_snippet.strip():
            raise ValueError("正文与代码为空，无法生成摘要")
        if len(json.dumps(self.grounding, ensure_ascii=False)) > 12000:
            raise ValueError("摘要引用信息过长")
        return self


class PostSummaryResponse(BaseModel):
    summary_md: str = Field(min_length=1, max_length=4000)
    summary_source: Literal["llm"] = "llm"
    provider: str = "openai_compatible"
    model: str
    tokens_in: int = Field(0, ge=0)
    tokens_out: int = Field(0, ge=0)
