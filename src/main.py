"""API Core — core API for tracking AI sessions and contract management."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.database import init_db, async_session
from src.routes import sessions, teams, analytics, usage, contracts
from src.seed import seed_data
from src.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Seed demo data on first startup
    async with async_session() as db:
        from sqlalchemy import select, func
        from src.models.team import Team
        result = await db.execute(select(func.count(Team.id)))
        if result.scalar() == 0:
            await seed_data(db)
    yield


app = FastAPI(
    title="API Core",
    description="Core API for AgentBoard — tracks AI sessions, token usage, costs, and contract management",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from src.middleware.usage_telemetry import UsageTelemetryMiddleware
app.add_middleware(UsageTelemetryMiddleware)

app.include_router(sessions.router, prefix=settings.api_prefix)
app.include_router(teams.router, prefix=settings.api_prefix)
app.include_router(analytics.router, prefix=settings.api_prefix)
app.include_router(usage.router, prefix=settings.api_prefix)
app.include_router(contracts.router, prefix=settings.api_prefix)


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "api-core", "version": "1.0.0"}
