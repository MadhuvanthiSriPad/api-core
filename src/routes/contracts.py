"""Contract management endpoints — view current contract, changes, and impact."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from propagate.check_status import sync_job_statuses
from propagate.sync_devin import sync_devin_sessions
from src.entities.contract_change import ContractChange
from src.entities.impact_set import ImpactSet
from src.entities.remediation_job import RemediationJob
from src.schemas.contracts import (
    ContractCurrentResponse,
    ContractChangeResponse,
    ContractChangeDetailResponse,
    ImpactSetResponse,
    RemediationJobResponse,
)

router = APIRouter(prefix="/contracts", tags=["contracts"])

_SYNC_COOLDOWN = 30  # seconds between sync calls
_last_sync_time: float = 0.0

CONTRACT_PATH = Path(__file__).resolve().parent.parent.parent / "openapi.yaml"
_JOB_STATUS_PRIORITY = {
    "green": 5,
    "pr_opened": 4,
    "running": 3,
    "queued": 2,
    "needs_human": 1,
    "ci_failed": 0,
}


def _dedupe_jobs_by_repo(jobs: Iterable[RemediationJob]) -> list[RemediationJob]:
    """Return the best current remediation view per repo.

    Multiple historical jobs can exist for the same repository. For dashboard
    rendering we prefer the most progressed visible state for that repo rather
    than blindly taking the latest timestamp, because stale duplicate rows can
    otherwise hide a newer PR/green state behind an older sync artifact.
    """
    by_repo: dict[str, RemediationJob] = {}

    def pr_number(job: RemediationJob) -> int:
        if not job.pr_url:
            return -1
        match = re.search(r"/pull/(\d+)", job.pr_url)
        return int(match.group(1)) if match else -1

    def sort_key(job: RemediationJob) -> tuple[int, int, int, object]:
        return (
            _JOB_STATUS_PRIORITY.get(job.status, -1),
            1 if job.pr_url else 0,
            pr_number(job),
            job.updated_at or job.created_at,
        )

    for job in jobs:
        repo_key = job.target_repo or f"job-{job.job_id}"
        current = by_repo.get(repo_key)
        if current is None or sort_key(job) > sort_key(current):
            by_repo[repo_key] = job
    return sorted(
        by_repo.values(),
        key=lambda job: (job.target_repo or "", job.updated_at or job.created_at),
        reverse=False,
    )


def _service_name_from_repo(repo_url: str) -> str:
    return repo_url.rstrip("/").split("/")[-1]


def _parse_json_string_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


@router.get("/current", response_model=ContractCurrentResponse)
async def get_current_contract():
    """Return the current openapi.yaml content and its version hash."""
    if not CONTRACT_PATH.exists():
        raise HTTPException(status_code=404, detail="openapi.yaml not found")

    content = CONTRACT_PATH.read_text()
    parsed = yaml.safe_load(content)
    version_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    return ContractCurrentResponse(
        version_hash=version_hash,
        content=parsed,
    )


@router.get("/changes", response_model=list[ContractChangeResponse])
async def list_changes(
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List recent contract changes with impact summary."""
    result = await db.execute(
        select(ContractChange)
        .options(
            selectinload(ContractChange.impact_sets),
            selectinload(ContractChange.remediation_jobs),
        )
        .order_by(ContractChange.created_at.desc())
        .limit(limit)
    )
    changes = result.scalars().all()

    response = []
    for change in changes:
        jobs = _dedupe_jobs_by_repo(change.remediation_jobs)
        unique_services = {imp.caller_service for imp in change.impact_sets}
        unique_services.update(
            _service_name_from_repo(job.target_repo)
            for job in jobs
            if job.target_repo
        )
        unique_services_list = sorted(unique_services)
        target_repos = sorted({j.target_repo for j in jobs if j.target_repo})
        active_jobs = sum(1 for j in jobs if j.status in {"queued", "running", "pr_opened"})
        pr_count = len({j.pr_url for j in jobs if j.pr_url})
        if not jobs:
            rem_status = "pending"
        elif all(j.status == "green" for j in jobs):
            rem_status = "all_green"
        elif any(j.status == "needs_human" for j in jobs):
            rem_status = "needs_human"
        else:
            rem_status = "in_progress"

        # Business value metrics: ~2-4 eng-hours per affected service
        n_services = len(unique_services_list)
        hours_saved = round(n_services * 2.5, 1) if jobs else 0.0
        risk = "critical" if change.is_breaking and n_services >= 3 else (
            "high" if change.is_breaking else (
                "medium" if n_services >= 2 else "low"
            )
        )

        response.append(ContractChangeResponse(
            id=change.id,
            base_ref=change.base_ref,
            head_ref=change.head_ref,
            created_at=change.created_at,
            is_breaking=change.is_breaking,
            severity=change.severity,
            summary_json=change.summary_json,
            changed_routes_json=change.changed_routes_json,
            changed_fields_json=change.changed_fields_json,
            affected_services=len(unique_services_list),
            impacted_services=unique_services_list,
            target_repos=target_repos,
            source_repo="api-core",
            active_jobs=active_jobs,
            pr_count=pr_count,
            remediation_status=rem_status,
            estimated_hours_saved=hours_saved,
            incident_risk_score=risk,
        ))
    return response


@router.post("/live-jobs/sync")
async def sync_live_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    include_terminal: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    """Manually sync Devin sessions into remediation jobs for dashboard fallback."""
    global _last_sync_time
    now = time.monotonic()
    if now - _last_sync_time < _SYNC_COOLDOWN:
        remaining = int(_SYNC_COOLDOWN - (now - _last_sync_time))
        raise HTTPException(status_code=429, detail=f"Sync cooldown: retry in {remaining}s")
    _last_sync_time = now
    devin_result = await sync_devin_sessions(
        db=db,
        limit=limit,
        include_terminal=include_terminal,
    )
    status_result = await sync_job_statuses(
        db=db,
        change_id=devin_result.get("change_id"),
    )
    return {
        **devin_result,
        "updated": int(devin_result.get("updated", 0)) + int(status_result.get("updated", 0)),
        "status_checked": status_result.get("checked", 0),
        "status_updated": status_result.get("updated", 0),
        "status_green": status_result.get("green", 0),
        "status_pr_opened": status_result.get("pr_opened", 0),
        "status_ci_failed": status_result.get("ci_failed", 0),
        "status_needs_human": status_result.get("needs_human", 0),
    }


@router.get("/changes/{change_id}", response_model=ContractChangeDetailResponse)
async def get_change_detail(
    change_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get full detail of a contract change including impacts and remediation jobs."""
    result = await db.execute(
        select(ContractChange)
        .options(
            selectinload(ContractChange.impact_sets),
            selectinload(ContractChange.remediation_jobs),
        )
        .where(ContractChange.id == change_id)
    )
    change = result.scalar_one_or_none()
    if not change:
        raise HTTPException(status_code=404, detail=f"Change {change_id} not found")

    jobs = _dedupe_jobs_by_repo(change.remediation_jobs)

    impacted_services = sorted(
        {imp.caller_service for imp in change.impact_sets if imp.caller_service}
    )
    changed_routes = _parse_json_string_list(change.changed_routes_json)
    if changed_routes:
        affected_routes = len(changed_routes)
    else:
        affected_routes = len(
            {
                f"{(imp.method or '').upper()} {imp.route_template}".strip()
                for imp in change.impact_sets
            }
        )
    total_calls_last_7d = sum(int(imp.calls_last_7d or 0) for imp in change.impact_sets)

    return ContractChangeDetailResponse(
        id=change.id,
        base_ref=change.base_ref,
        head_ref=change.head_ref,
        created_at=change.created_at,
        is_breaking=change.is_breaking,
        severity=change.severity,
        summary_json=change.summary_json,
        changed_routes_json=change.changed_routes_json,
        changed_fields_json=change.changed_fields_json,
        affected_services=len(impacted_services),
        affected_routes=affected_routes,
        total_calls_last_7d=total_calls_last_7d,
        impacted_services=impacted_services,
        changed_routes=changed_routes,
        impact_sets=[ImpactSetResponse.model_validate(imp) for imp in change.impact_sets],
        remediation_jobs=[RemediationJobResponse.model_validate(job) for job in jobs],
    )


@router.get("/service-graph")
async def get_service_graph():
    """Return the service dependency graph with wave structure for visualization."""
    from propagate.service_map import load_service_map
    from propagate.dependency_graph import build_dependency_graph_from_service_map

    smap = load_service_map()
    graph = build_dependency_graph_from_service_map(smap)
    waves_raw = graph.topological_sort()

    return {
        "waves": [
            {"wave": i, "services": w, "role": "source" if i == 0 else "parallel"}
            for i, w in enumerate(waves_raw)
        ],
        "edges": [
            {"from": dep, "to": svc}
            for svc, info in smap.items()
            for dep in info.depends_on
        ],
        "services": {
            name: {
                "repo": info.repo,
                "language": info.language,
                "client_paths": info.client_paths,
                "test_paths": info.test_paths,
                "frontend_paths": getattr(info, "frontend_paths", []),
            }
            for name, info in smap.items()
        },
    }


@router.get("/guardrails")
async def get_guardrails():
    """Return the current propagation guardrails configuration."""
    from propagate.guardrails import load_guardrails

    g = load_guardrails()
    return {
        "max_parallel": g.max_parallel,
        "protected_paths": g.protected_paths,
        "ci_required": g.ci_required,
        "auto_merge": g.auto_merge,
    }


@router.get("/sync-status")
async def get_sync_status():
    """Return the current Devin sync configuration status."""
    from src.config import settings

    return {
        "devin_sync_enabled": settings.devin_sync_enabled,
        "interval_seconds": settings.devin_sync_interval_seconds,
        "devin_api_configured": bool(settings.devin_api_key),
    }
