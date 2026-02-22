"""API Core — tracks AI sessions, token usage, costs, and contract management."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.database import init_db
from src.middleware.usage_telemetry import UsageTelemetryMiddleware
from src.routes import sessions, teams, analytics, usage, contracts


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="API Core",
    description="Tracks AI sessions, token usage, costs, and contract management",
    version=settings.api_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(UsageTelemetryMiddleware)

app.include_router(sessions.router, prefix=settings.api_prefix)
app.include_router(teams.router, prefix=settings.api_prefix)
app.include_router(analytics.router, prefix=settings.api_prefix)
app.include_router(usage.router, prefix=settings.api_prefix)
app.include_router(contracts.router, prefix=settings.api_prefix)


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "api-core", "version": settings.api_version}
