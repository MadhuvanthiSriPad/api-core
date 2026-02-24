"""Tests for Devin API client request payloads."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from propagate.devin_client import DevinClient


class TestDevinClient:
    @pytest.mark.asyncio
    async def test_create_session_includes_idempotency_key(self):
        client = DevinClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"session_id": "devin_123"}

        with patch.object(client, "_request_with_retry", new=AsyncMock(return_value=mock_resp)) as mock_req:
            result = await client.create_session("fix contract", idempotency_key="bundle_abc")

        assert result["session_id"] == "devin_123"
        mock_req.assert_awaited_once_with(
            "post",
            f"{client.base_url}/sessions",
            json={"prompt": "fix contract", "idempotency_key": "bundle_abc"},
        )

    @pytest.mark.asyncio
    async def test_send_message_posts_to_session_messages_endpoint(self):
        client = DevinClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}

        with patch.object(client, "_request_with_retry", new=AsyncMock(return_value=mock_resp)) as mock_req:
            result = await client.send_message("sess_123", "Wave 0 complete")

        assert result["ok"] is True
        mock_req.assert_awaited_once_with(
            "post",
            f"{client.base_url}/sessions/sess_123/messages",
            json={"message": "Wave 0 complete"},
        )

    @pytest.mark.asyncio
    async def test_send_message_includes_wave_context(self):
        client = DevinClient(api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        wave_context = {"type": "wave-context", "wave_index": 1}

        with patch.object(client, "_request_with_retry", new=AsyncMock(return_value=mock_resp)) as mock_req:
            result = await client.send_message(
                "sess_456",
                "Wave 1 complete",
                wave_context=wave_context,
            )

        assert result["ok"] is True
        mock_req.assert_awaited_once_with(
            "post",
            f"{client.base_url}/sessions/sess_456/messages",
            json={"message": "Wave 1 complete", "wave_context": wave_context},
        )
