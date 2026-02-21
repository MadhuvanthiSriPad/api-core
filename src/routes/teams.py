"""Team management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.team import Team
from src.models.agent_session import AgentSession
from src.schemas.sessions import TeamResponse

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("", response_model=list[TeamResponse])
async def list_teams(db: AsyncSession = Depends(get_db)):
    """List all teams with session counts and costs."""
    result = await db.execute(
        select(
            Team,
            func.count(AgentSession.session_id).label("session_count"),
            func.coalesce(func.sum(AgentSession.total_cost), 0.0).label("total_cost"),
        )
        .outerjoin(AgentSession, Team.id == AgentSession.team_id)
        .group_by(Team.id)
        .order_by(Team.name)
    )
    rows = result.all()
    return [
        TeamResponse(
            id=team.id,
            name=team.name,
            plan=team.plan,
            monthly_budget=team.monthly_budget,
            created_at=team.created_at,
            session_count=session_count,
            total_cost=round(float(total_cost), 4),
        )
        for team, session_count, total_cost in rows
    ]


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(team_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single team."""
    result = await db.execute(
        select(
            Team,
            func.count(AgentSession.session_id).label("session_count"),
            func.coalesce(func.sum(AgentSession.total_cost), 0.0).label("total_cost"),
        )
        .outerjoin(AgentSession, Team.id == AgentSession.team_id)
        .where(Team.id == team_id)
        .group_by(Team.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    team, session_count, total_cost = row
    return TeamResponse(
        id=team.id,
        name=team.name,
        plan=team.plan,
        monthly_budget=team.monthly_budget,
        created_at=team.created_at,
        session_count=session_count,
        total_cost=round(float(total_cost), 4),
    )
