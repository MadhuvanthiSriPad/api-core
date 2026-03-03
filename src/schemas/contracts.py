"""Pydantic schemas for contract management endpoints."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class ContractCurrentResponse(BaseModel):
    version_hash: str
    content: dict
    captured_at: datetime | None = None


class ImpactSetResponse(BaseModel):
    id: int
    caller_service: str
    route_template: str
    method: str | None = None
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
    devin_session_url: str | None = None
    pr_url: str | None = None
    notification_mode: str | None = None
    created_at: datetime
    updated_at: datetime
    bundle_hash: str | None = None
    error_summary: str | None = None
    is_dry_run: bool = False
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
    impacted_services: list[str] = Field(default_factory=list)
    target_repos: list[str] = Field(default_factory=list)
    source_repo: str = "api-core"
    active_jobs: int = 0
    pr_count: int = 0
    remediation_status: str = "pending"
    estimated_hours_saved: float = 0.0
    incident_risk_score: str = "low"

    model_config = {"from_attributes": True}


class BreakingIssueResponse(BaseModel):
    diff_type: str
    path: str
    method: str
    field: str
    detail: str
    weight: float


class SimulationResultResponse(BaseModel):
    id: int
    service_name: str
    risk_score: float
    risk_level: str
    breaking_issues: list[BreakingIssueResponse] = Field(default_factory=list)
    fields_affected: int = 0
    routes_affected: int = 0
    devin_analysis_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SimulationSummaryResponse(BaseModel):
    change_id: int
    total_services: int
    high_risk: int
    medium_risk: int
    safe: int
    simulations: list[SimulationResultResponse] = Field(default_factory=list)


class VerifySessionResponse(BaseModel):
    service_name: str
    devin_analysis_id: str | None = None
    error: str | None = None


class VerifyResponse(BaseModel):
    change_id: int
    dispatched: int
    sessions: list[VerifySessionResponse] = Field(default_factory=list)


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
    affected_services: int = 0
    affected_routes: int = 0
    total_calls_last_7d: int = 0
    impacted_services: list[str] = Field(default_factory=list)
    changed_routes: list[str] = Field(default_factory=list)
    impact_sets: list[ImpactSetResponse] = Field(default_factory=list)
    remediation_jobs: list[RemediationJobResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}
