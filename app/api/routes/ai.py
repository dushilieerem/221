"""AI 帖子摘要路由。

因子生成、策略生成、回测解读与策略转写已移交 SVQuant；RE 只保留社区
帖子摘要的兜底接口，等 SVQuant 移除 RE fallback 后再删除。
"""

from fastapi import APIRouter

from app.api.ai_post_summary import summarize_post
from app.schemas.post_summary import PostSummaryRequest, PostSummaryResponse


router = APIRouter()


@router.post("/ai/post-summaries", response_model=PostSummaryResponse)
def generate_post_summary(req: PostSummaryRequest) -> PostSummaryResponse:
    return summarize_post(req)


__all__ = ["router"]
