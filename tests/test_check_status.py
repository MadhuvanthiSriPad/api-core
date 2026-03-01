"""Tests for the check_status module."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from src.database import Base
from src.entities.remediation_job import RemediationJob, JobStatus
from src.entities.audit_log import AuditLog
from propagate.check_status import check_jobs, CI_UNKNOWN_MAX_ATTEMPTS


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


async def _create_job(status=JobStatus.RUNNING.value, devin_run_id="devin_123", pr_url=None):
    async with TestSession() as db:
        job = RemediationJob(
            change_id=1,
            target_repo="org/test-service",
            status=status,
            devin_run_id=devin_run_id,
            pr_url=pr_url,
            bundle_hash="abc123",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job.job_id


class TestCheckJobs:
    @pytest.mark.asyncio
    async def test_pr_opened_transition(self):
        """Job transitions to PR_OPENED when structured_output has pull_request."""
        job_id = await _create_job()

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "running",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/1"},
            },
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.PR_OPENED.value
            assert job.pr_url == "https://github.com/org/test/pull/1"

    @pytest.mark.asyncio
    async def test_needs_human_on_blocked(self):
        """Job transitions to NEEDS_HUMAN when Devin is blocked."""
        job_id = await _create_job()

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "blocked",
            "structured_output": {},
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.NEEDS_HUMAN.value

    @pytest.mark.asyncio
    async def test_green_on_ci_passed(self):
        """Job transitions to GREEN when CI passes."""
        job_id = await _create_job(pr_url="https://github.com/org/test/pull/1")

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/1"},
                "ci_status": "passed",
            },
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client), \
             patch("propagate.check_status._fetch_github_ci_status", return_value=(False, "unknown")), \
             patch("propagate.check_status._fetch_pr_changed_files", return_value=["src/client.py"]):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.GREEN.value

    @pytest.mark.asyncio
    async def test_ci_failed_on_failure(self):
        """Job transitions to CI_FAILED when CI fails."""
        job_id = await _create_job(pr_url="https://github.com/org/test/pull/1")

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/1"},
                "ci_status": "failed",
            },
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client), \
             patch("propagate.check_status._fetch_github_ci_status", return_value=(False, "unknown")):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.CI_FAILED.value

    @pytest.mark.asyncio
    async def test_ci_unknown_holds_at_pr_opened(self):
        """Job stays at PR_OPENED when CI status is unknown (first attempts)."""
        job_id = await _create_job(
            status=JobStatus.PR_OPENED.value,
            pr_url="https://github.com/org/test/pull/1",
        )

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/1"},
                "ci_status": "unknown",
            },
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client), \
             patch("propagate.check_status._fetch_github_ci_status", return_value=(False, "unknown")):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.PR_OPENED.value

    @pytest.mark.asyncio
    async def test_ci_unknown_fails_closed_after_max_attempts(self):
        """Job transitions to CI_FAILED after max unknown CI attempts."""
        job_id = await _create_job(
            status=JobStatus.PR_OPENED.value,
            pr_url="https://github.com/org/test/pull/1",
        )

        # Pre-seed enough "CI status unknown" audit entries to trigger fail-closed
        async with TestSession() as db:
            for i in range(CI_UNKNOWN_MAX_ATTEMPTS):
                db.add(AuditLog(
                    job_id=job_id,
                    old_status="pr_opened",
                    new_status="pr_opened",
                    detail=f"CI status unknown, holding at PR_OPENED (attempt {i + 1}/{CI_UNKNOWN_MAX_ATTEMPTS}): url",
                ))
            await db.commit()

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/1"},
                "ci_status": "unknown",
            },
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client), \
             patch("propagate.check_status._fetch_github_ci_status", return_value=(False, "unknown")):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.CI_FAILED.value
            assert "failing closed" in job.error_summary

    @pytest.mark.asyncio
    async def test_closed_unmerged_pr_is_not_kept_as_active_attachment(self):
        """Closed-unmerged PRs should fail the job and clear the visible pr_url."""
        job_id = await _create_job(pr_url="https://github.com/org/test/pull/55")

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/55"},
            },
        }

        with (
            patch("propagate.check_status.async_session", TestSession),
            patch("propagate.check_status.DevinClient", return_value=mock_client),
            patch(
                "propagate.check_status._fetch_github_pr_metadata",
                AsyncMock(return_value={"state": "closed", "merged": False, "head_sha": "deadbeef"}),
            ),
        ):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.NEEDS_HUMAN.value
            assert job.pr_url is None
            assert job.error_summary == "PR closed without merge"

    @pytest.mark.asyncio
    async def test_closed_pr_is_replaced_when_new_open_pr_exists(self):
        job_id = await _create_job(pr_url="https://github.com/org/test/pull/55")

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {
                "pull_request": {"url": "https://github.com/org/test/pull/55"},
                "ci_status": "passed",
            },
        }

        metadata_sequence = [
            {
                "state": "closed",
                "merged": False,
                "head_sha": "deadbeef",
                "head_ref": "devin/fix-contract",
                "title": "Fix contract fallout",
                "author_login": "devin-ai-integration",
            },
            {
                "state": "open",
                "merged": False,
                "head_sha": "cafebabe",
                "head_ref": "devin/fix-contract",
                "title": "Fix contract fallout",
                "author_login": "devin-ai-integration",
            },
        ]

        with (
            patch("propagate.check_status.async_session", TestSession),
            patch("propagate.check_status.DevinClient", return_value=mock_client),
            patch(
                "propagate.check_status._fetch_github_pr_metadata",
                AsyncMock(side_effect=metadata_sequence),
            ),
            patch(
                "propagate.check_status._find_replacement_open_pr",
                AsyncMock(return_value="https://github.com/org/test/pull/77"),
            ),
            patch("propagate.check_status._fetch_github_ci_status", AsyncMock(return_value=(True, "passed"))),
            patch("propagate.check_status._fetch_pr_changed_files", AsyncMock(return_value=["src/client.py"])),
        ):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.GREEN.value
            assert job.pr_url == "https://github.com/org/test/pull/77"
            assert job.error_summary is None

    @pytest.mark.asyncio
    async def test_stopped_without_pr_is_needs_human(self):
        job_id = await _create_job(pr_url=None)

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "status_enum": "stopped",
            "structured_output": {},
        }

        with patch("propagate.check_status.async_session", TestSession), \
             patch("propagate.check_status.DevinClient", return_value=mock_client):
            await check_jobs()

        async with TestSession() as db:
            result = await db.execute(
                select(RemediationJob).where(RemediationJob.job_id == job_id)
            )
            job = result.scalar_one()
            assert job.status == JobStatus.NEEDS_HUMAN.value
            assert job.error_summary == "Devin stopped without PR"
