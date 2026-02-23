"""Contract management endpoints — view current contract, changes, and impact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
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

CONTRACT_PATH = Path(__file__).resolve().parent.parent.parent / "openapi.yaml"


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
        unique_services = {imp.caller_service for imp in change.impact_sets}
        jobs = change.remediation_jobs
        if not jobs:
            rem_status = "pending"
        elif all(j.status == "green" for j in jobs):
            rem_status = "all_green"
        elif any(j.status == "needs_human" for j in jobs):
            rem_status = "needs_human"
        else:
            rem_status = "in_progress"

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
            affected_services=len(unique_services),
            remediation_status=rem_status,
        ))
    return response


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
        impact_sets=[ImpactSetResponse.model_validate(imp) for imp in change.impact_sets],
        remediation_jobs=[RemediationJobResponse.model_validate(job) for job in change.remediation_jobs],
    )
