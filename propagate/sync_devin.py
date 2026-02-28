"""Sync Devin sessions into remediation_jobs for dashboard visibility.

Provides:
- sync_devin_sessions()  -- one-shot sync callable from the /contracts/live-jobs/sync endpoint
- run_sync_loop()        -- background coroutine started in the FastAPI lifespan
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from propagate.devin_client import DevinClient
from propagate.check_status import sync_job_statuses
from propagate.notify import emit_webhook
from propagate.service_map import load_service_map
from src.config import settings
from src.database import async_session
from src.entities.contract_change import ContractChange
from src.entities.remediation_job import RemediationJob, JobStatus

logger = logging.getLogger(__name__)

# Devin status_enum values that map to terminal job states.
_DEVIN_TERMINAL = {"stopped", "failed"}
# Prevent overlapping sync writers (background loop + manual sync endpoint).
_SYNC_MUTEX: asyncio.Lock = asyncio.Lock()

def _map_status(devin_status: str, pr_url: str | None) -> str:
    """Map Devin state to remediation job state."""
    status = (devin_status or "").lower()
    if status == "running":
        return JobStatus.RUNNING.value
    if status in {"queued", "created", "in_progress"}:
        return JobStatus.RUNNING.value
    if status == "blocked":
        if pr_url:
            return JobStatus.PR_OPENED.value
        return JobStatus.NEEDS_HUMAN.value
    if status in {"failed", "error", "cancelled"}:
        return JobStatus.CI_FAILED.value
    if status in {"stopped", "finished", "completed", "succeeded", "success"}:
        return JobStatus.PR_OPENED.value if pr_url else JobStatus.GREEN.value
    return JobStatus.RUNNING.value


def _normalize_repo_url(raw: str | None) -> str | None:
    """Normalize repo identifiers into https GitHub URLs."""
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.startswith("https://github.com/") or value.startswith("http://github.com/"):
        return value.rstrip("/").removesuffix(".git").replace("http://", "https://", 1)
    if value.startswith("github.com/"):
        return f"https://{value.rstrip('/').removesuffix('.git')}"
    if "/" in value and " " not in value and value.count("/") == 1:
        return f"https://github.com/{value.rstrip('/').removesuffix('.git')}"
    return None


def _repo_from_pr_url(pr_url: str | None) -> str | None:
    """Extract repository URL from a GitHub pull request URL."""
    if not pr_url or "github.com/" not in pr_url:
        return None
    tail = pr_url.split("github.com/", 1)[1]
    parts = tail.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    return f"https://github.com/{owner}/{repo}".rstrip("/").removesuffix(".git")


def _extract_pr_url(payload: dict) -> str | None:
    """Extract PR URL from known Devin response shapes."""
    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        pr = structured.get("pull_request")
        if isinstance(pr, dict):
            url = pr.get("url")
            if isinstance(url, str) and url:
                return url
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        url = pull_request.get("url")
        if isinstance(url, str) and url:
            return url
    for key in ("pull_request_url", "pr_url"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_prompt_summary(payload: dict) -> str:
    """Derive a concise summary from Devin session content."""
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()[:200]
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:200]
    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            text = msg.get("message")
            if isinstance(text, str) and text.strip():
                return text.strip()[:200]
    return "Live Devin remediation sync"


def _build_recovery_complete(
    change: ContractChange,
    all_jobs: list,
    service_map: dict,
) -> dict:
    """Build a recovery_complete webhook payload from a fully-green change."""
    try:
        summary = json.loads(change.summary_json or "{}").get("summary", "")
    except Exception:
        summary = ""
    try:
        changed_routes = json.loads(change.changed_routes_json or "[]")
        if not isinstance(changed_routes, list):
            changed_routes = []
    except Exception:
        changed_routes = []

    job_details = []
    for j in all_jobs:
        svc_name = None
        for sname, sinfo in service_map.items():
            if _normalize_repo_url(sinfo.repo) == j.target_repo:
                svc_name = sname
                break
        job_details.append({
            "job_id": j.job_id,
            "target_repo": j.target_repo or "",
            "target_service": svc_name or (j.target_repo or "").split("/")[-1],
            "pr_url": j.pr_url or "",
            "started_at": j.created_at.isoformat() if j.created_at else "",
            "resolved_at": j.updated_at.isoformat() if j.updated_at else "",
        })

    created_times = [j.created_at for j in all_jobs if j.created_at]
    updated_times = [j.updated_at for j in all_jobs if j.updated_at]
    mttr_seconds = 0
    if created_times and updated_times:
        mttr_seconds = max(0, int((max(updated_times) - min(created_times)).total_seconds()))

    affected_services = [d["target_service"] for d in job_details if d["target_service"]]

    return {
        "event_type": "recovery_complete",
        "change_id": change.id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "severity": change.severity or "high",
        "is_breaking": bool(change.is_breaking),
        "summary": summary,
        "affected_services": affected_services,
        "changed_routes": changed_routes,
        "total_jobs": len(all_jobs),
        "jobs": job_details,
        "mttr_seconds": mttr_seconds,
    }


async def _latest_or_create_live_change(
    db: AsyncSession,
    summary: str,
) -> ContractChange:
    result = await db.execute(
        select(ContractChange).order_by(ContractChange.created_at.desc()).limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is not None:
        return latest

    change = ContractChange(
        base_ref="devin-live-sync",
        head_ref="devin-live-sync",
        is_breaking=True,
        severity="high",
        summary_json=json.dumps({"summary": summary}),
        changed_routes_json="[]",
        changed_fields_json="[]",
    )
    db.add(change)
    await db.flush()
    return change


async def sync_devin_sessions(
    db: AsyncSession,
    limit: int = 50,
    include_terminal: bool = True,
) -> dict:
    """Fetch recent Devin sessions and upsert them as remediation jobs.

    Returns a summary dict suitable for a JSON response.
    """
    service_map = load_service_map()
    mapped_repos = {
        _normalize_repo_url(info.repo) for info in service_map.values()
        if _normalize_repo_url(info.repo)
    }

    imported = 0
    updated = 0
    skipped = 0
    sessions: list[dict] = []
    change: ContractChange | None = None
    # Collect jobs that transition to pr_opened for webhook notification.
    pr_opened_events: list[dict] = []

    try:
        client = DevinClient()
    except Exception as exc:
        return {
            "synced": 0,
            "imported": 0,
            "updated": 0,
            "skipped": 0,
            "total_fetched": 0,
            "detail": str(exc),
        }
    try:
        sessions = await client.list_sessions(limit=limit)
        async with _SYNC_MUTEX:
            for sess in sessions:
                session_id = sess.get("session_id", "")
                if not session_id:
                    skipped += 1
                    continue

                # Fetch detail for richer fields (pull_request, messages, status_enum).
                try:
                    detail = await client.get_session(session_id)
                except Exception:
                    detail = sess

                devin_status = str(
                    detail.get("status_enum") or sess.get("status_enum") or "running"
                ).lower()
                if not include_terminal and devin_status in _DEVIN_TERMINAL:
                    continue

                pr_url = _extract_pr_url(detail) or _extract_pr_url(sess)
                repo = (
                    _repo_from_pr_url(pr_url)
                    or _normalize_repo_url(detail.get("repo") or detail.get("repository"))
                    or _normalize_repo_url(sess.get("repo") or sess.get("repository"))
                )
                if repo is None or (mapped_repos and repo not in mapped_repos):
                    skipped += 1
                    continue

                if change is None:
                    with db.no_autoflush:
                        change = await _latest_or_create_live_change(
                            db=db,
                            summary=_extract_prompt_summary(detail),
                        )

                # Check if we already track this session.
                with db.no_autoflush:
                    result = await db.execute(
                        select(RemediationJob).where(RemediationJob.devin_run_id == session_id)
                    )
                    job = result.scalar_one_or_none()
                    if job is None and change is not None:
                        repo_result = await db.execute(
                            select(RemediationJob)
                            .where(RemediationJob.change_id == change.id)
                            .where(RemediationJob.target_repo == repo)
                            .order_by(RemediationJob.updated_at.desc(), RemediationJob.created_at.desc())
                            .limit(1)
                        )
                        job = repo_result.scalar_one_or_none()

                mapped = _map_status(devin_status, pr_url)

                # Resolve service name from repo URL.
                _svc_name = None
                for sname, sinfo in service_map.items():
                    if _normalize_repo_url(sinfo.repo) == repo:
                        _svc_name = sname
                        break

                if job is None:
                    job = RemediationJob(
                        change_id=change.id,
                        target_repo=repo,
                        status=mapped,
                        devin_run_id=session_id,
                        pr_url=pr_url,
                    )
                    db.add(job)
                    await db.flush()  # assign job_id before building webhook payload
                    imported += 1
                    # New job created with PR already open → notify.
                    if mapped == JobStatus.PR_OPENED.value and pr_url:
                        pr_opened_events.append({
                            "event_type": "pr_opened",
                            "change_id": change.id,
                            "job_id": job.job_id,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "target_repo": repo,
                            "target_service": _svc_name or repo.split("/")[-1],
                            "pr_url": pr_url,
                            "devin_session_url": f"{settings.devin_app_base}/sessions/{session_id}",
                            "severity": "high",
                            "is_breaking": True,
                            "summary": _extract_prompt_summary(detail),
                            "changed_routes": [],
                        })
                else:
                    # Update only when values changed to avoid unnecessary writes/locks.
                    old_status = job.status
                    dirty = False
                    if job.change_id == 0 and change is not None:
                        job.change_id = change.id
                        dirty = True
                    if session_id and job.devin_run_id != session_id:
                        job.devin_run_id = session_id
                        dirty = True
                    if repo and job.target_repo != repo:
                        job.target_repo = repo
                        dirty = True
                    if job.status != mapped:
                        job.status = mapped
                        dirty = True
                    if pr_url and job.pr_url != pr_url:
                        job.pr_url = pr_url
                        dirty = True
                    if dirty:
                        job.updated_at = datetime.now(timezone.utc)
                        updated += 1
                    # Existing job transitions to pr_opened with a new PR URL → notify.
                    if (
                        mapped == JobStatus.PR_OPENED.value
                        and pr_url
                        and old_status != JobStatus.PR_OPENED.value
                    ):
                        pr_opened_events.append({
                            "event_type": "pr_opened",
                            "change_id": change.id if change else 0,
                            "job_id": job.job_id or 0,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "target_repo": repo,
                            "target_service": _svc_name or repo.split("/")[-1],
                            "pr_url": pr_url,
                            "devin_session_url": f"{settings.devin_app_base}/sessions/{session_id}",
                            "severity": "high",
                            "is_breaking": True,
                            "summary": _extract_prompt_summary(detail),
                            "changed_routes": [],
                        })

            await db.commit()

        # Fire notification webhooks after commit (fire-and-forget).
        for evt in pr_opened_events:
            await emit_webhook("/api/v1/webhooks/pr-opened", evt)

        # If all jobs for this change are now green, fire recovery_complete.
        # notification-service deduplicates via idempotency key so safe to fire on every sync.
        if change is not None:
            all_jobs_result = await db.execute(
                select(RemediationJob).where(RemediationJob.change_id == change.id)
            )
            all_jobs = all_jobs_result.scalars().all()
            if all_jobs and all(j.status == JobStatus.GREEN.value for j in all_jobs):
                rc_payload = _build_recovery_complete(change, list(all_jobs), service_map)
                await emit_webhook("/api/v1/webhooks/recovery-complete", rc_payload)

        return {
            "synced": imported + updated,
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "total_fetched": len(sessions),
            "change_id": change.id if change else None,
        }
    except Exception as exc:
        with suppress(Exception):
            await db.rollback()
        logger.warning("Failed to sync Devin sessions: %s", exc)
        return {
            "synced": imported + updated,
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "total_fetched": len(sessions),
            "error": str(exc),
        }
    finally:
        await client.close()


async def run_sync_loop(
    interval_seconds: int = 45,
    limit: int = 50,
    include_terminal: bool = True,
) -> None:
    """Long-running background loop that periodically syncs Devin sessions."""
    logger.info(
        "Starting Devin sync loop (interval=%ds, limit=%d)", interval_seconds, limit
    )
    while True:
        try:
            async with async_session() as db:
                result = await sync_devin_sessions(
                    db=db,
                    limit=limit,
                    include_terminal=include_terminal,
                )
                status_result = await sync_job_statuses(db=db)
                logger.debug("Sync loop result: %s", result)
                logger.debug("Status sync result: %s", status_result)
        except asyncio.CancelledError:
            logger.info("Sync loop cancelled")
            raise
        except Exception:
            logger.exception("Sync loop iteration failed")

        await asyncio.sleep(interval_seconds)
