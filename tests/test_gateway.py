"""Tests for api-core endpoints using SQLite in-memory."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.database import Base, get_db
from src.main import app
from src.entities import AgentSession, TokenUsage, Team


# Use SQLite for tests
test_engine = create_async_engine("sqlite+aiosqlite:///", echo=False)
TestSession = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSession() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


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
