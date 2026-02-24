"""Pydantic schemas for agent session endpoints."""

from __future__ import annotations

import enum
from datetime import datetime
from pydantic import BaseModel, Field


class PriorityEnum(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SessionCreate(BaseModel):
    team_id: str
    agent_name: str
    model: str = "devin-default"
    priority: PriorityEnum
    prompt: str | None = None
    tags: str | None = None


class TokenUsageRecord(BaseModel):
    timestamp: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost: float


class UsageBlock(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_tokens: int


class BillingBlock(BaseModel):
    total: float


class SessionResponse(BaseModel):
    session_id: str
    team_id: str
    agent_name: str
    model: str
    status: str
    priority: str | None = None
    usage: UsageBlock
    billing: BillingBlock
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float
    error_message: str | None
    tags: str | None

    model_config = {"from_attributes": True}


class SessionUpdate(BaseModel):
    status: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    total_cost: float | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    error_message: str | None = None


class SessionStats(BaseModel):
    total_sessions: int
    active_sessions: int
    completed_sessions: int
    failed_sessions: int
    total_tokens: int
    total_cost: float
    avg_duration_seconds: float
    success_rate: float


class TeamResponse(BaseModel):
    id: str
    name: str
    plan: str
    monthly_budget: float
    created_at: datetime
    total_sessions: int = 0
    session_count: int = 0
    total_cost: float = 0.0

    model_config = {"from_attributes": True}


class TokenUsageStats(BaseModel):
    period: str
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int
    total_cost: float
    breakdown_by_model: list[dict]
