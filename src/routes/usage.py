"""Usage telemetry endpoints — Datadog-style API analytics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models.usage_request import UsageRequest
from src.schemas.usage import TopRouteResponse, TopCallerResponse, RouteCallResponse

router = APIRouter(prefix="/usage", tags=["usage"])


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/top-routes", response_model=list[TopRouteResponse])
async def top_routes(
    since_days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Top API routes by call volume over the last N days."""
    cutoff = _since(since_days)
    result = await db.execute(
        select(
            UsageRequest.route_template,
            UsageRequest.method,
            func.count(UsageRequest.id).label("total_calls"),
            func.count(func.distinct(UsageRequest.caller_service)).label("unique_callers"),
            func.avg(UsageRequest.duration_ms).label("avg_duration_ms"),
        )
        .where(UsageRequest.ts >= cutoff)
        .group_by(UsageRequest.route_template, UsageRequest.method)
        .order_by(func.count(UsageRequest.id).desc())
        .limit(limit)
    )
    return [
        TopRouteResponse(
            route_template=row.route_template,
            method=row.method,
            total_calls=row.total_calls,
            unique_callers=row.unique_callers,
            avg_duration_ms=round(float(row.avg_duration_ms or 0), 2),
        )
        for row in result.all()
    ]


@router.get("/top-callers", response_model=list[TopCallerResponse])
async def top_callers(
    route: str | None = None,
    since_days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Top caller services, optionally filtered by route."""
    cutoff = _since(since_days)
    query = (
        select(
            UsageRequest.caller_service,
            func.count(UsageRequest.id).label("call_count"),
            func.count(func.distinct(UsageRequest.route_template)).label("routes_called"),
        )
        .where(UsageRequest.ts >= cutoff)
    )
    if route:
        query = query.where(UsageRequest.route_template == route)
    query = (
        query
        .group_by(UsageRequest.caller_service)
        .order_by(func.count(UsageRequest.id).desc())
        .limit(limit)
    )
    result = await db.execute(query)
    return [
        TopCallerResponse(
            caller_service=row.caller_service,
            call_count=row.call_count,
            routes_called=row.routes_called,
        )
        for row in result.all()
    ]


@router.get("/route-calls", response_model=list[RouteCallResponse])
async def route_calls(
    route: str | None = None,
    caller_service: str | None = None,
    since_days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Recent individual API calls, optionally filtered."""
    cutoff = _since(since_days)
    query = (
        select(UsageRequest)
        .where(UsageRequest.ts >= cutoff)
    )
    if route:
        query = query.where(UsageRequest.route_template == route)
    if caller_service:
        query = query.where(UsageRequest.caller_service == caller_service)
    query = query.order_by(UsageRequest.ts.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()
