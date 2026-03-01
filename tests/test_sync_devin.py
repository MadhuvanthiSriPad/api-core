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
    async def test_does_not_attach_closed_unmerged_pr(self):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_closed"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_closed",
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/404",
                }
            },
        }

        with (
            patch("propagate.sync_devin.DevinClient", return_value=mock_client),
            patch("propagate.sync_devin.load_service_map", return_value=_service_map()),
            patch(
                "propagate.sync_devin._fetch_github_pr_metadata",
                AsyncMock(return_value={"state": "closed", "merged": False, "head_sha": "deadbeef"}),
            ),
        ):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                row = (await db.execute(select(RemediationJob))).scalar_one()
                assert row.pr_url is None
                assert row.status == JobStatus.NEEDS_HUMAN.value
                assert row.error_summary == "PR closed without merge"
                assert result["imported"] == 1

    @pytest.mark.asyncio
    async def test_failed_devin_session_is_needs_human_not_ci_failed(self):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_failed"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_failed",
            "status_enum": "failed",
            "structured_output": {},
            "repo": "https://github.com/MadhuvanthiSriPad/billing-service",
        }

        with patch("propagate.sync_devin.DevinClient", return_value=mock_client), \
             patch("propagate.sync_devin.load_service_map", return_value=_service_map()):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                row = (await db.execute(select(RemediationJob))).scalar_one()
                assert row.pr_url is None
                assert row.status == JobStatus.NEEDS_HUMAN.value
                assert row.error_summary == "Devin session failed"
                assert result["imported"] == 1

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

    @pytest.mark.asyncio
    async def test_pr_opened_webhook_includes_notification_bundle(self):
        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_notify"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_notify",
            "status_enum": "stopped",
            "prompt": (
                "# URGENT: Breaking API Contract Change - Parallel Remediation Session\n"
                "**Breaking Change**: Added required field(s): request.body.sla_tier\n"
            ),
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/303",
                },
                "notification_bundle": {
                    "author": "devin",
                    "assertions": {
                        "source_repo": "api-core",
                        "target_repo": "https://github.com/MadhuvanthiSriPad/billing-service",
                        "pr_url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/303",
                    },
                    "jira": {
                        "summary": "Devin-authored Jira summary",
                        "description_text": "Devin-authored Jira description",
                    },
                    "slack": {
                        "text": "Devin-authored Slack text",
                        "blocks": [],
                    },
                },
            },
        }
        mock_emit = AsyncMock()

        with (
            patch("propagate.sync_devin.DevinClient", return_value=mock_client),
            patch("propagate.sync_devin.load_service_map", return_value=_service_map()),
            patch("propagate.sync_devin.emit_webhook", mock_emit),
        ):
            async with TestSession() as db:
                await sync_devin_sessions(db=db, limit=10, include_terminal=True)

        mock_emit.assert_awaited_once()
        path_arg, payload_arg = mock_emit.await_args.args
        assert path_arg == "/api/v1/webhooks/pr-opened"
        assert payload_arg["notification_bundle"]["author"] == "devin"
        assert payload_arg["notification_bundle"]["jira"]["summary"] == "Devin-authored Jira summary"
        assert payload_arg["notification_bundle"]["assertions"]["target_repo"].endswith("/billing-service")

    @pytest.mark.asyncio
    async def test_existing_job_with_same_pr_url_does_not_reemit_pr_opened_webhook(self):
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
                    target_repo="https://github.com/MadhuvanthiSriPad/billing-service",
                    status=JobStatus.CI_FAILED.value,
                    devin_run_id="devin_replay",
                    pr_url="https://github.com/MadhuvanthiSriPad/billing-service/pull/303",
                    bundle_hash="h1",
                    error_summary="CI status: failed",
                )
            )
            await db.commit()

        mock_client = AsyncMock()
        mock_client.list_sessions.return_value = [{"session_id": "devin_replay"}]
        mock_client.get_session.return_value = {
            "session_id": "devin_replay",
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {
                    "url": "https://github.com/MadhuvanthiSriPad/billing-service/pull/303",
                }
            },
        }
        mock_emit = AsyncMock()

        with (
            patch("propagate.sync_devin.DevinClient", return_value=mock_client),
            patch("propagate.sync_devin.load_service_map", return_value=_service_map()),
            patch("propagate.sync_devin.emit_webhook", mock_emit),
        ):
            async with TestSession() as db:
                result = await sync_devin_sessions(db=db, limit=10, include_terminal=True)
                row = (
                    await db.execute(
                        select(RemediationJob).where(RemediationJob.devin_run_id == "devin_replay")
                    )
                ).scalar_one()

        assert result["updated"] >= 1
        assert row.status == JobStatus.PR_OPENED.value
        mock_emit.assert_not_awaited()
