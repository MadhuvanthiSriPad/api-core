"""Usage telemetry endpoints — Datadog-style API analytics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.entities.usage_request import UsageRequest
from src.schemas.usage import (
    TopRouteResponse, TopCallerResponse, RouteCallResponse,
    ServiceHealthResponse, RouteErrorRateResponse, LatencyPercentilesResponse,
)
from propagate.service_map import load_service_map

router = APIRouter(prefix="/usage", tags=["usage"])


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _known_callers_filter():
    return func.lower(UsageRequest.caller_service).notin_(["", "unknown"])


def _top_caller_exclusions() -> set[str]:
    """Return callers hidden from rankings via service_map metadata."""
    try:
        return {
            name
            for name, info in load_service_map().items()
            if not info.include_in_top_callers
        }
    except Exception:
        return set()


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
            func.count(
                func.distinct(
                    case(
                        (_known_callers_filter(), UsageRequest.caller_service),
                        else_=None,
                    )
                )
            ).label("unique_callers"),
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
    excluded_callers = _top_caller_exclusions()
    query = (
        select(
            UsageRequest.caller_service,
            func.count(UsageRequest.id).label("call_count"),
            func.count(func.distinct(UsageRequest.route_template)).label("routes_called"),
        )
        .where(UsageRequest.ts >= cutoff)
        .where(_known_callers_filter())
    )
    if excluded_callers:
        query = query.where(UsageRequest.caller_service.notin_(excluded_callers))
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
    else:
        query = query.where(_known_callers_filter())
    query = query.order_by(UsageRequest.ts.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/service-health", response_model=list[ServiceHealthResponse])
async def service_health(
    since_days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Per-service health summary derived from usage_requests telemetry."""
    cutoff = _since(since_days)
    result = await db.execute(
        select(
            UsageRequest.caller_service,
            func.count(UsageRequest.id).label("total"),
            func.sum(case(
                (UsageRequest.status_code.between(400, 499), 1), else_=0
            )).label("err4xx"),
            func.sum(case(
                (UsageRequest.status_code >= 500, 1), else_=0
            )).label("err5xx"),
            func.avg(UsageRequest.duration_ms).label("avg_lat"),
            func.max(UsageRequest.ts).label("last_seen"),
        )
        .where(UsageRequest.ts >= cutoff)
        .where(_known_callers_filter())
        .group_by(UsageRequest.caller_service)
        .order_by(func.count(UsageRequest.id).desc())
    )
    out = []
    for row in result.all():
        total = row.total or 0
        e4 = int(row.err4xx or 0)
        e5 = int(row.err5xx or 0)
        err_rate = round((e4 + e5) / total * 100, 2) if total else 0.0
        srv_err = round(e5 / total * 100, 2) if total else 0.0
        uptime = round((total - e5) / total * 100, 2) if total else 100.0
        out.append(ServiceHealthResponse(
            caller_service=row.caller_service,
            total_requests=total,
            error_4xx=e4,
            error_5xx=e5,
            error_rate_pct=err_rate,
            server_error_rate_pct=srv_err,
            avg_latency_ms=round(float(row.avg_lat or 0), 2),
            uptime_pct=uptime,
            last_seen=row.last_seen,
        ))
    return out


@router.get("/error-rates", response_model=list[RouteErrorRateResponse])
async def error_rates(
    since_days: int = Query(default=7, ge=1, le=90),
    min_calls: int = Query(default=1, ge=1),
    limit: int = Query(default=30, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Per-route error breakdown from usage_requests telemetry."""
    cutoff = _since(since_days)
    result = await db.execute(
        select(
            UsageRequest.route_template,
            UsageRequest.method,
            func.count(UsageRequest.id).label("total"),
            func.sum(case(
                (UsageRequest.status_code.between(200, 299), 1), else_=0
            )).label("s2xx"),
            func.sum(case(
                (UsageRequest.status_code.between(400, 499), 1), else_=0
            )).label("e4xx"),
            func.sum(case(
                (UsageRequest.status_code >= 500, 1), else_=0
            )).label("e5xx"),
            func.avg(UsageRequest.duration_ms).label("avg_lat"),
        )
        .where(UsageRequest.ts >= cutoff)
        .group_by(UsageRequest.route_template, UsageRequest.method)
        .having(func.count(UsageRequest.id) >= min_calls)
        .order_by(func.sum(case((UsageRequest.status_code >= 500, 1), else_=0)).desc())
        .limit(limit)
    )
    out = []
    for row in result.all():
        total = row.total or 0
        e4 = int(row.e4xx or 0)
        e5 = int(row.e5xx or 0)
        out.append(RouteErrorRateResponse(
            route_template=row.route_template,
            method=row.method,
            total_calls=total,
            success_2xx=int(row.s2xx or 0),
            client_errors_4xx=e4,
            server_errors_5xx=e5,
            error_rate_pct=round((e4 + e5) / total * 100, 2) if total else 0.0,
            server_error_rate_pct=round(e5 / total * 100, 2) if total else 0.0,
            avg_latency_ms=round(float(row.avg_lat or 0), 2),
        ))
    return out


@router.get("/latency-percentiles", response_model=list[LatencyPercentilesResponse])
async def latency_percentiles(
    since_days: int = Query(default=7, ge=1, le=30),
    min_calls: int = Query(default=5, ge=2),
    route_limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
):
    """P50/P95/P99 latency per route (computed in Python for SQLite compat)."""
    cutoff = _since(since_days)
    top_q = await db.execute(
        select(
            UsageRequest.route_template,
            UsageRequest.method,
            func.count(UsageRequest.id).label("cnt"),
        )
        .where(UsageRequest.ts >= cutoff)
        .group_by(UsageRequest.route_template, UsageRequest.method)
        .having(func.count(UsageRequest.id) >= min_calls)
        .order_by(func.count(UsageRequest.id).desc())
        .limit(route_limit)
    )
    out = []
    for route_row in top_q.all():
        dur_q = await db.execute(
            select(UsageRequest.duration_ms)
            .where(UsageRequest.ts >= cutoff)
            .where(UsageRequest.route_template == route_row.route_template)
            .where(UsageRequest.method == route_row.method)
        )
        durations = sorted(r[0] for r in dur_q.all() if r[0] is not None)
        if len(durations) < 2:
            continue

        def pct(data: list[float], p: int) -> float:
            idx = min(int(len(data) * p / 100), len(data) - 1)
            return round(data[idx], 2)

        out.append(LatencyPercentilesResponse(
            route_template=route_row.route_template,
            method=route_row.method,
            sample_count=len(durations),
            p50_ms=pct(durations, 50),
            p95_ms=pct(durations, 95),
            p99_ms=pct(durations, 99),
            avg_ms=round(sum(durations) / len(durations), 2),
            max_ms=round(durations[-1], 2),
        ))
    return out
