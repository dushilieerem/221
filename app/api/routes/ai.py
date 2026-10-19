"""AI 草稿与回测解读路由。"""

from fastapi import APIRouter

from app.api.ai_factor import generate_factor
from app.api.ai_interpretation import interpret_backtest
from app.api.ai_strategy import generate_strategy
from app.api.ai_post_summary import summarize_post
from app.schemas.post_summary import PostSummaryRequest, PostSummaryResponse
from app.schemas.backtest import (
    BacktestInterpretationRequest,
    BacktestInterpretationResponse,
    FactorGenerationRequest,
    FactorGenerationResponse,
    StrategyGenerationRequest,
    StrategyGenerationResponse,
)


router = APIRouter()


@router.post("/ai/post-summaries", response_model=PostSummaryResponse)
def generate_post_summary(req: PostSummaryRequest) -> PostSummaryResponse:
    return summarize_post(req)


@router.post("/ai/factor-generations", response_model=FactorGenerationResponse)
def generate_factor_draft(req: FactorGenerationRequest) -> FactorGenerationResponse:
    return generate_factor(req)


@router.post("/ai/strategy-generations", response_model=StrategyGenerationResponse)
def generate_strategy_draft(req: StrategyGenerationRequest) -> StrategyGenerationResponse:
    return generate_strategy(req)


@router.post("/ai/backtest-interpretations", response_model=BacktestInterpretationResponse)
def interpret_backtest_result(
    req: BacktestInterpretationRequest,
) -> BacktestInterpretationResponse:
    return interpret_backtest(req)


__all__ = ["router"]
