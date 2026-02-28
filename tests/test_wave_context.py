"""Tests for wave-context propagation messaging."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from propagate.__main__ import _send_context_to_wave, _build_wave_context_payload
from src.database import Base
from src.entities.contract_change import ContractChange
from src.entities.remediation_job import RemediationJob, JobStatus


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


class TestWaveContextMessaging:
    @pytest.mark.asyncio
    async def test_send_context_includes_wave_context_payload(self):
        mock_client = AsyncMock()
        jobs = [
            SimpleNamespace(devin_run_id="sess_alpha"),
            SimpleNamespace(devin_run_id=None),
        ]

        with patch("propagate.__main__.DevinClient", return_value=mock_client):
            await _send_context_to_wave(
                wave_jobs=jobs,
                wave_idx=2,
                context_payload={
                    "source_wave_index": 1,
                    "summary_text": "Wave 1 complete",
                    "upstream_fix_summaries": [{"repo": "billing-service", "status": "green"}],
                    "notable_patterns": ["updated API client callsites"],
                    "test_fixtures_changed": ["tests/fixtures/session.json"],
                    "ci_green_prs": ["https://github.com/org/repo/pull/1"],
                },
            )

        mock_client.send_message.assert_awaited_once()
        args, kwargs = mock_client.send_message.await_args
        assert args[0] == "sess_alpha"
        assert args[1] == "Wave 1 complete"
        assert kwargs["wave_context"]["type"] == "wave-context"
        assert kwargs["wave_context"]["wave_index"] == 2
        assert kwargs["wave_context"]["source_wave_index"] == 1
        assert kwargs["wave_context"]["notable_patterns"] == ["updated API client callsites"]
        assert kwargs["wave_context"]["test_fixtures_changed"] == ["tests/fixtures/session.json"]

    @pytest.mark.asyncio
    async def test_build_payload_extracts_patterns_and_fixtures(self):
        async with TestSession() as db:
            change = ContractChange(
                base_ref="old",
                head_ref="new",
                is_breaking=True,
                severity="high",
                summary_json='{"summary":"x"}',
                changed_routes_json='["POST /api/v1/sessions"]',
                changed_fields_json="[]",
            )
            db.add(change)
            await db.flush()

            job = RemediationJob(
                change_id=change.id,
                target_repo="https://github.com/org/billing-service",
                status=JobStatus.GREEN.value,
                devin_run_id="sess_alpha",
                pr_url="https://github.com/org/billing-service/pull/12",
                bundle_hash="hash1",
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.job_id

        mock_client = AsyncMock()
        mock_client.get_session.return_value = {
            "structured_output": {
                "summary": "Updated billing client and fixtures for renamed contract fields.",
                "changed_files": [
                    "src/clients/gateway.py",
                    "tests/fixtures/session_response.json",
                    "tests/test_gateway.py",
                ],
            }
        }

        with patch("propagate.__main__.async_session", TestSession), \
             patch("propagate.__main__.DevinClient", return_value=mock_client):
            payload = await _build_wave_context_payload([job_id], wave_idx=1)

        assert payload is not None
        assert payload["source_wave_index"] == 1
        assert payload["upstream_fix_summaries"][0]["repo"] == "billing-service"
        assert payload["upstream_fix_summaries"][0]["status"] == JobStatus.GREEN.value
        assert payload["ci_green_prs"] == ["https://github.com/org/billing-service/pull/12"]
        assert "updated API client callsites" in payload["notable_patterns"]
        assert "updated tests/fixtures for contract compatibility" in payload["notable_patterns"]
        assert "tests/fixtures/session_response.json" in payload["test_fixtures_changed"]
