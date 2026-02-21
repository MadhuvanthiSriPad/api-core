"""Pydantic schemas for usage telemetry endpoints."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class TopRouteResponse(BaseModel):
    route_template: str
    method: str
    total_calls: int
    unique_callers: int
    avg_duration_ms: float


class TopCallerResponse(BaseModel):
    caller_service: str
    call_count: int
    routes_called: int


class RouteCallResponse(BaseModel):
    id: int
    caller_service: str
    route_template: str
    method: str
    status_code: int
    duration_ms: float
    ts: datetime

    model_config = {"from_attributes": True}
