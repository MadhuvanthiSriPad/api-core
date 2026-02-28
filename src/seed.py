"""Seed realistic agent session data for demo."""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.entities.team import Team
from src.entities.agent_session import AgentSession, SessionStatus
from src.entities.token_usage import TokenUsage
from src.entities.usage_request import UsageRequest
from src.entities.contract_change import ContractChange
from src.entities.impact_set import ImpactSet
from src.entities.remediation_job import RemediationJob, JobStatus
from src.entities.audit_log import AuditLog
from src.config import settings, calculate_cost

TEAMS = [
    {"id": "team_platform", "name": "Platform Engineering", "plan": "enterprise", "monthly_budget": 12000.0},
    {"id": "team_product", "name": "Product Engineering", "plan": "enterprise", "monthly_budget": 9500.0},
    {"id": "team_data", "name": "Data Science", "plan": "pro", "monthly_budget": 5200.0},
    {"id": "team_finance", "name": "Finance Ops", "plan": "pro", "monthly_budget": 3400.0},
    {"id": "team_support", "name": "Customer Support", "plan": "pro", "monthly_budget": 2600.0},
    {"id": "team_growth", "name": "Growth", "plan": "starter", "monthly_budget": 1400.0},
]
TEAM_WEIGHTS = [0.24, 0.22, 0.16, 0.14, 0.14, 0.10]

AGENTS = [
    "code-reviewer", "bug-fixer", "test-writer", "doc-generator",
    "data-pipeline", "sql-optimizer", "log-analyzer", "ticket-triager",
    "content-writer", "email-drafter", "deploy-assistant", "migration-planner",
]

MODELS = [
    "devin-default",
    "devin-fast",
    "devin-reasoning",
    "claude-3-5-sonnet",
]

MODEL_WEIGHTS = [0.42, 0.24, 0.18, 0.16]
DAYS_OF_HISTORY = 28


async def seed_data(db: AsyncSession):
    """Seed the database with realistic demo data."""
    # Create teams
    for team_data in TEAMS:
        team = Team(**team_data)
        db.add(team)
    await db.flush()

    now = datetime.now(timezone.utc)

    # Generate a month of production-like usage with weekday spikes.
    for day_offset in range(DAYS_OF_HISTORY - 1, -1, -1):
        day_start = now - timedelta(days=day_offset)
        weekday = day_start.weekday()
        if weekday < 5:
            sessions_per_day = random.randint(55, 120)
        else:
            sessions_per_day = random.randint(22, 58)
        if day_offset < 3:
            sessions_per_day += random.randint(12, 30)

        for _ in range(sessions_per_day):
            team = random.choices(TEAMS, weights=TEAM_WEIGHTS, k=1)[0]
            agent = random.choice(AGENTS)
            model = random.choices(MODELS, weights=MODEL_WEIGHTS, k=1)[0]

            started = day_start + timedelta(
                hours=random.choices(
                    population=list(range(24)),
                    weights=[1, 1, 1, 1, 1, 1, 2, 4, 7, 8, 8, 8, 7, 8, 8, 8, 7, 6, 4, 3, 2, 1, 1, 1],
                    k=1,
                )[0],
                minutes=random.randint(0, 59),
            )
            # Clamp to the past so durations are never negative
            if started > now:
                started = now - timedelta(minutes=random.randint(1, 60))

            # Determine status with realistic distribution
            status_roll = random.random()
            if day_offset == 0 and status_roll < 0.15:
                status = SessionStatus.RUNNING.value
                ended = None
                duration = max((now - started).total_seconds(), 10)
            elif status_roll < 0.80:
                status = SessionStatus.COMPLETED.value
                duration = random.uniform(20, 900)
                ended = started + timedelta(seconds=duration)
            elif status_roll < 0.93:
                status = SessionStatus.FAILED.value
                duration = random.uniform(15, 240)
                ended = started + timedelta(seconds=duration)
            elif status_roll < 0.97:
                status = SessionStatus.TIMEOUT.value
                duration = random.choice([300, 600, 900])
                ended = started + timedelta(seconds=duration)
            else:
                status = SessionStatus.CANCELLED.value
                duration = random.uniform(2, 30)
                ended = started + timedelta(seconds=duration)

            # Token counts scale with duration (capped to reasonable range)
            capped_duration = min(duration, 1200)
            model_multiplier = {
                "devin-default": 1.0,
                "devin-fast": 0.72,
                "devin-reasoning": 1.45,
                "claude-3-5-sonnet": 1.18,
            }.get(model, 1.0)
            base_input = int(random.uniform(1200, 18000) * (capped_duration / 90) * model_multiplier)
            base_output = int(random.uniform(500, 11000) * (capped_duration / 90) * model_multiplier)
            cached = int(base_input * random.uniform(0.08, 0.38))
            input_tokens = max(base_input - cached, 0)
            output_tokens = max(base_output, 0)
            cost = calculate_cost(input_tokens, output_tokens, cached)

            session_id = f"sess_{uuid.uuid4().hex[:16]}"
            error_msg = None
            if status == SessionStatus.FAILED.value:
                error_msg = random.choice([
                    "Rate limit exceeded",
                    "Context window overflow",
                    "Tool execution failed: git push rejected",
                    "API timeout after 120s",
                    "Invalid tool response format",
                ])

            priority = random.choice(["low", "medium", "high", "critical"])

            session = AgentSession(
                session_id=session_id,
                team_id=team["id"],
                agent_name=agent,
                model=model,
                status=status,
                priority=priority,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                total_cost=cost,
                started_at=started,
                ended_at=ended,
                duration_seconds=round(duration, 2),
                error_message=error_msg,
                tags=f"{agent},{team['id']}",
            )
            db.add(session)

            # Add 2-5 token usage events per session
            num_events = random.randint(3, 7)
            event_time = started
            for i in range(num_events):
                evt_input = input_tokens // num_events + random.randint(-100, 100)
                evt_output = output_tokens // num_events + random.randint(-50, 50)
                evt_cached = cached // num_events
                evt_cost = calculate_cost(max(evt_input, 0), max(evt_output, 0), max(evt_cached, 0))

                event = TokenUsage(
                    session_id=session_id,
                    timestamp=event_time,
                    model=model,
                    input_tokens=max(evt_input, 0),
                    output_tokens=max(evt_output, 0),
                    cached_tokens=max(evt_cached, 0),
                    cost=evt_cost,
                )
                db.add(event)
                event_time += timedelta(seconds=max(duration, 10) / num_events)

    await db.commit()

    # Seed usage_requests telemetry data
    await seed_usage_requests(db)

    # Seed contract change demo (complete causal chain)
    await seed_contract_change_demo(db)


# Routes that billing-service and dashboard-service call
BILLING_ROUTES = [
    ("POST", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions/{session_id}"),
    ("GET", "/api/v1/sessions/stats"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/analytics/cost-by-team"),
]

DASHBOARD_ROUTES = [
    ("GET", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions/stats"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/analytics/token-usage/daily"),
    ("GET", "/api/v1/analytics/cost-by-team"),
    ("GET", "/api/v1/usage/top-routes"),
    ("GET", "/api/v1/usage/top-callers"),
    ("GET", "/api/v1/usage/service-health"),
    ("GET", "/api/v1/usage/error-rates"),
    ("GET", "/api/v1/usage/latency-percentiles"),
    ("GET", "/api/v1/contracts/changes"),
    ("GET", "/api/v1/contracts/service-graph"),
]

# Weight heavier routes (called more frequently)
BILLING_WEIGHTS = [6, 4, 3, 2, 2, 3]
DASHBOARD_WEIGHTS = [5, 3, 2, 4, 3, 2, 2, 2, 2, 2, 2, 1]

NOTIFICATION_ROUTES = [
    ("GET", "/api/v1/sessions/{session_id}"),
    ("GET", "/api/v1/sessions/stats"),
    ("GET", "/api/v1/teams"),
    ("GET", "/api/v1/analytics/token-usage/daily"),
    ("GET", "/api/v1/contracts/changes"),
]
NOTIFICATION_WEIGHTS = [5, 3, 2, 2, 1]


async def seed_usage_requests(db: AsyncSession):
    """Seed production-like usage telemetry from both consumer services."""
    now = datetime.now(timezone.utc)

    def event_timestamp(day_start: datetime, hour_weights: list[int]) -> datetime:
        ts = day_start + timedelta(
            hours=random.choices(population=list(range(24)), weights=hour_weights, k=1)[0],
            minutes=random.randint(0, 59),
            seconds=random.randint(0, 59),
        )
        if ts > now:
            return now - timedelta(seconds=random.randint(1, 3600))
        return ts

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
        post_status_population: list[int] | None = None,
        post_status_weights: list[int] | None = None,
    ) -> None:
        for _ in range(count):
            method, route = random.choices(routes, weights=weights, k=1)[0]
            status_code = random.choices(status_population, weights=status_weights, k=1)[0]
            if method == "POST" and post_status_population and post_status_weights:
                status_code = random.choices(post_status_population, weights=post_status_weights, k=1)[0]
            if method == "PATCH":
                status_code = random.choices([200, 202, 400, 409, 500], weights=[80, 10, 4, 4, 2], k=1)[0]

            db.add(UsageRequest(
                ts=event_timestamp(day_start, hour_weights),
                caller_service=caller_service,
                method=method,
                route_template=route,
                status_code=status_code,
                duration_ms=round(random.uniform(*latency_range), 2),
            ))

    for day_offset in range(DAYS_OF_HISTORY - 1, -1, -1):
        day_start = now - timedelta(days=day_offset)
        weekday = day_start.weekday()

        # billing-service: steady synchronous API traffic with invoice spikes.
        num_billing = random.randint(120, 220) if weekday < 5 else random.randint(55, 110)
        await add_requests(
            caller_service="billing-service",
            routes=BILLING_ROUTES,
            weights=BILLING_WEIGHTS,
            count=num_billing,
            day_start=day_start,
            hour_weights=[1, 1, 1, 1, 1, 1, 2, 4, 7, 8, 9, 9, 8, 9, 8, 8, 6, 4, 2, 1, 1, 1, 1, 1],
            status_population=[200, 201, 400, 404, 500],
            status_weights=[77, 15, 4, 2, 2],
            post_status_population=[201, 400, 409, 500],
            post_status_weights=[88, 6, 3, 3],
            latency_range=(18, 260),
        )

        # dashboard-service: high-frequency polling plus interactive exploration.
        num_dashboard = random.randint(260, 420) if weekday < 5 else random.randint(120, 220)
        await add_requests(
            caller_service="dashboard-service",
            routes=DASHBOARD_ROUTES,
            weights=DASHBOARD_WEIGHTS,
            count=num_dashboard,
            day_start=day_start,
            hour_weights=[1, 1, 1, 1, 1, 1, 2, 3, 6, 8, 9, 9, 9, 9, 9, 9, 7, 5, 3, 2, 1, 1, 1, 1],
            status_population=[200, 304, 404, 429, 502],
            status_weights=[84, 8, 4, 2, 2],
            latency_range=(8, 210),
        )

        num_notification = random.randint(48, 102) if weekday < 5 else random.randint(18, 42)
        await add_requests(
            caller_service="notification-service",
            routes=NOTIFICATION_ROUTES,
            weights=NOTIFICATION_WEIGHTS,
            count=num_notification,
            day_start=day_start,
            hour_weights=[1, 1, 1, 1, 1, 1, 2, 3, 5, 6, 7, 8, 8, 8, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1],
            status_population=[200, 202, 400, 404, 500],
            status_weights=[84, 6, 4, 4, 2],
            latency_range=(14, 220),
        )

    await db.commit()


async def seed_contract_change_demo(db: AsyncSession):
    """Seed a complete contract change with impacts, jobs, and audit trail.

    Provides a full causal chain for the dashboard on first boot:
    breaking change → blast radius → verified remediation across repos.
    """
    now = datetime.now(timezone.utc)

    # 1. Contract change
    change = ContractChange(
        base_ref="a1b2c3d4e5f6a7b8",
        head_ref="z9y8x7w6v5u4t3s2",
        created_at=now - timedelta(hours=2),
        is_breaking=True,
        severity="high",
        summary_json=json.dumps({
            "summary": "Fix breaking session contract update across billing-service, "
                       "dashboard-service, and notification-service after api-core made "
                       "budget caps required and standardized usage + billing response fields."
        }),
        changed_routes_json=json.dumps([
            "POST /api/v1/sessions",
            "GET /api/v1/sessions",
            "GET /api/v1/sessions/{session_id}",
            "PATCH /api/v1/sessions/{session_id}",
        ]),
        changed_fields_json=json.dumps([
            {"route": "POST /api/v1/sessions", "field": "max_cost_usd", "change": "added (required)"},
            {"route": "GET /api/v1/sessions", "field": "usage.cache_read_tokens", "change": "renamed from usage.cached_tokens"},
            {"route": "GET /api/v1/sessions", "field": "billing.total_usd", "change": "renamed from billing.total"},
        ]),
    )
    db.add(change)
    await db.flush()

    # 2. Impact sets (route_template uses path-only format to match middleware/pipeline)
    impacts = [
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions",
            method="GET",
            caller_service="billing-service",
            calls_last_7d=1842,
            confidence="high",
            notes="Invoice generation and finance reconciliation pipelines",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions",
            method="POST",
            caller_service="billing-service",
            calls_last_7d=1268,
            confidence="high",
            notes="Bulk session creation from invoice backfills",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="GET",
            caller_service="dashboard-service",
            calls_last_7d=4216,
            confidence="high",
            notes="UI drill-down, job detail flyouts, and audit views",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="PATCH",
            caller_service="dashboard-service",
            calls_last_7d=968,
            confidence="medium",
            notes="Control-plane updates triggered from operator workflows",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="GET",
            caller_service="notification-service",
            calls_last_7d=1584,
            confidence="high",
            notes="Delivery workers hydrate notification payloads from session detail reads",
        ),
    ]
    for imp in impacts:
        db.add(imp)
    await db.flush()

    # 3. Remediation jobs — one current row per impacted repo.
    jobs_spec = [
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/billing-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_billing_green_043",
            "pr_url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/43",
            "created_at": now - timedelta(hours=1, minutes=32),
            "updated_at": now - timedelta(minutes=18),
            "bundle_hash": "billing-green-043",
            "error_summary": None,
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/dashboard-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_dashboard_green_017",
            "pr_url": "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/17",
            "created_at": now - timedelta(hours=1, minutes=28),
            "updated_at": now - timedelta(minutes=11),
            "bundle_hash": "dashboard-green-017",
            "error_summary": None,
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/notification-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_notification_green_008",
            "pr_url": "https://github.com/MadhuvanthiSriPad/notification-service/pull/8",
            "created_at": now - timedelta(hours=1, minutes=20),
            "updated_at": now - timedelta(minutes=7),
            "bundle_hash": "notification-green-008",
            "error_summary": None,
        },
    ]

    jobs = []
    for spec in jobs_spec:
        job = RemediationJob(change_id=change.id, is_dry_run=False, **spec)
        db.add(job)
        await db.flush()
        jobs.append(job)

    # 4. Audit log entries — full transition history per job
    audit = []

    # Job 0: billing → GREEN (queued→running→pr_opened→green)
    j, t = jobs[0], jobs[0].created_at
    audit += [
        AuditLog(job_id=j.job_id, old_status=None, new_status="queued",
                 changed_at=t, detail="Job created"),
        AuditLog(job_id=j.job_id, old_status="queued", new_status="running",
                 changed_at=t + timedelta(seconds=5), detail="Dispatching to Devin"),
        AuditLog(job_id=j.job_id, old_status="running", new_status="pr_opened",
                 changed_at=t + timedelta(minutes=18), detail=f"PR: {j.pr_url}"),
        AuditLog(job_id=j.job_id, old_status="pr_opened", new_status="green",
                 changed_at=t + timedelta(minutes=52), detail="CI passed, approved for merge"),
    ]

    # Job 1: dashboard → GREEN (queued→running→pr_opened→green)
    j, t = jobs[1], jobs[1].created_at
    audit += [
        AuditLog(job_id=j.job_id, old_status=None, new_status="queued",
                 changed_at=t, detail="Job created"),
        AuditLog(job_id=j.job_id, old_status="queued", new_status="running",
                 changed_at=t + timedelta(seconds=8), detail="Dispatching to Devin"),
        AuditLog(job_id=j.job_id, old_status="running", new_status="pr_opened",
                 changed_at=t + timedelta(minutes=25), detail=f"PR: {j.pr_url}"),
        AuditLog(job_id=j.job_id, old_status="pr_opened", new_status="green",
                 changed_at=t + timedelta(minutes=47), detail="CI passed, design review approved"),
    ]

    # Job 2: notification → GREEN (queued→running→pr_opened→green)
    j, t = jobs[2], jobs[2].created_at
    audit += [
        AuditLog(job_id=j.job_id, old_status=None, new_status="queued",
                 changed_at=t, detail="Job created"),
        AuditLog(job_id=j.job_id, old_status="queued", new_status="running",
                 changed_at=t + timedelta(seconds=11), detail="Dispatching to Devin"),
        AuditLog(job_id=j.job_id, old_status="running", new_status="pr_opened",
                 changed_at=t + timedelta(minutes=19), detail=f"PR: {j.pr_url}"),
        AuditLog(job_id=j.job_id, old_status="pr_opened", new_status="green",
                 changed_at=t + timedelta(minutes=38), detail="CI passed, notification preview validated"),
    ]

    for entry in audit:
        db.add(entry)

    await db.commit()
