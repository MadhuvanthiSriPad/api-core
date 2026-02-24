"""Sync Devin sessions into remediation_jobs for dashboard visibility.

Provides:
- sync_devin_sessions()  -- one-shot sync callable from the /contracts/live-jobs/sync endpoint
- run_sync_loop()        -- background coroutine started in the FastAPI lifespan
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from propagate.devin_client import DevinClient
from src.config import settings
from src.database import async_session
from src.entities.remediation_job import RemediationJob, JobStatus

logger = logging.getLogger(__name__)

# Devin status_enum values that map to terminal job states.
_DEVIN_TERMINAL = {"stopped", "failed"}

_STATUS_MAP: dict[str, str] = {
    "running": JobStatus.RUNNING.value,
    "blocked": JobStatus.NEEDS_HUMAN.value,
    "stopped": JobStatus.GREEN.value,
    "failed": JobStatus.CI_FAILED.value,
}


def _map_status(devin_status: str) -> str:
    return _STATUS_MAP.get(devin_status, JobStatus.RUNNING.value)


async def sync_devin_sessions(
    db: AsyncSession,
    limit: int = 50,
    include_terminal: bool = True,
) -> dict:
    """Fetch recent Devin sessions and upsert them as remediation jobs.

    Returns a summary dict suitable for a JSON response.
    """
    if not settings.devin_api_key:
        return {"synced": 0, "detail": "Devin API key not configured"}

    client = DevinClient()
    try:
        sessions = await client.list_sessions(limit=limit)
    except Exception as exc:
        logger.warning("Failed to list Devin sessions: %s", exc)
        return {"synced": 0, "error": str(exc)}
    finally:
        await client.close()

    synced = 0
    for sess in sessions:
        session_id = sess.get("session_id", "")
        if not session_id:
            continue

        devin_status = sess.get("status_enum", "running")
        if not include_terminal and devin_status in _DEVIN_TERMINAL:
            continue

        # Check if we already track this session.
        result = await db.execute(
            select(RemediationJob).where(RemediationJob.devin_run_id == session_id)
        )
        job = result.scalar_one_or_none()

        mapped = _map_status(devin_status)

        # Extract PR URL from structured output if available.
        pr_url: str | None = None
        structured = sess.get("structured_output") or {}
        pr_info = structured.get("pull_request") or {}
        if pr_info.get("url"):
            pr_url = pr_info["url"]

        if job is None:
            # Only create a new row if there is an associated change.
            # Without a change_id we cannot satisfy the NOT NULL FK, so
            # link to change_id=0 as a sentinel (the dashboard filters
            # live-synced jobs separately).
            job = RemediationJob(
                change_id=0,
                target_repo=sess.get("repo", session_id),
                status=mapped,
                devin_run_id=session_id,
                pr_url=pr_url,
            )
            db.add(job)
            synced += 1
        else:
            # Update existing row.
            if job.status != mapped:
                job.status = mapped
            if pr_url and not job.pr_url:
                job.pr_url = pr_url
            job.updated_at = datetime.now(timezone.utc)
            synced += 1

    await db.commit()
    return {"synced": synced, "total_fetched": len(sessions)}


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
                logger.debug("Sync loop result: %s", result)
        except asyncio.CancelledError:
            logger.info("Sync loop cancelled")
            raise
        except Exception:
            logger.exception("Sync loop iteration failed")

        await asyncio.sleep(interval_seconds)
