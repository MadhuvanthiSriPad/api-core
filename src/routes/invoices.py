"""Invoice endpoints — team session summary for billing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.entities.agent_session import AgentSession

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.get("/team-summary")
async def get_team_summary(
    hours: int = Query(default=720, le=8760),
    db: AsyncSession = Depends(get_db),
):
    """Get session counts and costs grouped by team for invoice generation.

    Uses the same session counting pattern as analytics cost-by-team.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    result = await db.execute(
        select(
            AgentSession.team_id,
            func.count(AgentSession.session_id).label("session_count"),
            func.coalesce(func.sum(AgentSession.total_cost), 0.0).label("total_cost"),
            func.coalesce(func.sum(AgentSession.input_tokens), 0).label("total_input_tokens"),
            func.coalesce(func.sum(AgentSession.output_tokens), 0).label("total_output_tokens"),
        )
        .where(AgentSession.started_at >= since)
        .group_by(AgentSession.team_id)
        .order_by(func.sum(AgentSession.total_cost).desc())
    )

    return [
        {
            "team_id": r.team_id,
            # Keep session_count for backward compatibility while exposing
            # the canonical total_sessions field consumed by billing-service.
            "total_sessions": int(r.session_count),
            "session_count": int(r.session_count),
            "total_cost": round(float(r.total_cost), 4),
            "total_input_tokens": int(r.total_input_tokens),
            "total_output_tokens": int(r.total_output_tokens),
        }
        for r in result.all()
    ]
