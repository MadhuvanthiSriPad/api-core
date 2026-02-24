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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from propagate.devin_client import DevinClient
from propagate.guardrails import load_guardrails
from src.config import settings
from src.database import async_session
from src.entities.audit_log import AuditLog
from src.entities.remediation_job import RemediationJob, JobStatus

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    JobStatus.GREEN.value,
    JobStatus.CI_FAILED.value,
    JobStatus.NEEDS_HUMAN.value,
}

CI_UNKNOWN_MAX_ATTEMPTS = 5  # After this many polls with "unknown" CI, fail closed


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
                    # Try GitHub Checks API first (authoritative), fall back to Devin structured_output
                    ci_passed, ci_status = await _fetch_github_ci_status(job.pr_url)

                    if ci_status == "unknown":
                        ci_status = (structured_output or {}).get("ci_status", "unknown")
                        ci_passed = ci_status in ("passed", "success")

                    if guardrails.ci_required and not ci_passed:
                        if ci_status == "unknown":
                            # Count prior "CI unknown" audit entries to determine attempt count
                            from sqlalchemy import func as sa_func
                            ci_unknown_count_result = await db.execute(
                                select(sa_func.count(AuditLog.id)).where(
                                    AuditLog.job_id == job.job_id,
                                    AuditLog.detail.contains("CI status unknown"),
                                )
                            )
                            ci_unknown_count = ci_unknown_count_result.scalar() or 0

                            if ci_unknown_count >= CI_UNKNOWN_MAX_ATTEMPTS:
                                # Fail closed after too many unknown checks
                                job.status = JobStatus.CI_FAILED.value
                                job.error_summary = f"CI status unknown after {CI_UNKNOWN_MAX_ATTEMPTS} checks — failing closed"
                                await _log_transition(
                                    db, job, old, JobStatus.CI_FAILED.value,
                                    f"CI status unknown after {CI_UNKNOWN_MAX_ATTEMPTS} checks — failing closed: {job.pr_url}",
                                )
                                print(f"  [{job.target_repo}] -> CI_FAILED (unknown after {CI_UNKNOWN_MAX_ATTEMPTS} checks): {job.pr_url}")
                            else:
                                # Hold at PR_OPENED, log for attempt counting
                                job.status = JobStatus.PR_OPENED.value
                                await _log_transition(
                                    db, job, old, JobStatus.PR_OPENED.value,
                                    f"CI status unknown, holding at PR_OPENED (attempt {ci_unknown_count + 1}/{CI_UNKNOWN_MAX_ATTEMPTS}): {job.pr_url}",
                                )
                                print(f"  [{job.target_repo}] -> PR_OPENED (CI unknown, attempt {ci_unknown_count + 1}/{CI_UNKNOWN_MAX_ATTEMPTS}): {job.pr_url}")
                        else:
                            job.status = JobStatus.CI_FAILED.value
                            job.error_summary = f"CI status: {ci_status}"
                            await _log_transition(
                                db, job, old, JobStatus.CI_FAILED.value,
                                f"PR exists but CI failed ({ci_status}): {job.pr_url}",
                            )
                            print(f"  [{job.target_repo}] -> CI_FAILED ({ci_status}): {job.pr_url}")
                    else:
                        # Post-execution path validation
                        pr_changed_files = (structured_output or {}).get("changed_files", [])
                        if not pr_changed_files and job.pr_url:
                            pr_changed_files = await _fetch_pr_changed_files(job.pr_url)
                        if pr_changed_files:
                            path_violations = guardrails.validate_paths(pr_changed_files)
                            if path_violations:
                                job.status = JobStatus.NEEDS_HUMAN.value
                                job.error_summary = f"PR touches protected paths: {'; '.join(path_violations)}"
                                await _log_transition(
                                    db, job, old, JobStatus.NEEDS_HUMAN.value,
                                    f"Post-execution path violation: {'; '.join(path_violations)}",
                                )
                                print(f"  [{job.target_repo}] -> NEEDS_HUMAN (protected path): {path_violations}")
                                continue
                        elif guardrails.protected_paths:
                            # Fail closed: cannot verify changed files against
                            # protected paths — require human review.
                            job.status = JobStatus.NEEDS_HUMAN.value
                            job.error_summary = "Cannot verify PR changed files against protected paths"
                            await _log_transition(
                                db, job, old, JobStatus.NEEDS_HUMAN.value,
                                "Path validation fail-closed: changed files unavailable",
                            )
                            print(f"  [{job.target_repo}] -> NEEDS_HUMAN (changed files unavailable for path check)")
                            continue

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
