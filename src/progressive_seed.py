"""Progressive demo seeder — drip-feeds contract pipeline data for live walkthroughs.

When the server starts with API_CORE_DEMO_MODE=true, this module progressively
inserts contract-change data so the dashboard pipeline advances step-by-step:

  detect → analyze → plan → dispatch → fix → notify

No frontend changes needed — the 10-second poll picks up each stage automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select

from src.config import settings
from src.database import async_session
from src.entities.audit_log import AuditLog
from src.entities.contract_change import ContractChange
from src.entities.impact_set import ImpactSet
from src.entities.remediation_job import RemediationJob, JobStatus
from src.entities.simulation import ContractSimulation

logger = logging.getLogger(__name__)

# ── Shared data ──────────────────────────────────────────────────────────────

CHANGE_SUMMARY = (
    "Premium enterprise SLA launch made sla_tier required on session creation "
    "and standardized billing.total_usd plus usage.cache_read_tokens in session "
    "responses. Billing, dashboard, and notification-service needed coordinated "
    "client fixes."
)

CHANGED_ROUTES = [
    "POST /api/v1/sessions",
    "GET /api/v1/sessions",
    "GET /api/v1/sessions/{session_id}",
]

CHANGED_FIELDS = [
    {"route": "POST /api/v1/sessions", "field": "sla_tier", "change": "added (required)"},
    {"route": "GET /api/v1/sessions", "field": "billing.total_usd", "change": "renamed from billing.total"},
    {"route": "GET /api/v1/sessions/{session_id}", "field": "usage.cache_read_tokens", "change": "renamed from usage.cached_tokens"},
]

JOBS = [
    {
        "target_repo": "https://github.com/MadhuvanthiSriPad/billing-service",
        "bundle_hash": "billing-sla-008",
        "devin_run_id": "devin_billing_sla_008",
        "pr_url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/8",
    },
    {
        "target_repo": "https://github.com/MadhuvanthiSriPad/dashboard-service",
        "bundle_hash": "dashboard-sla-004",
        "devin_run_id": "devin_dashboard_sla_004",
        "pr_url": "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/4",
    },
    {
        "target_repo": "https://github.com/MadhuvanthiSriPad/notification-service",
        "bundle_hash": "notification-sla-006",
        "devin_run_id": "devin_notification_sla_006",
        "pr_url": "https://github.com/MadhuvanthiSriPad/notification-service/pull/6",
    },
]

DEMO_STAGES = [
    {"key": "detect", "label": "Detect contract diff"},
    {"key": "analyze", "label": "Analyze blast radius"},
    {"key": "dispatch", "label": "Queue remediation jobs"},
    {"key": "running", "label": "Launch Devin sessions"},
    {"key": "billing_pr", "label": "Open billing-service PR"},
    {"key": "dashboard_pr", "label": "Open dashboard-service PR"},
    {"key": "notification_pr", "label": "Open notification-service PR"},
    {"key": "notify", "label": "Send stakeholder notifications"},
]

_STAGE_INDEX = {stage["key"]: index for index, stage in enumerate(DEMO_STAGES)}
_PR_STAGE_ORDER = [
    ("billing-service", "billing_pr"),
    ("dashboard-service", "dashboard_pr"),
    ("notification-service", "notification_pr"),
]
_DEMO_REF_PREFIX = "accr_"


# ── Stage functions ──────────────────────────────────────────────────────────

async def _stage_detect() -> int:
    """Insert the ContractChange row. Returns the change_id."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        change = ContractChange(
            base_ref="accr_2026_02_27",
            head_ref="accr_2026_03_01",
            created_at=now,
            is_breaking=True,
            severity="high",
            summary_json=json.dumps({"summary": CHANGE_SUMMARY}),
            changed_routes_json=json.dumps(CHANGED_ROUTES),
            changed_fields_json=json.dumps(CHANGED_FIELDS),
        )
        db.add(change)
        await db.flush()
        change_id = change.id
        await db.commit()
        logger.info("Stage DETECT: ContractChange id=%d created", change_id)
        return change_id


async def _stage_analyze(change_id: int) -> None:
    """Insert ImpactSets and ContractSimulations."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)

        impacts = [
            ImpactSet(
                change_id=change_id,
                route_template="/api/v1/sessions",
                method="POST",
                caller_service="billing-service",
                calls_last_7d=8,
                confidence="high",
                notes="Invoice backfill and reconciliation jobs create sessions with budget controls",
            ),
            ImpactSet(
                change_id=change_id,
                route_template="/api/v1/sessions",
                method="GET",
                caller_service="billing-service",
                calls_last_7d=12,
                confidence="high",
                notes="Finance rollups and invoice review pages read normalized billing fields",
            ),
            ImpactSet(
                change_id=change_id,
                route_template="/api/v1/sessions/{session_id}",
                method="GET",
                caller_service="dashboard-service",
                calls_last_7d=22,
                confidence="high",
                notes="Session drill-down cards and remediation detail views use the full response shape",
            ),
            ImpactSet(
                change_id=change_id,
                route_template="/api/v1/sessions/{session_id}",
                method="GET",
                caller_service="notification-service",
                calls_last_7d=9,
                confidence="high",
                notes="Recovery reports enrich Slack and Jira updates with current session details",
            ),
        ]

        simulations = [
            ContractSimulation(
                change_id=change_id,
                service_name="billing-service",
                risk_score=0.85,
                risk_level="high",
                breaking_issues_json=json.dumps([
                    {
                        "diff_type": "field_renamed",
                        "path": "/api/v1/sessions",
                        "method": "GET",
                        "field": "billing.total \u2192 billing.total_usd",
                        "detail": "Invoice rollup and reconciliation jobs reference billing.total directly; rename will cause runtime KeyError in cost aggregation pipeline",
                        "weight": 0.40,
                    },
                    {
                        "diff_type": "field_added_required",
                        "path": "/api/v1/sessions",
                        "method": "POST",
                        "field": "sla_tier",
                        "detail": "Session creation payloads from billing backfill jobs do not include sla_tier; requests will be rejected with 422 until client is patched",
                        "weight": 0.30,
                    },
                    {
                        "diff_type": "field_renamed",
                        "path": "/api/v1/sessions/{session_id}",
                        "method": "GET",
                        "field": "usage.cached_tokens \u2192 usage.cache_read_tokens",
                        "detail": "Budget audit reports read usage.cached_tokens for cost attribution; rename breaks finance export CSV generation",
                        "weight": 0.15,
                    },
                ]),
                fields_affected=3,
                routes_affected=3,
                created_at=now,
            ),
            ContractSimulation(
                change_id=change_id,
                service_name="dashboard-service",
                risk_score=0.52,
                risk_level="medium",
                breaking_issues_json=json.dumps([
                    {
                        "diff_type": "field_renamed",
                        "path": "/api/v1/sessions/{session_id}",
                        "method": "GET",
                        "field": "usage.cached_tokens \u2192 usage.cache_read_tokens",
                        "detail": "Session detail drill-down card renders usage.cached_tokens in the token breakdown widget; will show 'N/A' after rename",
                        "weight": 0.30,
                    },
                    {
                        "diff_type": "field_renamed",
                        "path": "/api/v1/sessions",
                        "method": "GET",
                        "field": "billing.total \u2192 billing.total_usd",
                        "detail": "Cost-by-team chart tooltip reads billing.total for the formatted display value; tooltip will render $0.00 for all rows",
                        "weight": 0.22,
                    },
                ]),
                fields_affected=2,
                routes_affected=2,
                created_at=now,
            ),
            ContractSimulation(
                change_id=change_id,
                service_name="notification-service",
                risk_score=0.18,
                risk_level="safe",
                breaking_issues_json=json.dumps([
                    {
                        "diff_type": "field_renamed",
                        "path": "/api/v1/sessions/{session_id}",
                        "method": "GET",
                        "field": "usage.cached_tokens \u2192 usage.cache_read_tokens",
                        "detail": "Slack recovery digest includes a token summary but uses a fallback default when fields are missing; rename has low impact",
                        "weight": 0.18,
                    },
                ]),
                fields_affected=1,
                routes_affected=1,
                created_at=now,
            ),
        ]

        for obj in impacts + simulations:
            db.add(obj)
        await db.commit()
        logger.info("Stage ANALYZE: 4 impacts + 3 simulations inserted")


async def _stage_dispatch(change_id: int) -> list[int]:
    """Insert 3 RemediationJobs (queued) + initial audit logs. Returns job_ids."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        job_ids: list[int] = []

        for spec in JOBS:
            job = RemediationJob(
                change_id=change_id,
                target_repo=spec["target_repo"],
                status=JobStatus.QUEUED.value,
                bundle_hash=spec["bundle_hash"],
                is_dry_run=False,
                created_at=now,
                updated_at=now,
            )
            db.add(job)
            await db.flush()
            job_ids.append(job.job_id)

            db.add(AuditLog(
                job_id=job.job_id,
                old_status=None,
                new_status="queued",
                changed_at=now,
                detail=f"Queued after contract diff classified as breaking — {_repo_name(spec['target_repo'])}",
            ))

        await db.commit()
        logger.info("Stage DISPATCH: 3 jobs queued, ids=%s", job_ids)
        return job_ids


async def _stage_running(change_id: int) -> None:
    """Update all jobs to running + add audit logs."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(RemediationJob).where(RemediationJob.change_id == change_id)
        )
        jobs = list(result.scalars().all())

        for job, spec in zip(jobs, JOBS):
            job.status = JobStatus.RUNNING.value
            job.devin_run_id = spec["devin_run_id"]
            job.updated_at = now
            db.add(AuditLog(
                job_id=job.job_id,
                old_status="queued",
                new_status="running",
                changed_at=now,
                detail=f"Devin session dispatched for {_repo_name(job.target_repo)}",
            ))

        await db.commit()
        logger.info("Stage RUNNING: 3 jobs → running")


async def _stage_pr(change_id: int, repo_name: str) -> None:
    """Update one job to awaiting_merge + add PR URL + audit log."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(RemediationJob).where(
                RemediationJob.change_id == change_id,
                RemediationJob.target_repo.contains(repo_name),
            )
        )
        job = result.scalars().first()
        if not job:
            logger.warning("Stage PR: no job found for %s", repo_name)
            return

        spec = next(s for s in JOBS if repo_name in s["target_repo"])
        job.status = JobStatus.AWAITING_MERGE.value
        job.pr_url = spec["pr_url"]
        job.updated_at = now

        db.add(AuditLog(
            job_id=job.job_id,
            old_status="running",
            new_status="awaiting_merge",
            changed_at=now,
            detail=f"PR opened — CI green, ready for review: {spec['pr_url']}",
        ))

        await db.commit()
        logger.info("Stage PR: %s → awaiting_merge", repo_name)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _repo_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _stage_label(stage_key: str | None) -> str | None:
    if stage_key is None:
        return None
    for stage in DEMO_STAGES:
        if stage["key"] == stage_key:
            return stage["label"]
    return None


def _has_pr_stage(job: RemediationJob) -> bool:
    return bool(job.pr_url) or job.status in {
        JobStatus.AWAITING_MERGE.value,
        JobStatus.CI_FAILED.value,
        JobStatus.NEEDS_HUMAN.value,
        JobStatus.MERGED.value,
    }


async def _stage_notify(change_id: int) -> None:
    """Record the final stakeholder-notification handoff."""
    async with async_session() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(RemediationJob).where(RemediationJob.change_id == change_id)
        )
        jobs = list(result.scalars().all())

        for job in jobs:
            db.add(AuditLog(
                job_id=job.job_id,
                old_status=job.status,
                new_status="notification_sent",
                changed_at=now,
                detail="Stakeholder notifications sent to Slack and Jira for review handoff",
            ))

        await db.commit()
        logger.info("Stage NOTIFY: stakeholder notifications recorded")


def _build_demo_status_payload(
    *,
    change_id: int | None,
    current_stage: str | None,
    next_stage: str | None,
    available: bool = True,
) -> dict:
    completed_steps = 0
    if available and next_stage is None:
        completed_steps = len(DEMO_STAGES)
    elif available and next_stage in _STAGE_INDEX:
        completed_steps = _STAGE_INDEX[next_stage]

    return {
        "change_id": change_id,
        "current_stage": current_stage,
        "current_label": _stage_label(current_stage),
        "next_stage": next_stage,
        "next_label": _stage_label(next_stage),
        "completed_steps": completed_steps,
        "total_steps": len(DEMO_STAGES),
        "is_complete": available and next_stage is None,
        "demo_available": available,
        "demo_enabled": settings.demo_mode,
    }


async def get_progressive_demo_status() -> dict:
    """Return the current manual demo progression state."""
    async with async_session() as db:
        change_result = await db.execute(
            select(ContractChange)
            .where(
                ContractChange.base_ref.like(f"{_DEMO_REF_PREFIX}%"),
                ContractChange.head_ref.like(f"{_DEMO_REF_PREFIX}%"),
            )
            .order_by(ContractChange.created_at.desc(), ContractChange.id.desc())
            .limit(1)
        )
        change = change_result.scalars().first()
        if not change:
            if not settings.demo_mode:
                return _build_demo_status_payload(
                    change_id=None,
                    current_stage=None,
                    next_stage=None,
                    available=False,
                )
            return _build_demo_status_payload(
                change_id=None,
                current_stage=None,
                next_stage="detect",
            )

        change_id = change.id
        impact_exists = (
            await db.execute(
                select(ImpactSet.id)
                .where(ImpactSet.change_id == change_id)
                .limit(1)
            )
        ).scalar_one_or_none() is not None
        simulation_exists = (
            await db.execute(
                select(ContractSimulation.id)
                .where(ContractSimulation.change_id == change_id)
                .limit(1)
            )
        ).scalar_one_or_none() is not None
        jobs_result = await db.execute(
            select(RemediationJob)
            .where(RemediationJob.change_id == change_id)
            .order_by(RemediationJob.created_at.asc(), RemediationJob.job_id.asc())
        )
        jobs = list(jobs_result.scalars().all())

        if not impact_exists or not simulation_exists:
            return _build_demo_status_payload(
                change_id=change_id,
                current_stage="detect",
                next_stage="analyze",
            )
        if not jobs:
            return _build_demo_status_payload(
                change_id=change_id,
                current_stage="analyze",
                next_stage="dispatch",
            )

        any_running_signal = any(
            job.status in {
                JobStatus.RUNNING.value,
                JobStatus.AWAITING_MERGE.value,
                JobStatus.CI_FAILED.value,
                JobStatus.NEEDS_HUMAN.value,
                JobStatus.MERGED.value,
            }
            or bool(job.devin_run_id)
            for job in jobs
        )
        if not any_running_signal:
            return _build_demo_status_payload(
                change_id=change_id,
                current_stage="dispatch",
                next_stage="running",
            )

        jobs_by_repo = {_repo_name(job.target_repo): job for job in jobs if job.target_repo}
        last_completed = "running"
        for repo_name, stage_key in _PR_STAGE_ORDER:
            job = jobs_by_repo.get(repo_name)
            if job is None or not _has_pr_stage(job):
                return _build_demo_status_payload(
                    change_id=change_id,
                    current_stage=last_completed,
                    next_stage=stage_key,
                )
            last_completed = stage_key

        job_ids = [job.job_id for job in jobs]
        if job_ids:
            audit_result = await db.execute(
                select(AuditLog).where(AuditLog.job_id.in_(job_ids))
            )
            for entry in audit_result.scalars().all():
                detail_text = (entry.detail or "").lower()
                status_text = (entry.new_status or "").lower()
                if (
                    status_text == "notification_sent"
                    or "stakeholder notifications sent" in detail_text
                ):
                    return _build_demo_status_payload(
                        change_id=change_id,
                        current_stage="notify",
                        next_stage=None,
                    )

        return _build_demo_status_payload(
            change_id=change_id,
            current_stage=last_completed,
            next_stage="notify",
        )


async def advance_progressive_demo() -> dict:
    """Advance the demo by one stage and return the updated status."""
    status = await get_progressive_demo_status()
    next_stage = status["next_stage"]
    if next_stage is None and not status.get("demo_available"):
        next_stage = "detect"
    change_id = status["change_id"]

    if next_stage is None:
        return {
            **status,
            "advanced_stage": None,
            "advanced_label": None,
            "message": "Demo is already at the final state.",
        }

    if next_stage == "detect":
        change_id = await _stage_detect()
    elif change_id is None:
        raise RuntimeError("Demo progression is missing a contract change record.")
    elif next_stage == "analyze":
        await _stage_analyze(change_id)
    elif next_stage == "dispatch":
        await _stage_dispatch(change_id)
    elif next_stage == "running":
        await _stage_running(change_id)
    elif next_stage == "notify":
        await _stage_notify(change_id)
    else:
        repo_name = next(
            repo for repo, stage_key in _PR_STAGE_ORDER if stage_key == next_stage
        )
        await _stage_pr(change_id, repo_name)

    updated = await get_progressive_demo_status()
    return {
        **updated,
        "advanced_stage": next_stage,
        "advanced_label": _stage_label(next_stage),
        "message": f"Advanced demo to {_stage_label(next_stage)}.",
    }


async def reset_progressive_demo() -> dict:
    """Clear contract-recovery demo records and return a reset status payload."""
    async with async_session() as db:
        await db.execute(delete(AuditLog))
        await db.execute(delete(RemediationJob))
        await db.execute(delete(ImpactSet))
        await db.execute(delete(ContractSimulation))
        await db.execute(delete(ContractChange))
        await db.commit()

    status = await get_progressive_demo_status()
    return {
        **status,
        "message": "Demo contract-recovery data cleared.",
    }


# ── Main orchestrator ────────────────────────────────────────────────────────

async def run_progressive_demo(speed_factor: float = 1.0) -> None:
    """Run the progressive pipeline reveal. Called as a background task."""

    async def wait(seconds: float) -> None:
        await asyncio.sleep(seconds / max(speed_factor, 0.1))

    # Guard: skip if contract data already exists
    async with async_session() as db:
        existing = await db.execute(select(ContractChange).limit(1))
        if existing.scalars().first():
            logger.info("Progressive demo: contract data already exists — skipping")
            return

    logger.info("Progressive demo: pipeline starts in 5s (speed=%.1fx)", speed_factor)
    await wait(5)

    # Stage 1: Detect
    logger.info("Progressive demo: ── Stage 1 — DETECT ──")
    change_id = await _stage_detect()
    await wait(5)

    # Stage 2: Analyze + Simulate
    logger.info("Progressive demo: ── Stage 2 — ANALYZE + SIMULATE ──")
    await _stage_analyze(change_id)
    await wait(5)

    # Stage 3: Dispatch (queued)
    logger.info("Progressive demo: ── Stage 3 — DISPATCH (queued) ──")
    await _stage_dispatch(change_id)
    await wait(5)

    # Stage 4: Running
    logger.info("Progressive demo: ── Stage 4 — RUNNING ──")
    await _stage_running(change_id)
    await wait(5)

    # Stage 5: PRs open one-by-one
    logger.info("Progressive demo: ── Stage 5a — billing-service PR ──")
    await _stage_pr(change_id, "billing-service")
    await wait(5)

    logger.info("Progressive demo: ── Stage 5b — dashboard-service PR ──")
    await _stage_pr(change_id, "dashboard-service")
    await wait(5)

    logger.info("Progressive demo: ── Stage 5c — notification-service PR ──")
    await _stage_pr(change_id, "notification-service")
    await wait(5)

    # Stage 6: Notify
    logger.info("Progressive demo: ── Stage 6 — NOTIFY ──")
    await _stage_notify(change_id)

    logger.info("Progressive demo: ✓ pipeline complete — stakeholder notifications sent")
