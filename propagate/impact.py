"""Impact mapping — determine affected services from service map and usage telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.entities.usage_request import UsageRequest


@dataclass
class ImpactRecord:
    caller_service: str
    route_template: str
    method: str
    calls_last_7d: int
    declared_only: bool = False


async def compute_impact_sets(
    db: AsyncSession,
    changed_routes: list[str],
    declared_dependents: set[str] | None = None,
) -> list[ImpactRecord]:
    """Return impacted services for each changed route.

    Primary source: declared_dependents — any service explicitly listed here
    is always included, regardless of call history.  This is the service map
    truth: if billing-service declares depends_on api-core, it is impacted by
    any api-core contract change.

    Enrichment: usage telemetry from the last 7 days provides call counts and
    can surface additional callers not in the service map.

    changed_routes: list of strings like "POST /api/v1/sessions"
    declared_dependents: set of service names from the service map
    """
    if not changed_routes:
        return []

    route_filters: list[tuple[str, str]] = []
    for route_str in changed_routes:
        parts = route_str.split(" ", 1)
        if len(parts) == 2:
            route_filters.append((parts[0].upper(), parts[1]))

    if not route_filters:
        return []

    # Collect telemetry call counts: (service, method, route) -> count
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    telemetry: dict[tuple[str, str, str], int] = {}
    telemetry_services: set[str] = set()

    for method, route_template in route_filters:
        result = await db.execute(
            select(
                UsageRequest.caller_service,
                UsageRequest.route_template,
                UsageRequest.method,
                func.count(UsageRequest.id).label("call_count"),
            )
            .where(
                UsageRequest.ts >= cutoff,
                UsageRequest.method == method,
                UsageRequest.route_template == route_template,
                UsageRequest.caller_service != "unknown",
            )
            .group_by(UsageRequest.caller_service, UsageRequest.route_template, UsageRequest.method)
        )
        for row in result.all():
            telemetry[(row.caller_service, row.method, row.route_template)] = row.call_count
            telemetry_services.add(row.caller_service)

    # Union of declared dependents and any telemetry callers
    all_services = set(declared_dependents or set()) | telemetry_services

    impacts: list[ImpactRecord] = []
    seen_services: set[str] = set()

    # Include all (service, route) combos with actual telemetry
    for (svc, method, route_template), count in sorted(telemetry.items()):
        impacts.append(ImpactRecord(
            caller_service=svc,
            route_template=route_template,
            method=method,
            calls_last_7d=count,
        ))
        seen_services.add(svc)

    # Ensure every declared dependent appears at least once
    for svc in sorted((declared_dependents or set()) - seen_services):
        method, route_template = route_filters[0]
        impacts.append(ImpactRecord(
            caller_service=svc,
            route_template=route_template,
            method=method,
            calls_last_7d=0,
            declared_only=True,
        ))

    return impacts
