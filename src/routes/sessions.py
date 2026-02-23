"""Agent session endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.entities.agent_session import AgentSession, SessionStatus
from src.entities.token_usage import TokenUsage
from src.config import settings, calculate_cost
from src.schemas.sessions import (
    SessionCreate,
    SessionResponse,
    SessionUpdate,
    SessionStats,
    TokenUsageRecord,
    UsageBlock,
    BillingBlock,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _session_to_response(session: AgentSession) -> SessionResponse:
    """Convert an ORM AgentSession to the nested SessionResponse schema."""
    return SessionResponse(
        session_id=session.session_id,
        team_id=session.team_id,
        agent_name=session.agent_name,
        model=session.model,
        status=session.status,
        priority=session.priority,
        usage=UsageBlock(
            input_tokens=session.input_tokens,
            output_tokens=session.output_tokens,
            cached_tokens=session.cached_tokens,
        ),
        billing=BillingBlock(total=session.total_cost),
        started_at=session.started_at,
        ended_at=session.ended_at,
        duration_seconds=session.duration_seconds,
        error_message=session.error_message,
        tags=session.tags,
    )


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)):
    """Start a new agent session."""
    session = AgentSession(
        session_id=f"sess_{uuid.uuid4().hex[:16]}",
        team_id=body.team_id,
        agent_name=body.agent_name,
        model=body.model,
        status=SessionStatus.RUNNING.value,
        priority=body.priority.value,
        prompt=body.prompt,
        tags=body.tags,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _session_to_response(session)


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    team_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List agent sessions with optional filters."""
    query = select(AgentSession).order_by(AgentSession.started_at.desc())
    if team_id:
        query = query.where(AgentSession.team_id == team_id)
    if status:
        query = query.where(AgentSession.status == status)
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    sessions = result.scalars().all()
    return [_session_to_response(s) for s in sessions]


@router.get("/stats", response_model=SessionStats)
async def get_session_stats(
    team_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get aggregate session statistics."""
    base = select(AgentSession)
    if team_id:
        base = base.where(AgentSession.team_id == team_id)

    result = await db.execute(
        select(
            func.count(AgentSession.session_id).label("total"),
            func.sum(case(
                (AgentSession.status == "running", 1), else_=0
            )).label("active"),
            func.sum(case(
                (AgentSession.status == "completed", 1), else_=0
            )).label("completed"),
            func.sum(case(
                (AgentSession.status == "failed", 1), else_=0
            )).label("failed"),
            func.coalesce(func.sum(
                AgentSession.input_tokens + AgentSession.output_tokens + AgentSession.cached_tokens
            ), 0).label("total_tokens"),
            func.coalesce(func.sum(AgentSession.total_cost), 0.0).label("total_cost"),
            func.coalesce(func.avg(AgentSession.duration_seconds), 0.0).label("avg_duration"),
        ).where(AgentSession.team_id == team_id if team_id else True)
    )
    row = result.one()
    total = row.total or 0
    completed = row.completed or 0
    failed = row.failed or 0

    return SessionStats(
        total_sessions=total,
        active_sessions=row.active or 0,
        completed_sessions=completed,
        failed_sessions=failed,
        total_tokens=row.total_tokens or 0,
        total_cost=round(float(row.total_cost or 0), 4),
        avg_duration_seconds=round(float(row.avg_duration or 0), 2),
        success_rate=round(completed / total * 100, 1) if total > 0 else 0.0,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single session by ID."""
    result = await db.execute(
        select(AgentSession).where(AgentSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return _session_to_response(session)


@router.patch("/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str, body: SessionUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a session (e.g., mark completed, add tokens)."""
    result = await db.execute(
        select(AgentSession).where(AgentSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(session, field, value)

    # Recalculate cost if tokens changed
    if any(k in update_data for k in ("input_tokens", "output_tokens", "cached_tokens")):
        session.total_cost = calculate_cost(
            session.input_tokens, session.output_tokens, session.cached_tokens
        )

    # Calculate duration if ended
    if session.ended_at and session.started_at:
        session.duration_seconds = (session.ended_at - session.started_at).total_seconds()

    await db.commit()
    await db.refresh(session)
    return _session_to_response(session)


@router.post("/{session_id}/tokens", response_model=TokenUsageRecord, status_code=201)
async def record_token_usage(
    session_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Record a token usage event for a session."""
    result = await db.execute(
        select(AgentSession).where(AgentSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    cost = calculate_cost(input_tokens, output_tokens, cached_tokens)

    event = TokenUsage(
        session_id=session_id,
        model=session.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost=cost,
    )
    db.add(event)

    # Update session totals
    session.input_tokens += input_tokens
    session.output_tokens += output_tokens
    session.cached_tokens += cached_tokens
    session.total_cost += cost

    await db.commit()
    await db.refresh(event)

    return TokenUsageRecord(
        timestamp=event.timestamp,
        model=event.model,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        cached_tokens=event.cached_tokens,
        cost=event.cost,
    )
