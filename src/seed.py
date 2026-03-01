"""Seed a coherent demo dataset with moderate, believable production numbers."""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import calculate_cost
from src.entities.agent_session import AgentSession, SessionStatus
from src.entities.audit_log import AuditLog
from src.entities.contract_change import ContractChange
from src.entities.impact_set import ImpactSet
from src.entities.remediation_job import RemediationJob, JobStatus
from src.entities.team import Team
from src.entities.token_usage import TokenUsage
from src.entities.usage_request import UsageRequest

RANDOM_SEED = 20260301
DAYS_OF_HISTORY = 21

TEAMS = [
    {"id": "team_platform", "name": "Platform Engineering", "plan": "enterprise", "monthly_budget": 6000.0},
    {"id": "team_product", "name": "Product Engineering", "plan": "enterprise", "monthly_budget": 4200.0},
    {"id": "team_finance", "name": "Finance Ops", "plan": "pro", "monthly_budget": 2400.0},
    {"id": "team_support", "name": "Customer Support", "plan": "pro", "monthly_budget": 1800.0},
    {"id": "team_data", "name": "Data Science", "plan": "pro", "monthly_budget": 2200.0},
]

TEAM_PROFILES = {
    "team_platform": {
        "weekday_sessions": (4, 6),
        "weekend_sessions": (1, 2),
        "agents": ("migration-planner", "contract-reviewer", "deploy-assistant"),
        "tags": ("platform", "premium-sla", "contracts"),
        "prompts": (
            "Review downstream breakage risk for the premium SLA launch and prepare a rollout note.",
            "Validate client compatibility for session response changes before the release cut.",
            "Prepare a remediation checklist for billing, dashboard, and notification consumers.",
        ),
        "priority_weights": [1, 3, 4, 2],
        "hour_weights": [1, 1, 1, 1, 1, 1, 2, 4, 7, 8, 8, 8, 7, 7, 7, 7, 5, 3, 2, 1, 1, 1, 1, 1],
    },
    "team_product": {
        "weekday_sessions": (3, 5),
        "weekend_sessions": (1, 2),
        "agents": ("release-coordinator", "feature-analyst", "qa-triager"),
        "tags": ("product", "launch", "enterprise"),
        "prompts": (
            "Draft launch readiness updates for enterprise SLA onboarding.",
            "Review the support impact of the new SLA tier experience.",
            "Summarize open risks for this week's premium customer rollout.",
        ),
        "priority_weights": [2, 4, 3, 1],
        "hour_weights": [1, 1, 1, 1, 1, 1, 2, 3, 5, 7, 7, 8, 8, 8, 8, 7, 5, 3, 2, 1, 1, 1, 1, 1],
    },
    "team_finance": {
        "weekday_sessions": (2, 3),
        "weekend_sessions": (0, 1),
        "agents": ("invoice-orchestrator", "budget-auditor", "reconciliation-bot"),
        "tags": ("finance", "billing", "controls"),
        "prompts": (
            "Reconcile premium SLA session costs against invoice previews.",
            "Check whether the new billing fields affect downstream finance exports.",
            "Summarize any billing deltas caused by contract updates this week.",
        ),
        "priority_weights": [1, 3, 3, 1],
        "hour_weights": [1, 1, 1, 1, 1, 1, 2, 5, 7, 8, 8, 8, 7, 7, 6, 5, 3, 2, 1, 1, 1, 1, 1, 1],
    },
    "team_support": {
        "weekday_sessions": (1, 3),
        "weekend_sessions": (1, 2),
        "agents": ("ticket-triager", "incident-summarizer", "notification-worker"),
        "tags": ("support", "notifications", "ops"),
        "prompts": (
            "Draft customer-facing guidance for the premium SLA launch.",
            "Review incident notes for downstream service regressions.",
            "Prepare support handoff notes for remediation PR review.",
        ),
        "priority_weights": [2, 4, 2, 1],
        "hour_weights": [1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 7, 7, 6, 6, 5, 4, 3, 2, 1, 1, 1, 1],
    },
    "team_data": {
        "weekday_sessions": (1, 2),
        "weekend_sessions": (0, 1),
        "agents": ("usage-analyst", "forecast-bot", "report-generator"),
        "tags": ("data", "analytics", "reporting"),
        "prompts": (
            "Check launch-week token usage and adoption signals for enterprise SLA accounts.",
            "Refresh the weekly operations report with remediation and platform cost data.",
            "Compare premium-tier traffic patterns before and after the contract update.",
        ),
        "priority_weights": [2, 3, 2, 1],
        "hour_weights": [1, 1, 1, 1, 1, 1, 2, 4, 6, 7, 7, 7, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1, 1, 1],
    },
}

MODELS = [
    "devin-default",
    "devin-fast",
    "devin-reasoning",
    "claude-3-5-sonnet",
]
MODEL_WEIGHTS = [0.46, 0.26, 0.18, 0.10]

BILLING_ROUTES = [
    ("POST", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions/{session_id}"),
    ("GET", "/api/v1/sessions/stats"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/analytics/cost-by-team"),
]
BILLING_WEIGHTS = [3, 5, 2, 2, 1, 2]

DASHBOARD_ROUTES = [
    ("GET", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions/{session_id}"),
    ("GET", "/api/v1/sessions/stats"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/analytics/token-usage/daily"),
    ("GET", "/api/v1/analytics/cost-by-team"),
    ("GET", "/api/v1/usage/top-routes"),
    ("GET", "/api/v1/usage/top-callers"),
    ("GET", "/api/v1/usage/service-health"),
    ("GET", "/api/v1/contracts/changes"),
    ("GET", "/api/v1/contracts/service-graph"),
]
DASHBOARD_WEIGHTS = [4, 4, 3, 2, 2, 2, 1, 1, 1, 2, 1]

NOTIFICATION_ROUTES = [
    ("GET", "/api/v1/sessions/{session_id}"),
    ("GET", "/api/v1/sessions/stats"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/contracts/changes"),
]
NOTIFICATION_WEIGHTS = [5, 2, 1, 1]


def _session_count(profile: dict, weekday: int, day_offset: int, rng: random.Random) -> int:
    bounds = profile["weekday_sessions"] if weekday < 5 else profile["weekend_sessions"]
    count = rng.randint(*bounds)
    if day_offset <= 2 and weekday < 5:
        count += 1
    return count


def _sample_timestamp(
    now: datetime,
    day_start: datetime,
    hour_weights: list[int],
    rng: random.Random,
) -> datetime:
    ts = day_start + timedelta(
        hours=rng.choices(list(range(24)), weights=hour_weights, k=1)[0],
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )
    if ts >= now:
        return now - timedelta(minutes=rng.randint(5, 45))
    return ts


def _status_for_day(day_offset: int, rng: random.Random) -> str:
    if day_offset == 0:
        return rng.choices(
            [
                SessionStatus.RUNNING.value,
                SessionStatus.COMPLETED.value,
                SessionStatus.FAILED.value,
                SessionStatus.TIMEOUT.value,
                SessionStatus.CANCELLED.value,
            ],
            weights=[12, 70, 9, 5, 4],
            k=1,
        )[0]
    return rng.choices(
        [
            SessionStatus.COMPLETED.value,
            SessionStatus.FAILED.value,
            SessionStatus.TIMEOUT.value,
            SessionStatus.CANCELLED.value,
        ],
        weights=[82, 10, 4, 4],
        k=1,
    )[0]


def _duration_for_status(status: str, started: datetime, now: datetime, rng: random.Random) -> tuple[datetime | None, float]:
    if status == SessionStatus.RUNNING.value:
        duration = max((now - started).total_seconds(), 180)
        return None, round(duration, 2)
    if status == SessionStatus.COMPLETED.value:
        duration = rng.uniform(180, 1100)
        return started + timedelta(seconds=duration), round(duration, 2)
    if status == SessionStatus.FAILED.value:
        duration = rng.uniform(120, 780)
        return started + timedelta(seconds=duration), round(duration, 2)
    if status == SessionStatus.TIMEOUT.value:
        duration = float(rng.choice([420, 600, 900]))
        return started + timedelta(seconds=duration), duration
    duration = rng.uniform(45, 240)
    return started + timedelta(seconds=duration), round(duration, 2)


def _totals_for_session(
    team_id: str,
    model: str,
    priority: str,
    duration_seconds: float,
    rng: random.Random,
) -> tuple[int, int, int]:
    model_multiplier = {
        "devin-default": 1.0,
        "devin-fast": 0.78,
        "devin-reasoning": 1.28,
        "claude-3-5-sonnet": 1.08,
    }[model]
    priority_multiplier = {
        "low": 0.85,
        "medium": 1.0,
        "high": 1.18,
        "critical": 1.35,
    }[priority]
    team_multiplier = {
        "team_platform": 1.22,
        "team_product": 1.0,
        "team_finance": 0.92,
        "team_support": 0.88,
        "team_data": 1.08,
    }[team_id]
    duration_factor = min(max(duration_seconds, 180) / 420, 2.2)

    base_input = rng.randint(700, 2400)
    base_output = rng.randint(220, 1100)
    gross_input = int(base_input * model_multiplier * priority_multiplier * team_multiplier * duration_factor)
    output_tokens = int(base_output * model_multiplier * priority_multiplier * duration_factor)
    cached_tokens = int(gross_input * rng.uniform(0.12, 0.28))
    input_tokens = max(gross_input - cached_tokens, 0)
    return input_tokens, output_tokens, cached_tokens


def _split_total(total: int, parts: int, rng: random.Random) -> list[int]:
    if parts <= 1:
        return [total]
    weights = [rng.uniform(0.8, 1.25) for _ in range(parts)]
    total_weight = sum(weights)
    chunks = [int(total * weight / total_weight) for weight in weights]
    chunks[-1] += total - sum(chunks)
    return chunks


def _error_message(status: str, team_id: str, rng: random.Random) -> str | None:
    if status == SessionStatus.FAILED.value:
        messages = {
            "team_platform": [
                "Schema adapter failed while mapping the new sla_tier field",
                "Downstream client tests broke on usage.cache_read_tokens rename",
            ],
            "team_finance": [
                "Billing summary export still expected billing.total instead of billing.total_usd",
                "Invoice reconciliation job hit a contract validation error",
            ],
            "team_support": [
                "Notification delivery worker could not hydrate session detail payload",
                "Escalation template render failed for a premium SLA incident summary",
            ],
        }
        return rng.choice(messages.get(team_id, [
            "Unexpected contract validation error during launch-week workflow",
            "Upstream session response shape did not match cached client expectations",
        ]))
    if status == SessionStatus.TIMEOUT.value:
        return "Long-running workflow timed out while waiting for downstream service confirmation"
    return None


async def seed_data(db: AsyncSession):
    """Seed the database with a believable demo dataset."""
    rng = random.Random(RANDOM_SEED)
    now = datetime.now(timezone.utc)

    for index, team_data in enumerate(TEAMS):
        created_at = now - timedelta(days=90 - index * 9)
        db.add(Team(created_at=created_at, **team_data))
    await db.flush()

    for day_offset in range(DAYS_OF_HISTORY - 1, -1, -1):
        day_start = (now - timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        weekday = day_start.weekday()

        for team_data in TEAMS:
            profile = TEAM_PROFILES[team_data["id"]]
            for _ in range(_session_count(profile, weekday, day_offset, rng)):
                started = _sample_timestamp(now, day_start, profile["hour_weights"], rng)
                status = _status_for_day(day_offset, rng)
                ended_at, duration_seconds = _duration_for_status(status, started, now, rng)
                priority = rng.choices(["low", "medium", "high", "critical"], weights=profile["priority_weights"], k=1)[0]
                model = rng.choices(MODELS, weights=MODEL_WEIGHTS, k=1)[0]
                input_tokens, output_tokens, cached_tokens = _totals_for_session(
                    team_data["id"], model, priority, duration_seconds, rng
                )
                total_cost = calculate_cost(input_tokens, output_tokens, cached_tokens)

                session_id = f"sess_{uuid.uuid4().hex[:16]}"
                session = AgentSession(
                    session_id=session_id,
                    team_id=team_data["id"],
                    agent_name=rng.choice(profile["agents"]),
                    model=model,
                    status=status,
                    priority=priority,
                    prompt=rng.choice(profile["prompts"]),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    total_cost=total_cost,
                    started_at=started,
                    ended_at=ended_at,
                    duration_seconds=duration_seconds,
                    error_message=_error_message(status, team_data["id"], rng),
                    tags=",".join(profile["tags"]),
                )
                db.add(session)

                event_count = rng.randint(2, 4)
                input_chunks = _split_total(input_tokens, event_count, rng)
                output_chunks = _split_total(output_tokens, event_count, rng)
                cached_chunks = _split_total(cached_tokens, event_count, rng)
                for index in range(event_count):
                    event_offset = duration_seconds * (index + 1) / (event_count + 1)
                    db.add(TokenUsage(
                        session_id=session_id,
                        timestamp=started + timedelta(seconds=event_offset),
                        model=model,
                        input_tokens=input_chunks[index],
                        output_tokens=output_chunks[index],
                        cached_tokens=cached_chunks[index],
                        cost=calculate_cost(
                            input_chunks[index],
                            output_chunks[index],
                            cached_chunks[index],
                        ),
                    ))

    await db.commit()
    await seed_usage_requests(db, rng)
    await seed_contract_change_demo(db)


async def seed_usage_requests(db: AsyncSession, rng: random.Random):
    """Seed small but believable cross-service telemetry."""
    now = datetime.now(timezone.utc)

    async def add_requests(
        *,
        caller_service: str,
        routes: list[tuple[str, str]],
        weights: list[int],
        count: int,
        day_start: datetime,
        hour_weights: list[int],
        status_population: list[int],
        status_weights: list[int],
        latency_range: tuple[float, float],
    ) -> None:
        for _ in range(count):
            method, route = rng.choices(routes, weights=weights, k=1)[0]
            ts = _sample_timestamp(now, day_start, hour_weights, rng)
            status_code = rng.choices(status_population, weights=status_weights, k=1)[0]
            db.add(UsageRequest(
                ts=ts,
                caller_service=caller_service,
                method=method,
                route_template=route,
                status_code=status_code,
                duration_ms=round(rng.uniform(*latency_range), 2),
            ))

    for day_offset in range(DAYS_OF_HISTORY - 1, -1, -1):
        day_start = (now - timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        weekday = day_start.weekday()

        billing_count = rng.randint(12, 18) if weekday < 5 else rng.randint(6, 9)
        dashboard_count = rng.randint(18, 26) if weekday < 5 else rng.randint(9, 13)
        notification_count = rng.randint(6, 10) if weekday < 5 else rng.randint(3, 6)

        if day_offset <= 2 and weekday < 5:
            billing_count += 2
            dashboard_count += 3
            notification_count += 1

        await add_requests(
            caller_service="billing-service",
            routes=BILLING_ROUTES,
            weights=BILLING_WEIGHTS,
            count=billing_count,
            day_start=day_start,
            hour_weights=[1, 1, 1, 1, 1, 1, 2, 4, 6, 7, 8, 8, 7, 7, 6, 5, 3, 2, 1, 1, 1, 1, 1, 1],
            status_population=[200, 201, 400, 409, 500],
            status_weights=[76, 14, 5, 3, 2],
            latency_range=(28, 140),
        )
        await add_requests(
            caller_service="dashboard-service",
            routes=DASHBOARD_ROUTES,
            weights=DASHBOARD_WEIGHTS,
            count=dashboard_count,
            day_start=day_start,
            hour_weights=[1, 1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8, 8, 7, 6, 4, 3, 2, 1, 1, 1, 1, 1],
            status_population=[200, 304, 404, 429, 502],
            status_weights=[85, 7, 4, 2, 2],
            latency_range=(18, 120),
        )
        await add_requests(
            caller_service="notification-service",
            routes=NOTIFICATION_ROUTES,
            weights=NOTIFICATION_WEIGHTS,
            count=notification_count,
            day_start=day_start,
            hour_weights=[1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 6, 6, 6, 5, 5, 4, 3, 2, 1, 1, 1, 1, 1],
            status_population=[200, 202, 404, 500],
            status_weights=[87, 8, 3, 2],
            latency_range=(24, 135),
        )

    await db.commit()


async def seed_contract_change_demo(db: AsyncSession):
    """Seed a moderate-scale enterprise incident around a premium SLA launch."""
    now = datetime.now(timezone.utc)

    change = ContractChange(
        base_ref="accr_2026_02_27",
        head_ref="accr_2026_03_01",
        created_at=now - timedelta(hours=2, minutes=10),
        is_breaking=True,
        severity="high",
        summary_json=json.dumps({
            "summary": "Premium enterprise SLA launch made sla_tier required on session creation and "
                       "standardized billing.total_usd plus usage.cache_read_tokens in session responses. "
                       "Billing, dashboard, and notification-service needed coordinated client fixes."
        }),
        changed_routes_json=json.dumps([
            "POST /api/v1/sessions",
            "GET /api/v1/sessions",
            "GET /api/v1/sessions/{session_id}",
        ]),
        changed_fields_json=json.dumps([
            {"route": "POST /api/v1/sessions", "field": "sla_tier", "change": "added (required)"},
            {"route": "GET /api/v1/sessions", "field": "billing.total_usd", "change": "renamed from billing.total"},
            {"route": "GET /api/v1/sessions/{session_id}", "field": "usage.cache_read_tokens", "change": "renamed from usage.cached_tokens"},
        ]),
    )
    db.add(change)
    await db.flush()

    impacts = [
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions",
            method="POST",
            caller_service="billing-service",
            calls_last_7d=28,
            confidence="high",
            notes="Invoice backfill and reconciliation jobs create sessions with budget controls",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions",
            method="GET",
            caller_service="billing-service",
            calls_last_7d=44,
            confidence="high",
            notes="Finance rollups and invoice review pages read normalized billing fields",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="GET",
            caller_service="dashboard-service",
            calls_last_7d=78,
            confidence="high",
            notes="Session drill-down cards and remediation detail views use the full response shape",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="GET",
            caller_service="notification-service",
            calls_last_7d=31,
            confidence="high",
            notes="Recovery reports enrich Slack and Jira updates with current session details",
        ),
    ]
    for impact in impacts:
        db.add(impact)
    await db.flush()

    jobs_spec = [
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/billing-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_billing_sla_012",
            "pr_url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/12",
            "created_at": now - timedelta(hours=1, minutes=24),
            "updated_at": now - timedelta(minutes=18),
            "bundle_hash": "billing-sla-012",
            "error_summary": None,
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/dashboard-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_dashboard_sla_009",
            "pr_url": "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/9",
            "created_at": now - timedelta(hours=1, minutes=17),
            "updated_at": now - timedelta(minutes=11),
            "bundle_hash": "dashboard-sla-009",
            "error_summary": None,
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/notification-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_notification_sla_005",
            "pr_url": "https://github.com/MadhuvanthiSriPad/notification-service/pull/5",
            "created_at": now - timedelta(hours=1, minutes=8),
            "updated_at": now - timedelta(minutes=6),
            "bundle_hash": "notification-sla-005",
            "error_summary": None,
        },
    ]

    jobs: list[RemediationJob] = []
    for spec in jobs_spec:
        job = RemediationJob(change_id=change.id, is_dry_run=False, **spec)
        db.add(job)
        await db.flush()
        jobs.append(job)

    audit_entries = [
        AuditLog(
            job_id=jobs[0].job_id,
            old_status=None,
            new_status="queued",
            changed_at=jobs[0].created_at,
            detail="Wave 0 queued after contract diff classified as breaking",
        ),
        AuditLog(
            job_id=jobs[0].job_id,
            old_status="queued",
            new_status="running",
            changed_at=jobs[0].created_at + timedelta(minutes=2),
            detail="api-core Devin sync detected scoped billing fix and dispatched session",
        ),
        AuditLog(
            job_id=jobs[0].job_id,
            old_status="running",
            new_status="pr_opened",
            changed_at=jobs[0].created_at + timedelta(minutes=24),
            detail=f"Billing adapter and tests updated in {jobs[0].pr_url}",
        ),
        AuditLog(
            job_id=jobs[0].job_id,
            old_status="pr_opened",
            new_status="green",
            changed_at=jobs[0].created_at + timedelta(minutes=49),
            detail="CI passed and finance reviewer approved the billing field migration",
        ),
        AuditLog(
            job_id=jobs[1].job_id,
            old_status=None,
            new_status="queued",
            changed_at=jobs[1].created_at,
            detail="Wave 1 queued after billing patch entered review",
        ),
        AuditLog(
            job_id=jobs[1].job_id,
            old_status="queued",
            new_status="running",
            changed_at=jobs[1].created_at + timedelta(minutes=1),
            detail="Dashboard BFF patch started with updated session detail contract",
        ),
        AuditLog(
            job_id=jobs[1].job_id,
            old_status="running",
            new_status="pr_opened",
            changed_at=jobs[1].created_at + timedelta(minutes=21),
            detail=f"Dashboard PR opened with detail-view mapping fix: {jobs[1].pr_url}",
        ),
        AuditLog(
            job_id=jobs[1].job_id,
            old_status="pr_opened",
            new_status="green",
            changed_at=jobs[1].created_at + timedelta(minutes=42),
            detail="UI smoke tests passed and overview card totals matched billing summary",
        ),
        AuditLog(
            job_id=jobs[2].job_id,
            old_status=None,
            new_status="queued",
            changed_at=jobs[2].created_at,
            detail="Wave 1 queued for notification-service after dashboard contract stabilized",
        ),
        AuditLog(
            job_id=jobs[2].job_id,
            old_status="queued",
            new_status="running",
            changed_at=jobs[2].created_at + timedelta(minutes=2),
            detail="Recovery report renderer updated for usage.cache_read_tokens alias removal",
        ),
        AuditLog(
            job_id=jobs[2].job_id,
            old_status="running",
            new_status="pr_opened",
            changed_at=jobs[2].created_at + timedelta(minutes=16),
            detail=f"Notification PR opened and previewed successfully: {jobs[2].pr_url}",
        ),
        AuditLog(
            job_id=jobs[2].job_id,
            old_status="pr_opened",
            new_status="green",
            changed_at=jobs[2].created_at + timedelta(minutes=33),
            detail="Webhook replay and recovery report preview both passed",
        ),
    ]

    for entry in audit_entries:
        db.add(entry)

    await db.commit()
