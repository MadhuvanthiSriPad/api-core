"""Poll Devin session statuses and update remediation_job rows.

Usage:
    python -m propagate.check_status              # check latest change
    python -m propagate.check_status --change-id 5 # check a specific change
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from propagate.devin_client import DevinClient
from propagate.guardrails import load_guardrails
from src.config import settings
from src.database import async_session
from src.entities.audit_log import AuditLog
from src.entities.remediation_job import RemediationJob, JobStatus

logger = logging.getLogger(__name__)

CI_UNKNOWN_MAX_ATTEMPTS = 5  # After this many polls with "unknown" CI, fail closed
TERMINAL_STATUSES = {
    JobStatus.GREEN.value,
    JobStatus.CI_FAILED.value,
    JobStatus.NEEDS_HUMAN.value,
}


async def _fetch_github_ci_status(pr_url: str) -> tuple[bool, str]:
    """Fetch CI status from GitHub Checks API as a fallback.

    Returns (ci_passed, ci_status_string).
    """
    github_token = settings.github_token
    if not github_token or not pr_url:
        return False, "unknown"

    # Parse PR URL: https://github.com/{owner}/{repo}/pull/{number}
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        return False, "unknown"

    owner, repo, pr_number = match.group(1), match.group(2), match.group(3)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get the PR to find the head SHA
            pr_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if pr_resp.status_code != 200:
                return False, "unknown"

            head_sha = pr_resp.json().get("head", {}).get("sha", "")
            if not head_sha:
                return False, "unknown"

            # Get check runs for that SHA
            checks_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if checks_resp.status_code != 200:
                return False, "unknown"

            check_runs = checks_resp.json().get("check_runs", [])
            if not check_runs:
                return False, "unknown"

            all_complete = all(cr.get("status") == "completed" for cr in check_runs)
            all_passed = all(cr.get("conclusion") in ("success", "skipped") for cr in check_runs)

            if not all_complete:
                return False, "pending"
            if all_passed:
                return True, "passed"
            return False, "failed"
    except Exception as e:
        logger.warning("GitHub Checks API fetch failed: %s", e)
        return False, "unknown"


async def _fetch_pr_changed_files(pr_url: str) -> list[str]:
    """Fetch the list of changed files from a GitHub PR.

    Returns a list of file paths, or empty list on failure.
    """
    github_token = settings.github_token
    if not github_token or not pr_url:
        return []

    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        return []

    owner, repo, pr_number = match.group(1), match.group(2), match.group(3)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code != 200:
                return []
            return [f.get("filename", "") for f in resp.json()]
    except Exception as e:
        logger.warning("GitHub PR files fetch failed: %s", e)
        return []


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


async def sync_job_statuses(
    db: AsyncSession,
    change_id: int | None = None,
    *,
    log_progress: bool = False,
) -> dict[str, int]:
    """Sync remediation jobs against live Devin/GitHub state."""
    client = DevinClient()
    guardrails = load_guardrails()
    summary = {
        "checked": 0,
        "updated": 0,
        "green": 0,
        "pr_opened": 0,
        "ci_failed": 0,
        "needs_human": 0,
        "running": 0,
    }

    def emit(message: str) -> None:
        if log_progress:
            print(message)

    try:
        stmt = select(RemediationJob).where(
            or_(
                RemediationJob.devin_run_id.isnot(None),
                RemediationJob.pr_url.isnot(None),
            )
        )
        if change_id is not None:
            stmt = stmt.where(RemediationJob.change_id == change_id)

        result = await db.execute(stmt.order_by(RemediationJob.updated_at.desc(), RemediationJob.created_at.desc()))
        jobs = list(result.scalars().all())

        if not jobs:
            emit("No remediation jobs to sync.")
            return summary

        emit(f"Checking {len(jobs)} remediation jobs...\n")

        for job in jobs:
            summary["checked"] += 1
            status = {}
            if job.devin_run_id:
                try:
                    status = await client.get_session(job.devin_run_id)
                except Exception as e:
                    logger.warning("Failed to poll %s: %s", job.devin_run_id, e)
                    emit(f"  [{job.target_repo}] poll error: {e}")

            devin_status = status.get("status_enum", "")
            structured_output = status.get("structured_output", {})
            if not isinstance(structured_output, dict):
                structured_output = {}

            dirty = False
            if structured_output:
                pr_info = structured_output.get("pull_request")
                if isinstance(pr_info, dict):
                    pr_url = pr_info.get("url", "")
                    if pr_url and job.pr_url != pr_url:
                        job.pr_url = pr_url
                        dirty = True

                if (
                    job.pr_url
                    and job.status not in {JobStatus.PR_OPENED.value, JobStatus.GREEN.value}
                    and devin_status not in {"stopped", "blocked"}
                ):
                    old = job.status
                    job.status = JobStatus.PR_OPENED.value
                    job.error_summary = None
                    await _log_transition(db, job, old, JobStatus.PR_OPENED.value, f"PR: {job.pr_url}")
                    emit(f"  [{job.target_repo}] -> PR_OPENED: {job.pr_url}")
                    dirty = True

            if devin_status == "blocked":
                if job.status != JobStatus.NEEDS_HUMAN.value or job.error_summary != "Devin session blocked":
                    old = job.status
                    job.status = JobStatus.NEEDS_HUMAN.value
                    job.error_summary = "Devin session blocked"
                    await _log_transition(db, job, old, JobStatus.NEEDS_HUMAN.value, job.error_summary)
                    emit(f"  [{job.target_repo}] -> NEEDS_HUMAN (blocked)")
                    dirty = True
            elif devin_status == "stopped":
                if job.pr_url:
                    ci_passed, ci_status = await _fetch_github_ci_status(job.pr_url)

                    if ci_status == "unknown":
                        ci_status = (structured_output or {}).get("ci_status", "unknown")
                        ci_passed = ci_status in ("passed", "success")

                    if guardrails.ci_required and not ci_passed:
                        if ci_status == "unknown":
                            from sqlalchemy import func as sa_func
                            ci_unknown_count_result = await db.execute(
                                select(sa_func.count(AuditLog.id)).where(
                                    AuditLog.job_id == job.job_id,
                                    AuditLog.detail.contains("CI status unknown"),
                                )
                            )
                            ci_unknown_count = ci_unknown_count_result.scalar() or 0

                            if ci_unknown_count >= CI_UNKNOWN_MAX_ATTEMPTS:
                                error_summary = (
                                    f"CI status unknown after {CI_UNKNOWN_MAX_ATTEMPTS} checks — failing closed"
                                )
                                if job.status != JobStatus.CI_FAILED.value or job.error_summary != error_summary:
                                    old = job.status
                                    job.status = JobStatus.CI_FAILED.value
                                    job.error_summary = error_summary
                                    await _log_transition(
                                        db,
                                        job,
                                        old,
                                        JobStatus.CI_FAILED.value,
                                        f"CI status unknown after {CI_UNKNOWN_MAX_ATTEMPTS} checks — failing closed: {job.pr_url}",
                                    )
                                    emit(
                                        f"  [{job.target_repo}] -> CI_FAILED (unknown after {CI_UNKNOWN_MAX_ATTEMPTS} checks): {job.pr_url}"
                                    )
                                    dirty = True
                            else:
                                detail = (
                                    f"CI status unknown, holding at PR_OPENED (attempt {ci_unknown_count + 1}/{CI_UNKNOWN_MAX_ATTEMPTS}): {job.pr_url}"
                                )
                                if job.status != JobStatus.PR_OPENED.value:
                                    old = job.status
                                    job.status = JobStatus.PR_OPENED.value
                                    job.error_summary = None
                                    await _log_transition(
                                        db,
                                        job,
                                        old,
                                        JobStatus.PR_OPENED.value,
                                        detail,
                                    )
                                    emit(
                                        f"  [{job.target_repo}] -> PR_OPENED (CI unknown, attempt {ci_unknown_count + 1}/{CI_UNKNOWN_MAX_ATTEMPTS}): {job.pr_url}"
                                    )
                                    dirty = True
                        else:
                            error_summary = f"CI status: {ci_status}"
                            if job.status != JobStatus.CI_FAILED.value or job.error_summary != error_summary:
                                old = job.status
                                job.status = JobStatus.CI_FAILED.value
                                job.error_summary = error_summary
                                await _log_transition(
                                    db,
                                    job,
                                    old,
                                    JobStatus.CI_FAILED.value,
                                    f"PR exists but CI failed ({ci_status}): {job.pr_url}",
                                )
                                emit(f"  [{job.target_repo}] -> CI_FAILED ({ci_status}): {job.pr_url}")
                                dirty = True
                    else:
                        pr_changed_files = (structured_output or {}).get("changed_files", [])
                        if not pr_changed_files and job.pr_url:
                            pr_changed_files = await _fetch_pr_changed_files(job.pr_url)
                        if pr_changed_files:
                            path_violations = guardrails.validate_paths(pr_changed_files)
                            if path_violations:
                                error_summary = f"PR touches protected paths: {'; '.join(path_violations)}"
                                if job.status != JobStatus.NEEDS_HUMAN.value or job.error_summary != error_summary:
                                    old = job.status
                                    job.status = JobStatus.NEEDS_HUMAN.value
                                    job.error_summary = error_summary
                                    await _log_transition(
                                        db,
                                        job,
                                        old,
                                        JobStatus.NEEDS_HUMAN.value,
                                        f"Post-execution path violation: {'; '.join(path_violations)}",
                                    )
                                    emit(f"  [{job.target_repo}] -> NEEDS_HUMAN (protected path): {path_violations}")
                                    dirty = True
                                continue
                        elif guardrails.protected_paths:
                            error_summary = "Cannot verify PR changed files against protected paths"
                            if job.status != JobStatus.NEEDS_HUMAN.value or job.error_summary != error_summary:
                                old = job.status
                                job.status = JobStatus.NEEDS_HUMAN.value
                                job.error_summary = error_summary
                                await _log_transition(
                                    db,
                                    job,
                                    old,
                                    JobStatus.NEEDS_HUMAN.value,
                                    "Path validation fail-closed: changed files unavailable",
                                )
                                emit(f"  [{job.target_repo}] -> NEEDS_HUMAN (changed files unavailable for path check)")
                                dirty = True
                            continue

                        _merge_ok, merge_reason = guardrails.check_can_merge(ci_passed)
                        detail = f"PR: {job.pr_url} | merge: {merge_reason}"
                        if job.status != JobStatus.GREEN.value or job.error_summary is not None:
                            old = job.status
                            job.status = JobStatus.GREEN.value
                            job.error_summary = None
                            await _log_transition(db, job, old, JobStatus.GREEN.value, detail)
                            emit(f"  [{job.target_repo}] -> GREEN: {job.pr_url} ({merge_reason})")
                            dirty = True
                else:
                    if job.status != JobStatus.CI_FAILED.value or job.error_summary != "Devin stopped without PR":
                        old = job.status
                        job.status = JobStatus.CI_FAILED.value
                        job.error_summary = "Devin stopped without PR"
                        await _log_transition(db, job, old, JobStatus.CI_FAILED.value, job.error_summary)
                        emit(f"  [{job.target_repo}] -> CI_FAILED (no PR)")
                        dirty = True
            else:
                emit(f"  [{job.target_repo}] still {job.status} (devin: {devin_status or 'unknown'})")

            if dirty:
                summary["updated"] += 1

        await db.commit()
        status_counts = {
            JobStatus.GREEN.value: "green",
            JobStatus.PR_OPENED.value: "pr_opened",
            JobStatus.CI_FAILED.value: "ci_failed",
            JobStatus.NEEDS_HUMAN.value: "needs_human",
            JobStatus.RUNNING.value: "running",
        }
        for job in jobs:
            bucket = status_counts.get(job.status)
            if bucket:
                summary[bucket] += 1
        return summary
    finally:
        await client.close()


async def check_jobs(change_id: int | None = None) -> None:
    """Check Devin status for jobs, optionally filtered by change_id."""
    async with async_session() as db:
        summary = await sync_job_statuses(db=db, change_id=change_id, log_progress=True)
    print(f"\nDone. Checked {summary['checked']} jobs, updated {summary['updated']}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Devin job statuses")
    parser.add_argument("--change-id", type=int, default=None, help="Filter by change_id")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(check_jobs(change_id=args.change_id))


if __name__ == "__main__":
    main()
