"""Tests for the impact mapping module."""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.database import Base
from src.entities.usage_request import UsageRequest
from propagate.impact import compute_impact_sets


test_engine = create_async_engine("sqlite+aiosqlite:///", echo=False)
TestSession = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def dispose_test_engine():
    yield
    await test_engine.dispose()


@pytest_asyncio.fixture
async def db():
    async with TestSession() as session:
        yield session


class TestComputeImpactSets:
    @pytest.mark.asyncio
    async def test_finds_recent_callers(self, db):
        now = datetime.now(timezone.utc)
        db.add(UsageRequest(
            ts=now - timedelta(days=1),
            caller_service="billing-service",
            method="POST",
            route_template="/api/v1/sessions",
            status_code=201,
            duration_ms=50.0,
        ))
        await db.commit()

        impacts = await compute_impact_sets(db, ["POST /api/v1/sessions"])
        assert len(impacts) == 1
        assert impacts[0].caller_service == "billing-service"
        assert impacts[0].method == "POST"
        assert impacts[0].calls_last_7d == 1

    @pytest.mark.asyncio
    async def test_ignores_old_data(self, db):
        old_ts = datetime.now(timezone.utc) - timedelta(days=10)
        db.add(UsageRequest(
            ts=old_ts,
            caller_service="old-service",
            method="GET",
            route_template="/api/v1/sessions",
            status_code=200,
            duration_ms=10.0,
        ))
        await db.commit()

        impacts = await compute_impact_sets(db, ["GET /api/v1/sessions"])
        assert len(impacts) == 0

    @pytest.mark.asyncio
    async def test_ignores_unknown_caller(self, db):
        now = datetime.now(timezone.utc)
        db.add(UsageRequest(
            ts=now - timedelta(hours=1),
            caller_service="unknown",
            method="GET",
            route_template="/api/v1/sessions",
            status_code=200,
            duration_ms=5.0,
        ))
        await db.commit()

        impacts = await compute_impact_sets(db, ["GET /api/v1/sessions"])
        assert len(impacts) == 0

    @pytest.mark.asyncio
    async def test_groups_by_caller(self, db):
        now = datetime.now(timezone.utc)
        for _ in range(3):
            db.add(UsageRequest(
                ts=now - timedelta(hours=1),
                caller_service="billing-service",
                method="POST",
                route_template="/api/v1/sessions",
                status_code=201,
                duration_ms=20.0,
            ))
        await db.commit()

        impacts = await compute_impact_sets(db, ["POST /api/v1/sessions"])
        assert len(impacts) == 1
        assert impacts[0].calls_last_7d == 3

    @pytest.mark.asyncio
    async def test_empty_routes(self, db):
        impacts = await compute_impact_sets(db, [])
        assert impacts == []

    @pytest.mark.asyncio
    async def test_no_callers(self, db):
        impacts = await compute_impact_sets(db, ["DELETE /api/v1/nonexistent"])
        assert impacts == []

    @pytest.mark.asyncio
    async def test_declared_dependent_always_included(self, db):
        """Services in the service map are impacted even with zero telemetry."""
        impacts = await compute_impact_sets(
            db,
            ["GET /api/v1/sessions/stats"],
            declared_dependents={"billing-service", "dashboard-service"},
        )
        callers = {imp.caller_service for imp in impacts}
        assert "billing-service" in callers
        assert "dashboard-service" in callers
        for imp in impacts:
            assert imp.calls_last_7d == 0

    @pytest.mark.asyncio
    async def test_declared_dependent_enriched_by_telemetry(self, db):
        """When a declared dependent also has telemetry, call count is populated."""
        now = datetime.now(timezone.utc)
        for _ in range(5):
            db.add(UsageRequest(
                ts=now - timedelta(hours=1),
                caller_service="billing-service",
                method="GET",
                route_template="/api/v1/sessions/stats",
                status_code=200,
                duration_ms=10.0,
            ))
        await db.commit()

        impacts = await compute_impact_sets(
            db,
            ["GET /api/v1/sessions/stats"],
            declared_dependents={"billing-service"},
        )
        assert len(impacts) == 1
        assert impacts[0].caller_service == "billing-service"
        assert impacts[0].calls_last_7d == 5

    @pytest.mark.asyncio
    async def test_telemetry_caller_not_in_map_still_included(self, db):
        """A service calling the API but not in the service map is still surfaced."""
        now = datetime.now(timezone.utc)
        db.add(UsageRequest(
            ts=now, caller_service="unknown-svc", method="GET",
            route_template="/api/v1/teams", status_code=200, duration_ms=5.0,
        ))
        await db.commit()

        impacts = await compute_impact_sets(
            db,
            ["GET /api/v1/teams"],
            declared_dependents=set(),
        )
        callers = {imp.caller_service for imp in impacts}
        assert "unknown-svc" in callers

    @pytest.mark.asyncio
    async def test_multiple_routes(self, db):
        now = datetime.now(timezone.utc)
        db.add(UsageRequest(
            ts=now, caller_service="svc-a", method="POST",
            route_template="/api/v1/sessions", status_code=201, duration_ms=10.0,
        ))
        db.add(UsageRequest(
            ts=now, caller_service="svc-b", method="GET",
            route_template="/api/v1/teams", status_code=200, duration_ms=5.0,
        ))
        await db.commit()

        impacts = await compute_impact_sets(db, [
            "POST /api/v1/sessions",
            "GET /api/v1/teams",
        ])
        # 2 services × 2 routes = 4 records; each service is surfaced for all
        # changed routes regardless of which specific route they called.
        assert len(impacts) == 4
        callers = {i.caller_service for i in impacts}
        assert callers == {"svc-a", "svc-b"}
