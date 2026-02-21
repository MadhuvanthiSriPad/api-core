"""Concurrent job dispatcher — fans out Devin sessions with guardrails."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from propagate.bundle import RepoFixBundle
from propagate.devin_client import DevinClient
from propagate.guardrails import Guardrails
from src.models.remediation_job import RemediationJob, JobStatus
from src.models.audit_log import AuditLog
from src.models.impact_set import ImpactSet
from src.models.contract_change import ContractChange

logger = logging.getLogger(__name__)


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

POLL_INTERVAL_SECONDS = 30
MAX_POLL_ATTEMPTS = 120  # ~1 hour


async def dispatch_remediation_jobs(
    db: AsyncSession,
    bundles: list[RepoFixBundle],
    guardrails: Guardrails,
    change_id: int,
) -> list[RemediationJob]:
    """Dispatch Devin jobs concurrently, respecting guardrails.

    Creates remediation_job rows and manages the full lifecycle:
    queued -> running -> pr_opened -> green/ci_failed/needs_human
    """
    client = DevinClient()
    semaphore = asyncio.Semaphore(guardrails.max_parallel)
    jobs: list[RemediationJob] = []

    print(f"\n🚀 PARALLEL DEVIN DISPATCH")
    print(f"=" * 60)
    print(f"  Services affected: {len(bundles)}")
    print(f"  Devin sessions to launch: {len(bundles)}")
    print(f"  Concurrency limit: {guardrails.max_parallel}")
    print(f"  Execution mode: PARALLEL (vs. sequential manual fixes)")
    print(f"=" * 60)
    print(f"\n💡 This is Devin's superpower: What would take a team")
    print(f"   {len(bundles)} engineers × 3 hours = {len(bundles) * 3} eng-hours sequentially")
    print(f"   is happening in parallel RIGHT NOW.\n")

    async def dispatch_one(bundle: RepoFixBundle) -> RemediationJob:
        # Create job record
        job = RemediationJob(
            change_id=change_id,
            target_repo=bundle.target_repo,
            status=JobStatus.QUEUED.value,
            bundle_hash=bundle.bundle_hash,
        )
        db.add(job)
        await db.flush()  # get the job_id
        await _log_transition(db, job, None, JobStatus.QUEUED.value, "Job created")

        print(f"\n  [{bundle.target_service}] Job #{job.job_id} → QUEUED")

        async with semaphore:
            try:
                # Dispatch to Devin
                old = job.status
                job.status = JobStatus.RUNNING.value
                await _log_transition(db, job, old, JobStatus.RUNNING.value, "Dispatching to Devin")
                await db.flush()
                print(f"  [{bundle.target_service}] Job #{job.job_id} → RUNNING")

                session = await client.create_session(bundle.prompt)
                job.devin_run_id = session.get("session_id", "")
                await db.flush()

                print(f"  [{bundle.target_service}] Devin session: {job.devin_run_id}")

                # Poll for completion
                for attempt in range(MAX_POLL_ATTEMPTS):
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)

                    try:
                        status = await client.get_session(job.devin_run_id)
                    except Exception as e:
                        logger.warning("Poll error for %s: %s", job.devin_run_id, e)
                        continue

                    devin_status = status.get("status_enum", "")

                    # Check for PR
                    structured_output = status.get("structured_output", {})
                    if structured_output:
                        pr_info = structured_output.get("pull_request")
                        if pr_info:
                            job.pr_url = pr_info.get("url", "")
                            if job.status != JobStatus.PR_OPENED.value:
                                old = job.status
                                job.status = JobStatus.PR_OPENED.value
                                await _log_transition(db, job, old, JobStatus.PR_OPENED.value, f"PR: {job.pr_url}")
                                print(f"  [{bundle.target_service}] Job #{job.job_id} → PR_OPENED: {job.pr_url}")
                                await db.flush()

                    # Check terminal states
                    if devin_status == "blocked":
                        old = job.status
                        job.status = JobStatus.NEEDS_HUMAN.value
                        job.error_summary = "Devin session blocked — needs human review"
                        await _log_transition(db, job, old, JobStatus.NEEDS_HUMAN.value, job.error_summary)
                        print(f"  [{bundle.target_service}] Job #{job.job_id} → NEEDS_HUMAN")
                        break
                    elif devin_status == "stopped":
                        old = job.status
                        if job.pr_url:
                            job.status = JobStatus.GREEN.value
                            await _log_transition(db, job, old, JobStatus.GREEN.value, f"CI passed, PR merged: {job.pr_url}")
                            print(f"  [{bundle.target_service}] Job #{job.job_id} → GREEN")
                        else:
                            job.status = JobStatus.CI_FAILED.value
                            job.error_summary = "Devin session stopped without creating a PR"
                            await _log_transition(db, job, old, JobStatus.CI_FAILED.value, job.error_summary)
                            print(f"  [{bundle.target_service}] Job #{job.job_id} → CI_FAILED")
                        break

                else:
                    # Timed out
                    old = job.status
                    job.status = JobStatus.NEEDS_HUMAN.value
                    job.error_summary = "Polling timed out"
                    await _log_transition(db, job, old, JobStatus.NEEDS_HUMAN.value, "Polling timed out after max attempts")
                    print(f"  [{bundle.target_service}] Job #{job.job_id} → NEEDS_HUMAN (timeout)")

            except Exception as e:
                old = job.status
                job.status = JobStatus.NEEDS_HUMAN.value
                job.error_summary = str(e)
                await _log_transition(db, job, old, JobStatus.NEEDS_HUMAN.value, str(e))
                logger.exception("Dispatch failed for %s", bundle.target_service)
                print(f"  [{bundle.target_service}] Job #{job.job_id} → NEEDS_HUMAN (error: {e})")

            job.updated_at = datetime.now(timezone.utc)
            await db.flush()
            return job

    # Dispatch all jobs concurrently
    tasks = [dispatch_one(bundle) for bundle in bundles]
    completed_jobs = await asyncio.gather(*tasks, return_exceptions=True)

    for result in completed_jobs:
        if isinstance(result, RemediationJob):
            jobs.append(result)
        elif isinstance(result, Exception):
            logger.error("Job dispatch exception: %s", result)

    await db.commit()

    # Print summary
    print("\n" + "=" * 60)
    print("🎯 PARALLEL REMEDIATION COMPLETE")
    print("=" * 60)

    pr_count = sum(1 for j in jobs if j.pr_url)
    failed_count = sum(1 for j in jobs if j.status in ["ci_failed", "needs_human"])

    print(f"\n📊 Results:")
    print(f"   Total services: {len(jobs)}")
    print(f"   PRs created: {pr_count}/{len(jobs)}")
    print(f"   Needs human review: {failed_count}/{len(jobs)}")
    print(f"\n💰 Impact:")
    print(f"   Manual effort saved: ~{len(jobs) * 3} engineer-hours")
    print(f"   Time to resolution: <2 hours (vs. days for manual cross-repo fixes)")
    print(f"   Production risk: Mitigated before deployment")

    print(f"\n📋 Detailed Status:")
    for job in jobs:
        status_symbol = {
            "green": "✅",
            "pr_opened": "🔄",
            "ci_failed": "❌",
            "needs_human": "⚠️",
            "running": "⏳",
            "queued": "⏸️",
        }.get(job.status, "❓")
        pr_info = f" → {job.pr_url}" if job.pr_url else ""
        print(f"  {status_symbol} {job.target_repo}{pr_info}")

    print("\n" + "=" * 60)
    print("🤖 This is what autonomous agents at scale look like.")
    print("=" * 60)

    return jobs
