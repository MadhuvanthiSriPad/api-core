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


def _parse_pr_url(pr_url: str) -> tuple[str, str, str] | None:
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url or "")
    if not match:
        return None
    return match.group(1), match.group(2)


async def _fetch_github_pr_metadata(pr_url: str) -> dict[str, str | bool]:
    """Fetch GitHub PR metadata needed to validate active PR attachment."""
    github_token = settings.github_token
    parsed = _parse_pr_url(pr_url)
    if not github_token or not parsed:
        return {"state": "unknown", "merged": False, "head_sha": "", "head_ref": "", "title": "", "author_login": ""}

    owner, repo, pr_number = parsed

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            pr_resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if pr_resp.status_code != 200:
                return {"state": "unknown", "merged": False, "head_sha": "", "head_ref": "", "title": "", "author_login": ""}

            payload = pr_resp.json()
            return {
                "state": str(payload.get("state") or "unknown"),
                "merged": bool(payload.get("merged") or False),
                "head_sha": str(payload.get("head", {}).get("sha") or ""),
                "head_ref": str(payload.get("head", {}).get("ref") or ""),
                "title": str(payload.get("title") or ""),
                "author_login": str(payload.get("user", {}).get("login") or ""),
            }
    except Exception as e:
        logger.warning("GitHub PR metadata fetch failed: %s", e)
        return {"state": "unknown", "merged": False, "head_sha": "", "head_ref": "", "title": "", "author_login": ""}


async def _find_replacement_open_pr(
    repo_url_or_pr_url: str,
    *,
    preferred_head_ref: str = "",
    preferred_title: str = "",
    preferred_author_login: str = "",
    exclude_pr_url: str = "",
) -> str | None:
    """Find an active open PR when a previously attached PR has gone stale."""
    github_token = settings.github_token
    parsed_pr = _parse_pr_url(repo_url_or_pr_url)
    if parsed_pr:
        owner, repo, _ = parsed_pr
    else:
        parsed_repo = _parse_repo_url(repo_url_or_pr_url)
        if not parsed_repo:
            return None
        owner, repo = parsed_repo

    if not github_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                params={"state": "open", "per_page": 20},
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if not isinstance(payload, list):
                return None

            candidates: list[dict] = []
            for pr in payload:
                pr_url = str(pr.get("html_url") or "")
                if not pr_url or pr_url == exclude_pr_url:
                    continue
                candidates.append(pr)

            if not candidates:
                return None

            if preferred_head_ref:
                for pr in candidates:
                    if str(pr.get("head", {}).get("ref") or "") == preferred_head_ref:
                        return str(pr.get("html_url") or "") or None

            if preferred_title:
                for pr in candidates:
                    if str(pr.get("title") or "") == preferred_title:
                        return str(pr.get("html_url") or "") or None

            if preferred_author_login:
                author_matches = [
                    pr for pr in candidates
                    if str(pr.get("user", {}).get("login") or "") == preferred_author_login
                ]
                if len(author_matches) == 1:
                    return str(author_matches[0].get("html_url") or "") or None

            # Fall back to the most recently created open PR.
            return str(candidates[0].get("html_url") or "") or None
    except Exception as e:
        logger.warning("GitHub replacement PR lookup failed: %s", e)
    return None


async def _fetch_github_ci_status(pr_url: str) -> tuple[bool, str]:
    """Fetch CI status from GitHub Checks API as a fallback.

    Returns (ci_passed, ci_status_string).
    """
    github_token = settings.github_token
    if not github_token or not pr_url:
        return False, "unknown"

    parsed = _parse_pr_url(pr_url)
    if not parsed:
        return False, "unknown"

    owner, repo, _pr_number = parsed

    try:
        metadata = await _fetch_github_pr_metadata(pr_url)
        if metadata["state"] == "closed" and not metadata["merged"]:
            return False, "closed"
        if metadata["merged"]:
            return True, "merged"

        head_sha = str(metadata["head_sha"] or "")
        if not head_sha:
            return False, "unknown"

        async with httpx.AsyncClient(timeout=15.0) as client:
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

    parsed = _parse_pr_url(pr_url)
    if not parsed:
        return []

    owner, repo, pr_number = parsed

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
    try:
        client = DevinClient()
    except ValueError:
        client = None
        logger.warning("Devin API key not configured — skipping Devin polling, GitHub-only mode")
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
            if job.devin_run_id and client is not None:
                try:
                    status = await client.get_session(job.devin_run_id)
                except Exception as e:
                    if "Authentication failed" in str(e):
                        emit(f"  Devin API auth failed — skipping remaining polls")
                        break
                    logger.warning("Failed to poll %s: %s", job.devin_run_id, e)
                    emit(f"  [{job.target_repo}] poll error: {e}")

            devin_status = status.get("status_enum", "")
            structured_output = status.get("structured_output", {})
            if not isinstance(structured_output, dict):
                structured_output = {}

            dirty = False
            candidate_pr_url = ""
            candidate_pr_metadata: dict[str, str | bool] = {
                "state": "unknown",
                "merged": False,
                "head_sha": "",
            }
            if structured_output:
                pr_info = structured_output.get("pull_request")
                if isinstance(pr_info, dict):
                    candidate_pr_url = pr_info.get("url", "")
                    candidate_pr_metadata = await _fetch_github_pr_metadata(candidate_pr_url)
                    attach_pr = bool(candidate_pr_url) and not (
                        candidate_pr_metadata["state"] == "closed" and not candidate_pr_metadata["merged"]
                    )
                    next_pr_url = candidate_pr_url if attach_pr else None
                    if job.pr_url != next_pr_url:
                        job.pr_url = next_pr_url
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

            if devin_status in ("blocked", "stopped") or (not devin_status and client is None):
                pr_state_url = candidate_pr_url or job.pr_url or ""
                pr_state_metadata = (
                    candidate_pr_metadata
                    if candidate_pr_url
                    else await _fetch_github_pr_metadata(pr_state_url)
                    if pr_state_url
                    else {"state": "unknown", "merged": False, "head_sha": ""}
                )
                if pr_state_url and pr_state_metadata["state"] == "closed" and not pr_state_metadata["merged"]:
                    replacement_pr_url = await _find_replacement_open_pr(
                        pr_state_url,
                        preferred_head_ref=str(pr_state_metadata.get("head_ref") or ""),
                        preferred_title=str(pr_state_metadata.get("title") or ""),
                        preferred_author_login=str(pr_state_metadata.get("author_login") or ""),
                        exclude_pr_url=pr_state_url,
                    )
                    if replacement_pr_url:
                        if job.pr_url != replacement_pr_url:
                            job.pr_url = replacement_pr_url
                            dirty = True
                        pr_state_url = replacement_pr_url
                        pr_state_metadata = await _fetch_github_pr_metadata(pr_state_url)

                if pr_state_url and pr_state_metadata["state"] == "closed" and not pr_state_metadata["merged"]:
                    error_summary = "PR closed without merge"
                    if (
                        job.status != JobStatus.NEEDS_HUMAN.value
                        or job.error_summary != error_summary
                        or job.pr_url is not None
                    ):
                        old = job.status
                        job.status = JobStatus.NEEDS_HUMAN.value
                        job.error_summary = error_summary
                        job.pr_url = None
                        await _log_transition(
                            db,
                            job,
                            old,
                            JobStatus.NEEDS_HUMAN.value,
                            f"PR closed without merge: {pr_state_url}",
                        )
                        emit(f"  [{job.target_repo}] -> NEEDS_HUMAN (closed PR): {pr_state_url}")
                        dirty = True
                    if dirty:
                        summary["updated"] += 1
                    continue

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
                    # No PR on the job — try to discover one in the repo.
                    replacement_pr_url = await _find_replacement_open_pr(job.target_repo or "") if job.target_repo else None
                    if replacement_pr_url:
                        old = job.status
                        job.pr_url = replacement_pr_url
                        job.status = JobStatus.PR_OPENED.value
                        job.error_summary = None
                        await _log_transition(db, job, old, JobStatus.PR_OPENED.value, f"Found PR: {replacement_pr_url}")
                        emit(f"  [{job.target_repo}] -> PR_OPENED (found replacement): {replacement_pr_url}")
                        dirty = True
                    else:
                        no_pr_msg = f"Devin {devin_status} without PR"
                        if job.status != JobStatus.NEEDS_HUMAN.value or job.error_summary != no_pr_msg:
                            old = job.status
                            job.status = JobStatus.NEEDS_HUMAN.value
                            job.error_summary = no_pr_msg
                            await _log_transition(db, job, old, JobStatus.NEEDS_HUMAN.value, job.error_summary)
                            emit(f"  [{job.target_repo}] -> NEEDS_HUMAN (no PR)")
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
        if client is not None:
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
