"""Tests for the dispatcher module."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.database import Base
from src.entities.remediation_job import RemediationJob, JobStatus
from src.entities.audit_log import AuditLog
from propagate.bundle import RepoFixBundle
from propagate.guardrails import Guardrails
from propagate.dispatcher import dispatch_remediation_jobs


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


def _bundle(
    service="billing-service",
    repo="org/billing-service",
    client_paths=None,
    test_paths=None,
    frontend_paths=None,
):
    return RepoFixBundle(
        target_repo=repo,
        target_service=service,
        change_summary="Test change",
        breaking_changes=[],
        affected_routes=["POST /api/v1/sessions"],
        call_count_7d=42,
        client_paths=client_paths or ["src/client.py"],
        test_paths=test_paths or ["tests/test_client.py"],
        frontend_paths=frontend_paths or [],
        prompt="Fix the breaking change",
    )


class TestDispatchOne:
    @pytest.mark.asyncio
    async def test_guardrail_violation_sets_needs_human(self):
        """Bundles touching protected paths should be blocked with NEEDS_HUMAN."""
        bundle = _bundle(client_paths=["infra/main.tf"])
        guardrails = Guardrails()

        with patch("propagate.dispatcher.async_session_factory", TestSession), \
             patch("propagate.dispatcher.DevinClient") as MockClient:
            MockClient.return_value = MagicMock()
            jobs = await dispatch_remediation_jobs([bundle], guardrails, change_id=1)

        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.NEEDS_HUMAN.value
        assert "Guardrail violation" in jobs[0].error_summary

    @pytest.mark.asyncio
    async def test_guardrail_violation_in_test_paths_sets_needs_human(self):
        """Guardrails should apply to declared test paths too."""
        bundle = _bundle(test_paths=[".github/workflows/ci.yaml"])
        guardrails = Guardrails()

        with patch("propagate.dispatcher.async_session_factory", TestSession), \
             patch("propagate.dispatcher.DevinClient") as MockClient:
            MockClient.return_value = MagicMock()
            jobs = await dispatch_remediation_jobs([bundle], guardrails, change_id=1)

        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.NEEDS_HUMAN.value
        assert "Guardrail violation" in jobs[0].error_summary

    @pytest.mark.asyncio
    async def test_guardrail_violation_in_frontend_paths_sets_needs_human(self):
        """Guardrails should apply to frontend path declarations."""
        bundle = _bundle(frontend_paths=["terraform/modules/main.tf"])
        guardrails = Guardrails()

        with patch("propagate.dispatcher.async_session_factory", TestSession), \
             patch("propagate.dispatcher.DevinClient") as MockClient:
            MockClient.return_value = MagicMock()
            jobs = await dispatch_remediation_jobs([bundle], guardrails, change_id=1)

        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.NEEDS_HUMAN.value
        assert "Guardrail violation" in jobs[0].error_summary

    @pytest.mark.asyncio
    async def test_successful_dispatch(self):
        """Successful dispatch transitions QUEUED -> RUNNING with devin_run_id."""
        bundle = _bundle()
        guardrails = Guardrails()

        mock_client = AsyncMock()
        mock_client.create_session.return_value = {"session_id": "devin_test_001"}

        with patch("propagate.dispatcher.async_session_factory", TestSession), \
             patch("propagate.dispatcher.DevinClient", return_value=mock_client):
            jobs = await dispatch_remediation_jobs([bundle], guardrails, change_id=1)

        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.RUNNING.value
        assert jobs[0].devin_run_id == "devin_test_001"
        mock_client.create_session.assert_awaited_once_with(
            bundle.prompt,
            idempotency_key=f"change-1-{bundle.bundle_hash}",
        )

    @pytest.mark.asyncio
    async def test_dispatch_api_error_sets_needs_human(self):
        """API errors during dispatch should result in NEEDS_HUMAN."""
        bundle = _bundle()
        guardrails = Guardrails()

        mock_client = AsyncMock()
        mock_client.create_session.side_effect = RuntimeError("API timeout")

        with patch("propagate.dispatcher.async_session_factory", TestSession), \
             patch("propagate.dispatcher.DevinClient", return_value=mock_client):
            jobs = await dispatch_remediation_jobs([bundle], guardrails, change_id=1)

        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.NEEDS_HUMAN.value
        assert "API timeout" in jobs[0].error_summary
