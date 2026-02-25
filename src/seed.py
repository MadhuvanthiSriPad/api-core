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
    {"id": "team_eng", "name": "Engineering", "plan": "enterprise", "monthly_budget": 5000.0},
    {"id": "team_data", "name": "Data Science", "plan": "pro", "monthly_budget": 3000.0},
    {"id": "team_support", "name": "Customer Support", "plan": "pro", "monthly_budget": 2000.0},
    {"id": "team_marketing", "name": "Marketing", "plan": "free", "monthly_budget": 500.0},
]

AGENTS = [
    "code-reviewer", "bug-fixer", "test-writer", "doc-generator",
    "data-pipeline", "sql-optimizer", "log-analyzer", "ticket-triager",
    "content-writer", "email-drafter", "deploy-assistant", "migration-planner",
]

MODELS = [
    "devin-default",
    "devin-fast",
    "devin-reasoning",
]

MODEL_WEIGHTS = [0.6, 0.3, 0.1]


async def seed_data(db: AsyncSession):
    """Seed the database with realistic demo data."""
    # Create teams
    for team_data in TEAMS:
        team = Team(**team_data)
        db.add(team)
    await db.flush()

    now = datetime.now(timezone.utc)

    # Generate sessions over the last 7 days
    for day_offset in range(7, -1, -1):
        day_start = now - timedelta(days=day_offset)
        sessions_per_day = random.randint(15, 40)

        for _ in range(sessions_per_day):
            team = random.choice(TEAMS)
            agent = random.choice(AGENTS)
            model = random.choices(MODELS, weights=MODEL_WEIGHTS, k=1)[0]

            started = day_start + timedelta(
                hours=random.randint(0, 23),
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
            elif status_roll < 0.78:
                status = SessionStatus.COMPLETED.value
                duration = random.uniform(10, 600)
                ended = started + timedelta(seconds=duration)
            elif status_roll < 0.92:
                status = SessionStatus.FAILED.value
                duration = random.uniform(5, 120)
                ended = started + timedelta(seconds=duration)
            elif status_roll < 0.97:
                status = SessionStatus.TIMEOUT.value
                duration = 300
                ended = started + timedelta(seconds=duration)
            else:
                status = SessionStatus.CANCELLED.value
                duration = random.uniform(2, 30)
                ended = started + timedelta(seconds=duration)

            # Token counts scale with duration (capped to reasonable range)
            capped_duration = min(duration, 600)  # cap at 10 minutes for token calc
            base_input = int(random.uniform(500, 15000) * (capped_duration / 60))
            base_output = int(random.uniform(200, 8000) * (capped_duration / 60))
            cached = int(base_input * random.uniform(0, 0.4))
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
            num_events = random.randint(2, 5)
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
    ("GET", "/api/v1/contracts/changes"),
]

# Weight heavier routes (called more frequently)
BILLING_WEIGHTS = [5, 3, 2, 1, 2]
DASHBOARD_WEIGHTS = [4, 3, 2, 3, 2, 2, 1, 1]


async def seed_usage_requests(db: AsyncSession):
    """Seed 7 days of realistic usage telemetry from both consumer services."""
    now = datetime.now(timezone.utc)

    for day_offset in range(7, -1, -1):
        day_start = now - timedelta(days=day_offset)

        # billing-service: 40-80 calls per day
        num_billing = random.randint(40, 80)
        for _ in range(num_billing):
            method, route = random.choices(BILLING_ROUTES, weights=BILLING_WEIGHTS, k=1)[0]
            ts = day_start + timedelta(
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
                seconds=random.randint(0, 59),
            )
            if ts > now:
                ts = now - timedelta(seconds=random.randint(1, 3600))

            status_code = random.choices([200, 201, 404, 500], weights=[85, 10, 3, 2], k=1)[0]
            if method == "POST":
                status_code = random.choices([201, 400, 500], weights=[90, 7, 3], k=1)[0]

            db.add(UsageRequest(
                ts=ts,
                caller_service="billing-service",
                method=method,
                route_template=route,
                status_code=status_code,
                duration_ms=round(random.uniform(5, 200), 2),
            ))

        # dashboard-service: 60-120 calls per day (more frequent due to polling)
        num_dashboard = random.randint(60, 120)
        for _ in range(num_dashboard):
            method, route = random.choices(DASHBOARD_ROUTES, weights=DASHBOARD_WEIGHTS, k=1)[0]
            ts = day_start + timedelta(
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
                seconds=random.randint(0, 59),
            )
            if ts > now:
                ts = now - timedelta(seconds=random.randint(1, 3600))

            status_code = random.choices([200, 404, 502], weights=[92, 5, 3], k=1)[0]

            db.add(UsageRequest(
                ts=ts,
                caller_service="dashboard-service",
                method=method,
                route_template=route,
                status_code=status_code,
                duration_ms=round(random.uniform(3, 150), 2),
            ))

    await db.commit()


async def seed_contract_change_demo(db: AsyncSession):
    """Seed a complete contract change with impacts, jobs, and audit trail.

    Provides a full causal chain for the dashboard on first boot:
    breaking change → blast radius → remediation timeline (mixed states).
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
            "summary": "Added required 'max_cost_usd' field to SessionCreate. "
                       "Renamed 'usage.cached_tokens' to 'usage.cache_read_tokens' in SessionResponse. "
                       "Renamed 'billing.total' to 'billing.total_usd'."
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
            calls_last_7d=312,
            confidence="high",
            notes="GET — Reads session list for invoice generation",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions",
            method="POST",
            caller_service="billing-service",
            calls_last_7d=245,
            confidence="high",
            notes="POST — Creates sessions on behalf of clients",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="GET",
            caller_service="dashboard-service",
            calls_last_7d=487,
            confidence="high",
            notes="GET — Fetches individual session detail for drill-down",
        ),
        ImpactSet(
            change_id=change.id,
            route_template="/api/v1/sessions/{session_id}",
            method="PATCH",
            caller_service="dashboard-service",
            calls_last_7d=156,
            confidence="medium",
            notes="PATCH — Updates session metadata from dashboard controls",
        ),
    ]
    for imp in impacts:
        db.add(imp)
    await db.flush()

    # 3. Remediation jobs — one per interesting state
    jobs_spec = [
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/billing-service",
            "status": JobStatus.GREEN.value,
            "devin_run_id": "devin_billing_001",
            "pr_url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/42",
            "created_at": now - timedelta(hours=1, minutes=50),
            "updated_at": now - timedelta(hours=1, minutes=10),
            "bundle_hash": "ab12cd34",
            "error_summary": None,
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/dashboard-service",
            "status": JobStatus.PR_OPENED.value,
            "devin_run_id": "devin_dashboard_001",
            "pr_url": "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/17",
            "created_at": now - timedelta(hours=1, minutes=48),
            "updated_at": now - timedelta(minutes=45),
            "bundle_hash": "ef56gh78",
            "error_summary": None,
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/billing-service",
            "status": JobStatus.CI_FAILED.value,
            "devin_run_id": "devin_billing_002",
            "pr_url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/43",
            "created_at": now - timedelta(hours=1, minutes=45),
            "updated_at": now - timedelta(minutes=30),
            "bundle_hash": "ij90kl12",
            "error_summary": (
                "CI failed: test_invoice_generation assertion error — "
                "expected 'billing.total' and optional budget cap, but response now uses "
                "'billing.total_usd' and session creation requires 'max_cost_usd'"
            ),
        },
        {
            "target_repo": "https://github.com/MadhuvanthiSriPad/dashboard-service",
            "status": JobStatus.NEEDS_HUMAN.value,
            "devin_run_id": "devin_dashboard_002",
            "pr_url": None,
            "created_at": now - timedelta(hours=1, minutes=42),
            "updated_at": now - timedelta(minutes=20),
            "bundle_hash": "mn34op56",
            "error_summary": (
                "Devin session blocked — frontend usage views still reference "
                "'usage.cached_tokens'. Requires human review to decide between "
                "adding a backward-compat shim or fully migrating to "
                "'usage.cache_read_tokens'."
            ),
        },
    ]

    jobs = []
    for spec in jobs_spec:
        job = RemediationJob(change_id=change.id, is_dry_run=True, **spec)
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
                 changed_at=t + timedelta(minutes=40), detail="CI passed, PR merged"),
    ]

    # Job 1: dashboard → PR_OPENED (queued→running→pr_opened)
    j, t = jobs[1], jobs[1].created_at
    audit += [
        AuditLog(job_id=j.job_id, old_status=None, new_status="queued",
                 changed_at=t, detail="Job created"),
        AuditLog(job_id=j.job_id, old_status="queued", new_status="running",
                 changed_at=t + timedelta(seconds=8), detail="Dispatching to Devin"),
        AuditLog(job_id=j.job_id, old_status="running", new_status="pr_opened",
                 changed_at=t + timedelta(minutes=25), detail=f"PR: {j.pr_url}"),
    ]

    # Job 2: billing → CI_FAILED (queued→running→pr_opened→ci_failed)
    j, t = jobs[2], jobs[2].created_at
    audit += [
        AuditLog(job_id=j.job_id, old_status=None, new_status="queued",
                 changed_at=t, detail="Job created"),
        AuditLog(job_id=j.job_id, old_status="queued", new_status="running",
                 changed_at=t + timedelta(seconds=6), detail="Dispatching to Devin"),
        AuditLog(job_id=j.job_id, old_status="running", new_status="pr_opened",
                 changed_at=t + timedelta(minutes=22), detail=f"PR: {j.pr_url}"),
        AuditLog(job_id=j.job_id, old_status="pr_opened", new_status="ci_failed",
                 changed_at=t + timedelta(minutes=35), detail=j.error_summary),
    ]

    # Job 3: dashboard → NEEDS_HUMAN (queued→running→needs_human)
    j, t = jobs[3], jobs[3].created_at
    audit += [
        AuditLog(job_id=j.job_id, old_status=None, new_status="queued",
                 changed_at=t, detail="Job created"),
        AuditLog(job_id=j.job_id, old_status="queued", new_status="running",
                 changed_at=t + timedelta(seconds=4), detail="Dispatching to Devin"),
        AuditLog(job_id=j.job_id, old_status="running", new_status="needs_human",
                 changed_at=t + timedelta(minutes=42), detail=j.error_summary),
    ]

    for entry in audit:
        db.add(entry)

    await db.commit()
