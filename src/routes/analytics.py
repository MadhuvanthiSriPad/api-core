"""Analytics endpoints — token usage stats and cost breakdowns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.entities.token_usage import TokenUsage
from src.entities.agent_session import AgentSession
from src.schemas.sessions import TokenUsageStats

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/token-usage", response_model=TokenUsageStats)
async def get_token_usage(
    team_id: str | None = None,
    hours: int = Query(default=24, le=720),
    db: AsyncSession = Depends(get_db),
):
    """Get token usage statistics for a time period."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    query = (
        select(
            func.coalesce(func.sum(TokenUsage.input_tokens), 0).label("input"),
            func.coalesce(func.sum(TokenUsage.output_tokens), 0).label("output"),
            func.coalesce(func.sum(TokenUsage.cached_tokens), 0).label("cached"),
            func.coalesce(func.sum(TokenUsage.cost), 0.0).label("cost"),
        )
        .where(TokenUsage.timestamp >= since)
    )
    if team_id:
        query = query.join(
            AgentSession, TokenUsage.session_id == AgentSession.session_id
        ).where(AgentSession.team_id == team_id)

    result = await db.execute(query)
    row = result.one()

    # Breakdown by model
    model_query = (
        select(
            TokenUsage.model,
            func.sum(TokenUsage.input_tokens).label("input"),
            func.sum(TokenUsage.output_tokens).label("output"),
            func.sum(TokenUsage.cost).label("cost"),
        )
        .where(TokenUsage.timestamp >= since)
        .group_by(TokenUsage.model)
    )
    if team_id:
        model_query = model_query.join(
            AgentSession, TokenUsage.session_id == AgentSession.session_id
        ).where(AgentSession.team_id == team_id)

    model_result = await db.execute(model_query)
    breakdown = [
        {
            "model": r.model,
            "input_tokens": int(r.input or 0),
            "output_tokens": int(r.output or 0),
            "cost": round(float(r.cost or 0), 4),
        }
        for r in model_result.all()
    ]

    return TokenUsageStats(
        period=f"last_{hours}h",
        total_input_tokens=int(row.input or 0),
        total_output_tokens=int(row.output or 0),
        total_cached_tokens=int(row.cached or 0),
        total_cost=round(float(row.cost or 0), 4),
        breakdown_by_model=breakdown,
    )


@router.get("/token-usage/daily")
async def get_daily_token_usage(
    days: int = Query(default=7, le=30),
    db: AsyncSession = Depends(get_db),
):
    """Get daily token usage breakdown for charting."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            func.date(TokenUsage.timestamp).label("date"),
            func.coalesce(func.sum(TokenUsage.input_tokens), 0).label("input"),
            func.coalesce(func.sum(TokenUsage.output_tokens), 0).label("output"),
        )
        .where(TokenUsage.timestamp >= since)
        .group_by(func.date(TokenUsage.timestamp))
        .order_by(func.date(TokenUsage.timestamp))
    )

    return [
        {
            "date": str(r.date),
            "input_tokens": int(r.input or 0),
            "output_tokens": int(r.output or 0),
        }
        for r in result.all()
    ]


@router.get("/cost-by-team")
async def get_cost_by_team(
    hours: int = Query(default=24, le=720),
    db: AsyncSession = Depends(get_db),
):
    """Get cost breakdown per team."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    result = await db.execute(
        select(
            AgentSession.team_id,
            func.count(AgentSession.session_id).label("total_sessions"),
            func.coalesce(func.sum(AgentSession.total_cost), 0.0).label("cost"),
            func.coalesce(func.sum(
                AgentSession.input_tokens + AgentSession.output_tokens
            ), 0).label("tokens"),
        )
        .where(AgentSession.started_at >= since)
        .group_by(AgentSession.team_id)
        .order_by(func.sum(AgentSession.total_cost).desc())
    )

    return [
        {
            "team_id": r.team_id,
            # Preserve the previous "sessions" key while providing
            # the contract expected by billing-service ("total_sessions").
            "total_sessions": int(r.total_sessions),
            "sessions": int(r.total_sessions),
            "total_cost": round(float(r.cost), 4),
            "total_tokens": int(r.tokens),
        }
        for r in result.all()
    ]
