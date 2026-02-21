"""Pydantic schemas for contract management endpoints."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class ContractCurrentResponse(BaseModel):
    version_hash: str
    content: dict
    captured_at: datetime | None = None


class ImpactSetResponse(BaseModel):
    id: int
    caller_service: str
    route_template: str
    calls_last_7d: int
    confidence: str
    notes: str | None = None

    model_config = {"from_attributes": True}


class AuditLogEntry(BaseModel):
    id: int
    job_id: int
    old_status: str | None = None
    new_status: str
    changed_at: datetime
    detail: str | None = None

    model_config = {"from_attributes": True}


class RemediationJobResponse(BaseModel):
    job_id: int
    target_repo: str
    status: str
    devin_run_id: str | None = None
    pr_url: str | None = None
    created_at: datetime
    updated_at: datetime
    bundle_hash: str | None = None
    error_summary: str | None = None
    audit_entries: list[AuditLogEntry] = []

    model_config = {"from_attributes": True}


class ContractChangeResponse(BaseModel):
    id: int
    base_ref: str | None = None
    head_ref: str | None = None
    created_at: datetime
    is_breaking: bool
    severity: str
    summary_json: str
    changed_routes_json: str
    changed_fields_json: str | None = None
    affected_services: int = 0
    remediation_status: str = "pending"

    model_config = {"from_attributes": True}


class ContractChangeDetailResponse(BaseModel):
    id: int
    base_ref: str | None = None
    head_ref: str | None = None
    created_at: datetime
    is_breaking: bool
    severity: str
    summary_json: str
    changed_routes_json: str
    changed_fields_json: str | None = None
    impact_sets: list[ImpactSetResponse] = []
    remediation_jobs: list[RemediationJobResponse] = []

    model_config = {"from_attributes": True}
