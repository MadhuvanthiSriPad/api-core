"""Impact mapping — query usage telemetry to find affected services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.usage_request import UsageRequest


@dataclass
class ImpactRecord:
    caller_service: str
    route_template: str
    calls_last_7d: int


async def compute_impact_sets(
    db: AsyncSession,
    changed_routes: list[str],
) -> list[ImpactRecord]:
    """Query usage_requests for the last 7 days to find impacted callers.

    changed_routes: list of strings like "POST /api/v1/sessions"
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    # Parse changed routes into (method, path) tuples
    route_filters = []
    for route_str in changed_routes:
        parts = route_str.split(" ", 1)
        if len(parts) == 2:
            route_filters.append((parts[0], parts[1]))

    if not route_filters:
        return []

    impacts: list[ImpactRecord] = []

    for method, route_template in route_filters:
        result = await db.execute(
            select(
                UsageRequest.caller_service,
                UsageRequest.route_template,
                func.count(UsageRequest.id).label("call_count"),
            )
            .where(
                UsageRequest.ts >= cutoff,
                UsageRequest.method == method,
                UsageRequest.route_template == route_template,
                UsageRequest.caller_service != "unknown",
            )
            .group_by(UsageRequest.caller_service, UsageRequest.route_template)
        )

        for row in result.all():
            impacts.append(ImpactRecord(
                caller_service=row.caller_service,
                route_template=row.route_template,
                calls_last_7d=row.call_count,
            ))

    return impacts
