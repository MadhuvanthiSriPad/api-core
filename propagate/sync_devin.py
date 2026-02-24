"""Sync live Devin sessions into remediation_jobs for dashboard fallback."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from propagate.devin_client import DevinClient
from propagate.service_map import load_service_map
from src.database import async_session
from src.entities.audit_log import AuditLog
from src.entities.contract_change import ContractChange
from src.entities.remediation_job import RemediationJob, JobStatus

logger = logging.getLogger(__name__)

ACTIVE_DEVIN_STATUSES = {"queued", "created", "pending", "starting", "running", "working", "in_progress"}
TERMINAL_DEVIN_STATUSES = {"stopped", "completed", "succeeded", "failed", "error", "blocked", "cancelled"}
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
SESSION_URL_RE = re.compile(r"https://app\.devin\.ai/sessions?/([^/?#]+)")
BREAKING_CHANGE_RE = re.compile(r"^\*\*Breaking Change\*\*:\s*(.+)$", re.MULTILINE)


def _normalize_repo_url(value: str | None) -> str | None:
    if not value:
        return None
    repo = value.strip()
    if not repo:
        return None
    if repo.startswith("git@github.com:"):
        repo = "https://github.com/" + repo.split(":", 1)[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    match = re.match(r"^https://github\.com/([^/]+)/([^/#?]+)", repo)
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


def _repo_from_pr_url(pr_url: str | None) -> str | None:
    if not pr_url:
        return None
    match = re.match(r"^https://github\.com/([^/]+)/([^/]+)/pull/\d+", pr_url.strip())
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}"


def _session_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = SESSION_URL_RE.search(url.strip())
    if match:
        return match.group(1)
    return None


def _extract_session_url(payload: dict[str, Any]) -> str | None:
    for key in ("session_url", "app_url", "web_url", "url"):
        value = payload.get(key)
        if isinstance(value, str) and "app.devin.ai" in value:
            return value.strip()

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        for key in ("session_url", "app_url", "web_url", "url"):
            value = structured.get(key)
            if isinstance(value, str) and "app.devin.ai" in value:
                return value.strip()
    return None


def _extract_session_id(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "run_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    session_id_from_url = _session_id_from_url(_extract_session_url(payload))
    if session_id_from_url:
        return session_id_from_url

    # Some Devin APIs return "id". Keep this as a guarded fallback.
    raw_id = payload.get("id")
    if isinstance(raw_id, str):
        candidate = raw_id.strip()
        if candidate and not candidate.isdigit():
            return candidate
    return None


def _extract_lookup_id(payload: dict[str, Any]) -> str | None:
    """Extract an identifier suitable for querying Devin session detail."""
    for key in ("session_id", "run_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _extract_session_id(payload)


def _session_lookup_id_from_ref(run_ref: str | None) -> str | None:
    if not run_ref:
        return None
    stripped = run_ref.strip()
    if not stripped:
        return None
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return _session_id_from_url(stripped)
    return stripped


def _extract_status(payload: dict[str, Any]) -> str:
    for key in ("status_enum", "status", "state"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "unknown"


def _extract_pr_url(payload: dict[str, Any]) -> str | None:
    candidate_keys = ("pr_url", "pull_request_url")
    for key in candidate_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    pr_block = payload.get("pull_request")
    if isinstance(pr_block, dict):
        for key in ("url", "html_url"):
            value = pr_block.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        pr_obj = structured.get("pull_request")
        if isinstance(pr_obj, dict):
            for key in ("url", "html_url"):
                value = pr_obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        text_hint = json.dumps(structured)
        match = PR_URL_RE.search(text_hint)
        if match:
            return match.group(0)

    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        match = PR_URL_RE.search(prompt)
        if match:
            return match.group(0)

    return None


def _extract_repo_url(payload: dict[str, Any], pr_url: str | None) -> str | None:
    repo_keys = ("target_repo", "repo", "repo_url", "repository", "github_repo")
    for key in repo_keys:
        value = payload.get(key)
        if isinstance(value, str):
            repo = _normalize_repo_url(value)
            if repo:
                return repo

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in repo_keys:
            value = metadata.get(key)
            if isinstance(value, str):
                repo = _normalize_repo_url(value)
                if repo:
                    return repo

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        for key in repo_keys:
            value = structured.get(key)
            if isinstance(value, str):
                repo = _normalize_repo_url(value)
                if repo:
                    return repo

    return _repo_from_pr_url(pr_url)


def _extract_change_description(payload: dict[str, Any]) -> str | None:
    candidate_keys = (
        "change_summary",
        "summary",
        "title",
        "task_title",
        "problem_statement",
        "description",
    )
    for key in candidate_keys:
        value = payload.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text[:220]

    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        for key in candidate_keys:
            value = structured.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text[:220]

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in candidate_keys:
            value = metadata.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text[:220]

    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        match = BREAKING_CHANGE_RE.search(prompt)
        if match:
            return match.group(1).strip()[:220]
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        if first_line:
            return first_line[:220]
    return None


def _map_devin_status_to_job_status(devin_status: str, has_pr: bool) -> str:
    status = devin_status.lower()
    if status in {"queued", "created", "pending", "starting"}:
        return JobStatus.QUEUED.value
    if status in {"running", "working", "in_progress"}:
        return JobStatus.RUNNING.value
    if status == "blocked":
        return JobStatus.NEEDS_HUMAN.value
    if status in {"failed", "error", "cancelled"}:
        return JobStatus.CI_FAILED.value
    if status in {"stopped", "completed", "succeeded"}:
        return JobStatus.PR_OPENED.value if has_pr else JobStatus.NEEDS_HUMAN.value
    return JobStatus.PR_OPENED.value if has_pr else JobStatus.RUNNING.value


async def _log_transition(
    db: AsyncSession,
    job: RemediationJob,
    old_status: str | None,
    new_status: str,
    detail: str | None,
) -> None:
    db.add(
        AuditLog(
            job_id=job.job_id,
            old_status=old_status,
            new_status=new_status,
            detail=detail,
        )
    )


def _dedupe_jobs_by_repo(jobs: list[RemediationJob]) -> list[RemediationJob]:
    by_repo: dict[str, RemediationJob] = {}
    ordered = sorted(
        jobs,
        key=lambda job: job.updated_at or job.created_at,
        reverse=True,
    )
    for job in ordered:
        repo_key = job.target_repo or f"job-{job.job_id}"
        if repo_key not in by_repo:
            by_repo[repo_key] = job
    return list(by_repo.values())


def _build_sync_summary(jobs: list[RemediationJob], change_descriptions: list[str]) -> str:
    if not jobs:
        return "Live Devin sync: no tracked sessions"

    repos = sorted(
        {
            (job.target_repo or "").rstrip("/").split("/")[-1]
            for job in jobs
            if job.target_repo
        }
    )
    pr_count = len({job.pr_url for job in jobs if job.pr_url})
    active_count = sum(
        1 for job in jobs if job.status in {JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.PR_OPENED.value}
    )

    repos_label = ", ".join(repos) if repos else "unknown-repo"
    descriptor = ""
    if change_descriptions:
        ordered = sorted({d.strip() for d in change_descriptions if d and d.strip()})
        if ordered:
            first = ordered[0]
            if len(ordered) > 1:
                descriptor = f"{first} (+{len(ordered) - 1} more)"
            else:
                descriptor = first

    if descriptor:
        return f"{descriptor} | live sync: {repos_label} | {pr_count} open PR(s), {active_count} active session(s)"
    return f"Live Devin sync: {repos_label} | {pr_count} open PR(s), {active_count} active session(s)"


async def _ensure_sync_change_row(db: AsyncSession) -> ContractChange:
    result = await db.execute(
        select(ContractChange)
        .where(ContractChange.head_ref == "devin-live-sync")
        .order_by(ContractChange.created_at.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    fallback_change = ContractChange(
        base_ref="devin-live-sync",
        head_ref="devin-live-sync",
        is_breaking=False,
        severity="low",
        summary_json=json.dumps({"summary": "Live Devin sync: no tracked sessions"}),
        changed_routes_json=json.dumps([]),
        changed_fields_json=json.dumps([]),
    )
    db.add(fallback_change)
    await db.flush()
    return fallback_change


async def _sync_with_db(
    db: AsyncSession,
    *,
    limit: int,
    include_terminal: bool,
) -> dict[str, int]:
    client = DevinClient()
    service_map = load_service_map()
    known_repos = {_normalize_repo_url(info.repo) for info in service_map.values()}
    known_repos.discard(None)
    # Match by owner/repo slug (e.g. "org/billing-service") for tighter filtering
    known_repo_slugs = {
        "/".join(repo.split("/")[-2:]) for repo in known_repos
    }
    sync_change = await _ensure_sync_change_row(db)

    existing_sync_jobs = await db.execute(
        select(RemediationJob).where(RemediationJob.change_id == sync_change.id)
    )
    sync_job_by_repo: dict[str, RemediationJob] = {}
    for existing_job in sorted(
        list(existing_sync_jobs.scalars().all()),
        key=lambda job: job.updated_at or job.created_at,
        reverse=True,
    ):
        if existing_job.target_repo and existing_job.target_repo not in sync_job_by_repo:
            sync_job_by_repo[existing_job.target_repo] = existing_job

    summaries = await client.list_sessions(limit=limit)
    imported = 0
    updated = 0
    skipped = 0
    change_descriptions: list[str] = []

    for summary in summaries:
        if not isinstance(summary, dict):
            skipped += 1
            continue

        summary_lookup_id = _extract_lookup_id(summary)
        if not summary_lookup_id:
            skipped += 1
            continue

        try:
            detail = await client.get_session(summary_lookup_id)
        except Exception as exc:
            logger.warning("Failed to fetch Devin session %s: %s", summary_lookup_id, exc)
            skipped += 1
            continue

        if not isinstance(detail, dict):
            skipped += 1
            continue

        summary_session_id = _extract_session_id(summary) or summary_lookup_id
        session_id = _extract_session_id(detail) or summary_session_id
        if not session_id:
            skipped += 1
            continue
        session_url = _extract_session_url(detail) or _extract_session_url(summary)
        session_ref = session_url or session_id

        status = _extract_status(detail)
        if not include_terminal and status in TERMINAL_DEVIN_STATUSES:
            skipped += 1
            continue

        pr_url = _extract_pr_url(detail) or _extract_pr_url(summary)
        repo_url = _extract_repo_url(detail, pr_url) or _extract_repo_url(summary, pr_url)
        change_description = _extract_change_description(detail) or _extract_change_description(summary)
        if change_description:
            change_descriptions.append(change_description)

        if repo_url:
            repo_slug = "/".join(repo_url.split("/")[-2:])
            if repo_url not in known_repos and repo_slug not in known_repo_slugs:
                skipped += 1
                continue
        elif not pr_url:
            skipped += 1
            continue

        if status not in ACTIVE_DEVIN_STATUSES and status not in TERMINAL_DEVIN_STATUSES and not pr_url:
            skipped += 1
            continue

        lookup_candidates = {c for c in (session_id, session_url) if c}
        existing_result = await db.execute(
            select(RemediationJob).where(RemediationJob.devin_run_id.in_(lookup_candidates))
        )
        job = existing_result.scalar_one_or_none()

        if job is None:
            all_jobs_result = await db.execute(
                select(RemediationJob).where(RemediationJob.devin_run_id.isnot(None))
            )
            for candidate_job in all_jobs_result.scalars().all():
                candidate_lookup = _session_lookup_id_from_ref(candidate_job.devin_run_id)
                if candidate_lookup and candidate_lookup == session_id:
                    job = candidate_job
                    break

        if job is None and pr_url:
            job_by_pr_result = await db.execute(
                select(RemediationJob).where(RemediationJob.pr_url == pr_url).limit(1)
            )
            job = job_by_pr_result.scalar_one_or_none()

        if job is None and repo_url:
            job = sync_job_by_repo.get(repo_url)

        if job is None:
            resolved_repo = repo_url or _repo_from_pr_url(pr_url)
            if not resolved_repo:
                logger.warning("Skipping Devin session %s — cannot determine target repo", session_ref)
                skipped += 1
                continue
            job = RemediationJob(
                change_id=sync_change.id,
                target_repo=resolved_repo,
                status=JobStatus.QUEUED.value,
                devin_run_id=session_ref,
                pr_url=pr_url,
                bundle_hash=None,
                error_summary=None,
            )
            db.add(job)
            await db.flush()
            await _log_transition(
                db,
                job,
                None,
                JobStatus.QUEUED.value,
                "Imported from Devin live session sync",
            )
            imported += 1
            if job.target_repo:
                sync_job_by_repo[job.target_repo] = job
        else:
            if not job.devin_run_id or job.devin_run_id != session_ref:
                job.devin_run_id = session_ref
                updated += 1
            if repo_url and (not job.target_repo or "unknown/unknown" in job.target_repo):
                job.target_repo = repo_url
                updated += 1
            if job.change_id == sync_change.id and job.target_repo:
                sync_job_by_repo[job.target_repo] = job

        if pr_url and pr_url != job.pr_url:
            job.pr_url = pr_url
            updated += 1

        mapped_status = _map_devin_status_to_job_status(status, has_pr=bool(job.pr_url))
        if mapped_status != job.status:
            old = job.status
            job.status = mapped_status
            await _log_transition(
                db,
                job,
                old,
                mapped_status,
                f"Synced from Devin status={status}",
            )
            updated += 1

        job.updated_at = datetime.now(timezone.utc)

    sync_jobs_result = await db.execute(
        select(RemediationJob).where(RemediationJob.change_id == sync_change.id)
    )
    sync_jobs = _dedupe_jobs_by_repo(list(sync_jobs_result.scalars().all()))
    summary_text = _build_sync_summary(sync_jobs, change_descriptions)
    summary_json = json.dumps({"summary": summary_text})
    if sync_change.summary_json != summary_json:
        sync_change.summary_json = summary_json
        updated += 1
    sync_change.severity = "low"
    sync_change.is_breaking = False
    sync_change.changed_routes_json = json.dumps([])
    sync_change.changed_fields_json = json.dumps(
        sorted(
            {
                (job.target_repo or "").rstrip("/").split("/")[-1]
                for job in sync_jobs
                if job.target_repo
            }
        )
    )

    await db.commit()
    return {
        "scanned": len(summaries),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
    }


async def sync_devin_sessions(
    *,
    db: AsyncSession | None = None,
    limit: int = 50,
    include_terminal: bool = True,
) -> dict[str, int]:
    """Sync Devin sessions into remediation jobs.

    If an AsyncSession is passed, it is reused; otherwise a temporary one is created.
    """
    if db is not None:
        return await _sync_with_db(db, limit=limit, include_terminal=include_terminal)

    async with async_session() as own_db:
        return await _sync_with_db(own_db, limit=limit, include_terminal=include_terminal)


async def run_sync_job(
    *,
    limit: int = 50,
    include_terminal: bool = True,
) -> dict[str, int]:
    """Run one sync cycle and return counters."""
    result = await sync_devin_sessions(limit=limit, include_terminal=include_terminal)
    logger.info(
        "Devin sync job: scanned=%s imported=%s updated=%s skipped=%s",
        result["scanned"],
        result["imported"],
        result["updated"],
        result["skipped"],
    )
    return result


async def run_sync_loop(
    *,
    interval_seconds: int = 45,
    limit: int = 50,
    include_terminal: bool = True,
) -> None:
    """Continuously sync Devin sessions into remediation_jobs."""
    while True:
        try:
            await run_sync_job(limit=limit, include_terminal=include_terminal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Devin sync loop iteration failed: %s", exc)
        await asyncio.sleep(max(interval_seconds, 5))


def cli() -> None:
    parser = argparse.ArgumentParser(description="Sync live Devin sessions into remediation_jobs")
    parser.add_argument("--limit", type=int, default=50, help="Max sessions to inspect")
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Only import active sessions (skip terminal Devin sessions)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously as a background sync worker",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=45,
        help="Polling interval in seconds when --watch is enabled",
    )
    args = parser.parse_args()

    include_terminal = not args.active_only
    if args.watch:
        asyncio.run(
            run_sync_loop(
                interval_seconds=args.interval,
                limit=args.limit,
                include_terminal=include_terminal,
            )
        )
        return

    result = asyncio.run(run_sync_job(limit=args.limit, include_terminal=include_terminal))
    print("Devin sync complete")
    print(f"  scanned:  {result['scanned']}")
    print(f"  imported: {result['imported']}")
    print(f"  updated:  {result['updated']}")
    print(f"  skipped:  {result['skipped']}")


if __name__ == "__main__":
    cli()
