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


class ServiceHealthResponse(BaseModel):
    caller_service: str
    total_requests: int
    error_4xx: int
    error_5xx: int
    error_rate_pct: float
    server_error_rate_pct: float
    avg_latency_ms: float
    uptime_pct: float
    last_seen: datetime | None


class RouteErrorRateResponse(BaseModel):
    route_template: str
    method: str
    total_calls: int
    success_2xx: int
    client_errors_4xx: int
    server_errors_5xx: int
    error_rate_pct: float
    server_error_rate_pct: float
    avg_latency_ms: float


class LatencyPercentilesResponse(BaseModel):
    route_template: str
    method: str
    sample_count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_ms: float
    max_ms: float
