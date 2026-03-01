"""Tests for api-core endpoints using SQLite in-memory."""

import asyncio
import json

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.database import Base, get_db
from src.main import app
from src.routes import contracts as contracts_routes
from src.entities import (
    AgentSession,
    ContractChange,
    ImpactSet,
    RemediationJob,
    Team,
    TokenUsage,
    UsageRequest,
)


# Use SQLite for tests
test_engine = create_async_engine("sqlite+aiosqlite:///", echo=False)
TestSession = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSession() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db

# Enable debug mode so auth middleware allows requests without API key
from src.config import settings
settings.debug = True


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
async def seed_team():
    async with TestSession() as db:
        team = Team(id="team_test", name="Test Team", plan="pro", monthly_budget=1000.0)
        db.add(team)
        await db.commit()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["service"] == "api-core"


class TestSessions:
    @pytest.mark.asyncio
    async def test_create_session(self, client, seed_team):
        resp = await client.post("/api/v1/sessions", json={
            "team_id": "team_test",
            "agent_name": "code-reviewer",
            "model": "devin-default",
            "priority": "high",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"].startswith("sess_")
        assert data["team_id"] == "team_test"
        assert data["agent_name"] == "code-reviewer"
        assert data["status"] == "running"
        assert data["priority"] == "high"
        # Token fields are nested under usage
        assert data["usage"]["input_tokens"] == 0
        assert data["usage"]["output_tokens"] == 0
        assert data["usage"]["cached_tokens"] == 0
        # Cost is nested under billing.total
        assert data["billing"]["total"] == 0.0

    @pytest.mark.asyncio
    async def test_list_sessions(self, client, seed_team):
        # Create two sessions
        await client.post("/api/v1/sessions", json={
            "team_id": "team_test", "agent_name": "agent-a", "priority": "low",
        })
        await client.post("/api/v1/sessions", json={
            "team_id": "team_test", "agent_name": "agent-b", "priority": "medium",
        })
        resp = await client.get("/api/v1/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_get_session(self, client, seed_team):
        create = await client.post("/api/v1/sessions", json={
            "team_id": "team_test", "agent_name": "test-agent", "priority": "low",
        })
        sid = create.json()["session_id"]
        resp = await client.get(f"/api/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["agent_name"] == "test-agent"
        assert "usage" in resp.json()
        assert "billing" in resp.json()

    @pytest.mark.asyncio
    async def test_update_session(self, client, seed_team):
        create = await client.post("/api/v1/sessions", json={
            "team_id": "team_test", "agent_name": "test-agent", "priority": "high",
        })
        sid = create.json()["session_id"]
        resp = await client.patch(f"/api/v1/sessions/{sid}", json={
            "status": "completed",
            "input_tokens": 5000,
            "output_tokens": 2000,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["usage"]["input_tokens"] == 5000
        assert data["billing"]["total"] > 0

    @pytest.mark.asyncio
    async def test_record_token_usage(self, client, seed_team):
        create = await client.post("/api/v1/sessions", json={
            "team_id": "team_test", "agent_name": "test-agent", "priority": "medium",
        })
        sid = create.json()["session_id"]
        resp = await client.post(
            f"/api/v1/sessions/{sid}/tokens",
            params={"input_tokens": 1000, "output_tokens": 500},
        )
        assert resp.status_code == 201
        assert resp.json()["input_tokens"] == 1000
        assert resp.json()["cost"] > 0

        # Check session totals updated
        session = await client.get(f"/api/v1/sessions/{sid}")
        assert session.json()["usage"]["input_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_session_stats(self, client, seed_team):
        await client.post("/api/v1/sessions", json={
            "team_id": "team_test", "agent_name": "a", "priority": "low",
        })
        resp = await client.get("/api/v1/sessions/stats")
        assert resp.status_code == 200
        assert resp.json()["total_sessions"] == 1

    @pytest.mark.asyncio
    async def test_create_session_requires_priority(self, client, seed_team):
        """Creating a session without priority should fail validation."""
        resp = await client.post("/api/v1/sessions", json={
            "team_id": "team_test",
            "agent_name": "code-reviewer",
        })
        assert resp.status_code == 422  # validation error


class TestTeams:
    @pytest.mark.asyncio
    async def test_list_teams(self, client, seed_team):
        resp = await client.get("/api/v1/teams")
        assert resp.status_code == 200
        teams = resp.json()
        assert len(teams) == 1
        assert teams[0]["id"] == "team_test"

    @pytest.mark.asyncio
    async def test_get_team(self, client, seed_team):
        resp = await client.get("/api/v1/teams/team_test")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Team"

    @pytest.mark.asyncio
    async def test_get_team_includes_total_sessions_alias(self, client, seed_team):
        await client.post("/api/v1/sessions", json={
            "team_id": "team_test",
            "agent_name": "team-agent",
            "priority": "low",
        })
        resp = await client.get("/api/v1/teams/team_test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 1
        assert data["session_count"] == 1


class TestAnalytics:
    @pytest.mark.asyncio
    async def test_cost_by_team_includes_total_sessions(self, client, seed_team):
        await client.post("/api/v1/sessions", json={
            "team_id": "team_test",
            "agent_name": "analytics-agent",
            "priority": "medium",
        })
        resp = await client.get("/api/v1/analytics/cost-by-team")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["team_id"] == "team_test"
        assert rows[0]["total_sessions"] == 1
        # Backward-compatible alias retained for existing consumers.
        assert rows[0]["sessions"] == 1


class TestUsageTelemetry:
    @pytest.mark.asyncio
    async def test_top_callers_excludes_unknown(self, client):
        # Simulate legacy rows.
        async with TestSession() as db:
            db.add_all(
                [
                    UsageRequest(
                        caller_service="unknown",
                        method="GET",
                        route_template="/api/v1/sessions/stats",
                        status_code=200,
                        duration_ms=1.0,
                    ),
                    UsageRequest(
                        caller_service="billing-service",
                        method="GET",
                        route_template="/api/v1/sessions/stats",
                        status_code=200,
                        duration_ms=2.0,
                    ),
                ]
            )
            await db.commit()

        resp = await client.get("/api/v1/usage/top-callers")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload
        callers = {row["caller_service"] for row in payload}
        assert "unknown" not in callers
        assert "billing-service" in callers

    @pytest.mark.asyncio
    async def test_top_callers_respects_service_map_visibility(self, client):
        async with TestSession() as db:
            db.add_all(
                [
                    UsageRequest(
                        caller_service="dashboard-service",
                        method="GET",
                        route_template="/api/v1/sessions/stats",
                        status_code=200,
                        duration_ms=1.0,
                    ),
                    UsageRequest(
                        caller_service="dashboard-service",
                        method="GET",
                        route_template="/api/v1/teams",
                        status_code=200,
                        duration_ms=1.0,
                    ),
                    UsageRequest(
                        caller_service="billing-service",
                        method="GET",
                        route_template="/api/v1/sessions/stats",
                        status_code=200,
                        duration_ms=2.0,
                    ),
                ]
            )
            await db.commit()

        resp = await client.get("/api/v1/usage/top-callers")
        assert resp.status_code == 200
        payload = resp.json()
        callers = {row["caller_service"] for row in payload}
        assert "dashboard-service" not in callers
        assert "billing-service" in callers

    @pytest.mark.asyncio
    async def test_route_calls_excludes_unknown_by_default(self, client):
        async with TestSession() as db:
            db.add_all(
                [
                    UsageRequest(
                        caller_service="unknown",
                        method="GET",
                        route_template="/api/v1/sessions/stats",
                        status_code=200,
                        duration_ms=1.0,
                    ),
                    UsageRequest(
                        caller_service="dashboard-service",
                        method="GET",
                        route_template="/api/v1/sessions/stats",
                        status_code=200,
                        duration_ms=2.0,
                    ),
                ]
            )
            await db.commit()

        resp = await client.get("/api/v1/usage/route-calls")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload
        callers = {row["caller_service"] for row in payload}
        assert "unknown" not in callers
        assert "dashboard-service" in callers


class TestContracts:
    @pytest.mark.asyncio
    async def test_contract_change_detail_includes_blast_radius_metrics(self, client):
        async with TestSession() as db:
            change = ContractChange(
                is_breaking=True,
                severity="high",
                summary_json=json.dumps({"summary": "breaking"}),
                changed_routes_json=json.dumps([
                    "POST /api/v1/sessions",
                    "GET /api/v1/sessions",
                    "GET /api/v1/sessions/{session_id}",
                    "PATCH /api/v1/sessions/{session_id}",
                ]),
                changed_fields_json=json.dumps([
                    {"route": "POST /api/v1/sessions", "field": "priority", "change": "added (required)"}
                ]),
            )
            db.add(change)
            await db.flush()
            db.add_all(
                [
                    ImpactSet(
                        change_id=change.id,
                        caller_service="billing-service",
                        method="GET",
                        route_template="/api/v1/sessions",
                        calls_last_7d=312,
                        confidence="high",
                    ),
                    ImpactSet(
                        change_id=change.id,
                        caller_service="billing-service",
                        method="POST",
                        route_template="/api/v1/sessions",
                        calls_last_7d=245,
                        confidence="high",
                    ),
                    ImpactSet(
                        change_id=change.id,
                        caller_service="dashboard-service",
                        method="GET",
                        route_template="/api/v1/sessions/{session_id}",
                        calls_last_7d=487,
                        confidence="high",
                    ),
                    ImpactSet(
                        change_id=change.id,
                        caller_service="dashboard-service",
                        method="PATCH",
                        route_template="/api/v1/sessions/{session_id}",
                        calls_last_7d=156,
                        confidence="medium",
                    ),
                ]
            )
            await db.commit()
            change_id = change.id

        resp = await client.get(f"/api/v1/contracts/changes/{change_id}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["affected_services"] == 2
        assert data["affected_routes"] == 4
        assert data["total_calls_last_7d"] == 1200
        assert sorted(data["impacted_services"]) == ["billing-service", "dashboard-service"]
        assert len(data["changed_routes"]) == 4
        assert {row["method"] for row in data["impact_sets"]} == {"GET", "PATCH", "POST"}

    @pytest.mark.asyncio
    async def test_contract_change_detail_prefers_best_visible_job_per_repo(self, client):
        async with TestSession() as db:
            change = ContractChange(
                is_breaking=True,
                severity="high",
                summary_json=json.dumps({"summary": "repo jobs"}),
                changed_routes_json="[]",
                changed_fields_json="[]",
            )
            db.add(change)
            await db.flush()
            db.add_all(
                [
                    RemediationJob(
                        change_id=change.id,
                        target_repo="https://github.com/example/billing-service",
                        status="green",
                        pr_url="https://github.com/example/billing-service/pull/42",
                    ),
                    RemediationJob(
                        change_id=change.id,
                        target_repo="https://github.com/example/billing-service",
                        status="ci_failed",
                        pr_url="https://github.com/example/billing-service/pull/43",
                    ),
                    RemediationJob(
                        change_id=change.id,
                        target_repo="https://github.com/example/dashboard-service",
                        status="pr_opened",
                        pr_url="https://github.com/example/dashboard-service/pull/17",
                    ),
                    RemediationJob(
                        change_id=change.id,
                        target_repo="https://github.com/example/dashboard-service",
                        status="needs_human",
                        pr_url=None,
                    ),
                ]
            )
            await db.commit()
            change_id = change.id

        resp = await client.get(f"/api/v1/contracts/changes/{change_id}")
        assert resp.status_code == 200
        jobs = {row["target_repo"]: row for row in resp.json()["remediation_jobs"]}
        assert jobs["https://github.com/example/billing-service"]["status"] == "green"
        assert jobs["https://github.com/example/billing-service"]["pr_url"].endswith("/pull/42")
        assert jobs["https://github.com/example/dashboard-service"]["status"] == "pr_opened"
        assert jobs["https://github.com/example/dashboard-service"]["pr_url"].endswith("/pull/17")

    @pytest.mark.asyncio
    async def test_live_jobs_sync_combines_devin_and_status_updates(self, client, monkeypatch):
        async def fake_sync_devin_sessions(db, limit=50, include_terminal=True):
            return {
                "synced": 2,
                "imported": 1,
                "updated": 1,
                "skipped": 0,
                "total_fetched": 2,
                "change_id": 42,
            }

        async def fake_sync_job_statuses(db, change_id=None, log_progress=False):
            assert change_id == 42
            return {
                "checked": 2,
                "updated": 2,
                "green": 2,
                "pr_opened": 0,
                "ci_failed": 0,
                "needs_human": 0,
                "running": 0,
            }

        monkeypatch.setattr(contracts_routes, "sync_devin_sessions", fake_sync_devin_sessions)
        monkeypatch.setattr(contracts_routes, "sync_job_statuses", fake_sync_job_statuses)
        contracts_routes._last_sync_time = 0

        resp = await client.post("/api/v1/contracts/live-jobs/sync")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["imported"] == 1
        assert payload["updated"] == 3
        assert payload["status_checked"] == 2
        assert payload["status_updated"] == 2
        assert payload["status_green"] == 2

    @pytest.mark.asyncio
    async def test_changes_endpoint_auto_refreshes_live_state_when_enabled(self, client, monkeypatch):
        calls = {"devin": 0, "status": 0}

        async def fake_sync_devin_sessions(db, limit=50, include_terminal=True):
            calls["devin"] += 1
            return {
                "synced": 1,
                "imported": 0,
                "updated": 1,
                "skipped": 0,
                "total_fetched": 1,
                "change_id": 7,
            }

        async def fake_sync_job_statuses(db, change_id=None, log_progress=False):
            calls["status"] += 1
            assert change_id == 7
            return {
                "checked": 1,
                "updated": 1,
                "green": 1,
                "pr_opened": 0,
                "ci_failed": 0,
                "needs_human": 0,
                "running": 0,
            }

        monkeypatch.setattr(settings, "devin_sync_enabled", False)
        monkeypatch.setattr(settings, "devin_api_key", "test-devin-key")
        monkeypatch.setattr(settings, "devin_read_refresh_enabled", True)
        monkeypatch.setattr(settings, "devin_read_refresh_seconds", 60)
        monkeypatch.setattr(contracts_routes, "sync_devin_sessions", fake_sync_devin_sessions)
        monkeypatch.setattr(contracts_routes, "sync_job_statuses", fake_sync_job_statuses)
        contracts_routes._last_live_refresh_time = 0

        first = await client.get("/api/v1/contracts/changes")
        second = await client.get("/api/v1/contracts/changes")

        assert first.status_code == 200
        assert second.status_code == 200
        assert calls == {"devin": 1, "status": 1}

    @pytest.mark.asyncio
    async def test_changes_endpoint_returns_stale_data_when_live_refresh_times_out(self, client, monkeypatch):
        async def slow_sync_devin_sessions(db, limit=50, include_terminal=True):
            await asyncio.sleep(0.05)
            return {
                "synced": 0,
                "imported": 0,
                "updated": 0,
                "skipped": 0,
                "total_fetched": 0,
                "change_id": None,
            }

        monkeypatch.setattr(settings, "devin_sync_enabled", False)
        monkeypatch.setattr(settings, "devin_api_key", "test-devin-key")
        monkeypatch.setattr(settings, "devin_read_refresh_enabled", True)
        monkeypatch.setattr(settings, "devin_read_refresh_seconds", 60)
        monkeypatch.setattr(settings, "devin_read_refresh_timeout_seconds", 0.01)
        monkeypatch.setattr(contracts_routes, "sync_devin_sessions", slow_sync_devin_sessions)
        contracts_routes._last_live_refresh_time = 0

        resp = await client.get("/api/v1/contracts/changes")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_change_detail_auto_refreshes_requested_change_when_live_sync_returns_none(self, client, monkeypatch):
        async with TestSession() as db:
            change = ContractChange(
                base_ref="main",
                head_ref="feature/realtime",
                is_breaking=True,
                severity="high",
                summary_json=json.dumps({"summary": "live refresh test"}),
                changed_routes_json=json.dumps(["POST /api/v1/sessions"]),
                changed_fields_json="[]",
            )
            db.add(change)
            await db.commit()
            change_id = change.id

        async def fake_sync_devin_sessions(db, limit=50, include_terminal=True):
            return {
                "synced": 0,
                "imported": 0,
                "updated": 0,
                "skipped": 0,
                "total_fetched": 0,
                "change_id": None,
            }

        async def fake_sync_job_statuses(db, change_id=None, log_progress=False):
            assert change_id == change_id_under_test
            return {
                "checked": 0,
                "updated": 0,
                "green": 0,
                "pr_opened": 0,
                "ci_failed": 0,
                "needs_human": 0,
                "running": 0,
            }

        change_id_under_test = change_id
        monkeypatch.setattr(settings, "devin_sync_enabled", False)
        monkeypatch.setattr(settings, "devin_api_key", "test-devin-key")
        monkeypatch.setattr(settings, "devin_read_refresh_enabled", True)
        monkeypatch.setattr(settings, "devin_read_refresh_seconds", 60)
        monkeypatch.setattr(contracts_routes, "sync_devin_sessions", fake_sync_devin_sessions)
        monkeypatch.setattr(contracts_routes, "sync_job_statuses", fake_sync_job_statuses)
        contracts_routes._last_live_refresh_time = 0

        resp = await client.get(f"/api/v1/contracts/changes/{change_id_under_test}")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_change_detail_returns_stale_data_when_live_refresh_times_out(self, client, monkeypatch):
        async with TestSession() as db:
            change = ContractChange(
                base_ref="main",
                head_ref="feature/timeout",
                is_breaking=True,
                severity="high",
                summary_json=json.dumps({"summary": "timeout refresh test"}),
                changed_routes_json=json.dumps(["GET /api/v1/contracts/changes/{change_id}"]),
                changed_fields_json="[]",
            )
            db.add(change)
            await db.commit()
            change_id = change.id

        async def slow_sync_devin_sessions(db, limit=50, include_terminal=True):
            await asyncio.sleep(0.05)
            return {
                "synced": 0,
                "imported": 0,
                "updated": 0,
                "skipped": 0,
                "total_fetched": 0,
                "change_id": None,
            }

        monkeypatch.setattr(settings, "devin_sync_enabled", False)
        monkeypatch.setattr(settings, "devin_api_key", "test-devin-key")
        monkeypatch.setattr(settings, "devin_read_refresh_enabled", True)
        monkeypatch.setattr(settings, "devin_read_refresh_seconds", 60)
        monkeypatch.setattr(settings, "devin_read_refresh_timeout_seconds", 0.01)
        monkeypatch.setattr(contracts_routes, "sync_devin_sessions", slow_sync_devin_sessions)
        contracts_routes._last_live_refresh_time = 0

        resp = await client.get(f"/api/v1/contracts/changes/{change_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == change_id


class TestApiKeyAuth:
    @pytest.mark.asyncio
    async def test_requires_api_key_when_configured(self, client, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "api_key", "test-secret")
        denied = await client.get("/api/v1/sessions")
        assert denied.status_code == 401

        allowed = await client.get("/api/v1/sessions", headers={"X-API-Key": "test-secret"})
        assert allowed.status_code == 200

    @pytest.mark.asyncio
    async def test_health_is_exempt_from_api_key(self, client, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "api_key", "test-secret")
        resp = await client.get("/health")
        assert resp.status_code == 200
