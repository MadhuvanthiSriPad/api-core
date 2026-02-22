"""Poll Devin session statuses and update remediation_job rows.

Usage:
    python -m propagate.check_status              # check latest change
    python -m propagate.check_status --change-id 5 # check a specific change
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from propagate.devin_client import DevinClient
from propagate.guardrails import load_guardrails
from src.database import async_session
from src.models.audit_log import AuditLog
from src.models.remediation_job import RemediationJob, JobStatus

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    JobStatus.GREEN.value,
    JobStatus.CI_FAILED.value,
    JobStatus.NEEDS_HUMAN.value,
}


async def _log_transition(
    db: AsyncSession,
    job: RemediationJob,
    old_status: str,
    new_status: str,
    detail: str | None = None,
):
    entry = AuditLog(
        job_id=job.job_id,
        old_status=old_status,
        new_status=new_status,
        detail=detail,
    )
    db.add(entry)


async def check_jobs(change_id: int | None = None) -> None:
    """Check Devin status for all running jobs, optionally filtered by change_id."""
    client = DevinClient()
    guardrails = load_guardrails()

    async with async_session() as db:
        stmt = select(RemediationJob).where(
            RemediationJob.status.notin_(TERMINAL_STATUSES),
            RemediationJob.devin_run_id.isnot(None),
        )
        if change_id is not None:
            stmt = stmt.where(RemediationJob.change_id == change_id)

        result = await db.execute(stmt)
        jobs = list(result.scalars().all())

        if not jobs:
            print("No in-progress jobs to check.")
            return

        print(f"Checking {len(jobs)} in-progress jobs...\n")

        for job in jobs:
            try:
                status = await client.get_session(job.devin_run_id)
            except Exception as e:
                logger.warning("Failed to poll %s: %s", job.devin_run_id, e)
                print(f"  [{job.target_repo}] poll error: {e}")
                continue

            devin_status = status.get("status_enum", "")

            # Check for PR creation
            structured_output = status.get("structured_output", {})
            if structured_output:
                pr_info = structured_output.get("pull_request")
                if pr_info:
                    job.pr_url = pr_info.get("url", "")
                    if job.status != JobStatus.PR_OPENED.value:
                        old = job.status
                        job.status = JobStatus.PR_OPENED.value
                        await _log_transition(db, job, old, JobStatus.PR_OPENED.value, f"PR: {job.pr_url}")
                        print(f"  [{job.target_repo}] -> PR_OPENED: {job.pr_url}")

            # Terminal states
            if devin_status == "blocked":
                old = job.status
                job.status = JobStatus.NEEDS_HUMAN.value
                job.error_summary = "Devin session blocked"
                await _log_transition(db, job, old, JobStatus.NEEDS_HUMAN.value, job.error_summary)
                print(f"  [{job.target_repo}] -> NEEDS_HUMAN (blocked)")
            elif devin_status == "stopped":
                old = job.status
                if job.pr_url:
                    # Check CI status from structured output before marking GREEN
                    ci_status = (structured_output or {}).get("ci_status", "unknown")
                    ci_passed = ci_status in ("passed", "success")

                    if guardrails.ci_required and not ci_passed and ci_status != "unknown":
                        job.status = JobStatus.CI_FAILED.value
                        job.error_summary = f"CI status: {ci_status}"
                        await _log_transition(
                            db, job, old, JobStatus.CI_FAILED.value,
                            f"PR exists but CI failed ({ci_status}): {job.pr_url}",
                        )
                        print(f"  [{job.target_repo}] -> CI_FAILED ({ci_status}): {job.pr_url}")
                    else:
                        job.status = JobStatus.GREEN.value
                        merge_ok, merge_reason = guardrails.check_can_merge(ci_passed)
                        detail = f"PR: {job.pr_url} | merge: {merge_reason}"
                        await _log_transition(db, job, old, JobStatus.GREEN.value, detail)
                        print(f"  [{job.target_repo}] -> GREEN: {job.pr_url} ({merge_reason})")
                else:
                    job.status = JobStatus.CI_FAILED.value
                    job.error_summary = "Devin stopped without PR"
                    await _log_transition(db, job, old, JobStatus.CI_FAILED.value, job.error_summary)
                    print(f"  [{job.target_repo}] -> CI_FAILED (no PR)")
            else:
                print(f"  [{job.target_repo}] still {job.status} (devin: {devin_status})")

        await db.commit()
    print("\nDone.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Devin job statuses")
    parser.add_argument("--change-id", type=int, default=None, help="Filter by change_id")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(check_jobs(change_id=args.change_id))


if __name__ == "__main__":
    main()
