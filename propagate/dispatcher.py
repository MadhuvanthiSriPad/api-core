"""Fire-and-forget job dispatcher — fans out Devin sessions, then exits."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from propagate.bundle import RepoFixBundle
from propagate.devin_client import DevinClient
from propagate.guardrails import Guardrails
from src.config import settings
from src.database import async_session as async_session_factory
from src.entities.remediation_job import RemediationJob, JobStatus
from src.entities.audit_log import AuditLog

logger = logging.getLogger(__name__)


def _guardrail_target_paths(bundle: RepoFixBundle) -> list[str]:
    """Return all path classes a remediation is expected to touch."""
    return sorted(set(bundle.client_paths + bundle.test_paths + bundle.frontend_paths))


async def _log_transition(
    db: AsyncSession,
    job: RemediationJob,
    old_status: str | None,
    new_status: str,
    detail: str | None = None,
):
    """Insert an audit_log row for a job state transition."""
    entry = AuditLog(
        job_id=job.job_id,
        old_status=old_status,
        new_status=new_status,
        detail=detail,
    )
    db.add(entry)


async def dispatch_remediation_jobs(
    bundles: list[RepoFixBundle],
    guardrails: Guardrails,
    change_id: int,
) -> list[RemediationJob]:
    """Dispatch Devin jobs concurrently, then return immediately.

    Creates remediation_job rows and dispatches Devin sessions.
    Each dispatch_one() gets its own AsyncSession to avoid concurrency issues.
    Does NOT poll for completion — use ``check_status`` to monitor.
    """
    client = DevinClient()
    semaphore = asyncio.Semaphore(guardrails.max_parallel)
    jobs: list[RemediationJob] = []

    print(f"\nDispatching {len(bundles)} Devin sessions (concurrency={guardrails.max_parallel})")

    async def dispatch_one(bundle: RepoFixBundle) -> RemediationJob:
        # Each coroutine gets its own session to avoid concurrent AsyncSession use
        async with async_session_factory() as own_db:
            # Validate guardrails against all declared target paths.
            violations = guardrails.validate_paths(_guardrail_target_paths(bundle))
            if violations:
                logger.warning(
                    "Guardrail violation for %s: %s", bundle.target_service, violations
                )
                job = RemediationJob(
                    change_id=change_id,
                    target_repo=bundle.target_repo,
                    status=JobStatus.NEEDS_HUMAN.value,
                    bundle_hash=bundle.bundle_hash,
                    error_summary=f"Guardrail violation: {'; '.join(violations)}",
                )
                own_db.add(job)
                await own_db.flush()
                await _log_transition(
                    own_db, job, None, JobStatus.NEEDS_HUMAN.value,
                    f"Blocked by guardrail: {'; '.join(violations)}"
                )
                await own_db.commit()
                print(f"  [{bundle.target_service}] BLOCKED by guardrail: {violations}")
                return job

            job = RemediationJob(
                change_id=change_id,
                target_repo=bundle.target_repo,
                status=JobStatus.QUEUED.value,
                bundle_hash=bundle.bundle_hash,
            )
            own_db.add(job)
            await own_db.flush()
            await _log_transition(own_db, job, None, JobStatus.QUEUED.value, "Job created")

            async with semaphore:
                try:
                    old = job.status
                    job.status = JobStatus.RUNNING.value
                    await _log_transition(own_db, job, old, JobStatus.RUNNING.value, "Dispatching to Devin")
                    await own_db.flush()

                    session = await client.create_session(
                        bundle.prompt,
                        idempotency_key=bundle.bundle_hash,
                    )
                    job.devin_run_id = session.get("session_id", "")
                    await own_db.flush()

                    session_url = f"{settings.devin_app_base}/sessions/{job.devin_run_id}"
                    print(f"  [{bundle.target_service}] dispatched -> {session_url}")

                except Exception as e:
                    old = job.status
                    job.status = JobStatus.NEEDS_HUMAN.value
                    job.error_summary = str(e)
                    await _log_transition(own_db, job, old, JobStatus.NEEDS_HUMAN.value, str(e))
                    logger.exception("Dispatch failed for %s", bundle.target_service)
                    print(f"  [{bundle.target_service}] FAILED: {e}")

                job.updated_at = datetime.now(timezone.utc)
                await own_db.commit()
                return job

    tasks = [dispatch_one(bundle) for bundle in bundles]
    completed_jobs = await asyncio.gather(*tasks, return_exceptions=True)

    for result in completed_jobs:
        if isinstance(result, RemediationJob):
            jobs.append(result)
        elif isinstance(result, Exception):
            logger.error("Job dispatch exception: %s", result)

    dispatched = sum(1 for j in jobs if j.devin_run_id)
    failed = len(jobs) - dispatched
    print(f"\nDone: {dispatched} dispatched, {failed} failed")
    print("Run `python -m propagate.check_status` to poll results.\n")

    return jobs
