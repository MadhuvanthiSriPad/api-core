"""Tests for Devin live-session sync fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from propagate.service_map import ServiceInfo
from propagate.sync_devin import sync_devin_sessions
from src.database import Base
from src.entities.contract_change import ContractChange
from src.entities.remediation_job import JobStatus, RemediationJob


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


def _service_map():
    return {
        "billing-service": ServiceInfo(repo="https://github.com/MadhuvanthiSriPad/billing-service"),
        "dashboard-service": ServiceInfo(repo="https://github.com/MadhuvanthiSriPad/dashboard-service"),
    }


class TestSyncDevin:
    @pytest.mark.asyncio
    async def test_imports_running_session_and_pr_url(self):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_abc"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_abc",
            "status_enum": "running",
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/101",
                }
            },
        }

        with patch("propagate.sync_devin.DevinClient", return_value=mock_client), \
             patch("propagate.sync_devin.load_service_map", return_value=_service_map()):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)

                jobs = (await db.execute(select(RemediationJob))).scalars().all()
                assert len(jobs) == 1
                assert jobs[0].devin_run_id == "devin_abc"
                assert jobs[0].status == JobStatus.RUNNING.value
                assert jobs[0].target_repo == "https://github.com/MadhuvanthiSriPad/billing-service"
                assert jobs[0].pr_url == "https://github.com/MadhuvanthiSriPad/billing-service/pull/101"
                assert result["imported"] == 1

    @pytest.mark.asyncio
    async def test_backfills_pr_url_on_existing_job(self):
        async with TestSession() as db:
            change = ContractChange(
                base_ref="a",
                head_ref="b",
                is_breaking=True,
                severity="medium",
                summary_json='{"summary":"x"}',
                changed_routes_json='["POST /api/v1/sessions"]',
                changed_fields_json="[]",
            )
            db.add(change)
            await db.flush()
            db.add(
                RemediationJob(
                    change_id=change.id,
                    target_repo="https://github.com/MadhuvanthiSriPad/dashboard-service",
                    status=JobStatus.RUNNING.value,
                    devin_run_id="devin_xyz",
                    pr_url=None,
                    bundle_hash="h1",
                )
            )
            await db.commit()

        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_xyz"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_xyz",
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/88",
                }
            },
        }

        with patch("propagate.sync_devin.DevinClient", return_value=mock_client), \
             patch("propagate.sync_devin.load_service_map", return_value=_service_map()):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                row = (
                    await db.execute(
                        select(RemediationJob).where(RemediationJob.devin_run_id == "devin_xyz")
                    )
                ).scalar_one()
                assert row.pr_url == "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/88"
                assert row.status == JobStatus.PR_OPENED.value
                assert result["updated"] >= 1

    @pytest.mark.asyncio
    async def test_skips_sessions_not_in_service_map(self):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_unknown"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_unknown",
            "status_enum": "running",
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/someone/other-repo/pull/1",
                }
            },
        }

        with patch("propagate.sync_devin.DevinClient", return_value=mock_client), \
             patch("propagate.sync_devin.load_service_map", return_value=_service_map()):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                count = (await db.execute(select(func.count(RemediationJob.job_id)))).scalar_one()
                assert count == 0
                assert result["imported"] == 0

    @pytest.mark.asyncio
    async def test_new_pr_opened_job_emits_webhook_with_real_job_id(self):
        """Regression: newly imported pr_opened jobs must carry their DB-assigned
        job_id in the webhook payload, not 0."""
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [
            {"session_id": "devin_billing_pr"},
            {"session_id": "devin_dashboard_pr"},
        ]
        mock_client.get_session.side_effect = [
            {
                "session_id": "devin_billing_pr",
                "status_enum": "stopped",
                "structured_output": {
                    "pull_request": {
                        "url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/10",
                    }
                },
            },
            {
                "session_id": "devin_dashboard_pr",
                "status_enum": "stopped",
                "structured_output": {
                    "pull_request": {
                        "url": "https://github.com/MadhuvanthiSriPad/dashboard-service/pull/20",
                    }
                },
            },
        ]

        emitted_payloads: list[dict] = []

        async def capture_webhook(path: str, payload: dict) -> None:
            emitted_payloads.append(payload)

        with patch("propagate.sync_devin.DevinClient", return_value=mock_client), \
             patch("propagate.sync_devin.load_service_map", return_value=_service_map()), \
             patch("propagate.sync_devin.emit_webhook", side_effect=capture_webhook):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                assert result["imported"] == 2

        # Each webhook must carry a unique, non-zero job_id.
        pr_opened_payloads = [p for p in emitted_payloads if p.get("event_type") == "pr_opened"]
        assert len(pr_opened_payloads) == 2, f"Expected 2 pr_opened webhooks, got {len(pr_opened_payloads)}"
        job_ids = [p["job_id"] for p in pr_opened_payloads]
        assert all(jid != 0 for jid in job_ids), f"job_id must not be 0: {job_ids}"
        assert len(set(job_ids)) == 2, f"job_ids must be unique across downstream PRs: {job_ids}"

    @pytest.mark.asyncio
    async def test_sync_change_summary_uses_devin_change_description(self):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_desc"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_desc",
            "status_enum": "running",
            "prompt": (
                "# URGENT: Breaking API Contract Change - Parallel Remediation Session\n"
                "**Breaking Change**: New required field(s): request.body.max_cost_usd\n"
            ),
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/202",
                }
            },
        }

        with patch("propagate.sync_devin.DevinClient", return_value=mock_client), \
             patch("propagate.sync_devin.load_service_map", return_value=_service_map()):
            async with TestSession() as db:
                await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                latest_change = (
                    await db.execute(
                        select(ContractChange)
                        .where(ContractChange.head_ref == "devin-live-sync")
                        .order_by(ContractChange.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one()
                assert "request.body.max_cost_usd" in latest_change.summary_json
