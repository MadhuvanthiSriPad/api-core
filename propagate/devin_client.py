"""Real Devin API client for creating sessions and polling status."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Errors worth retrying (transient)
_RETRYABLE_STATUS_CODES = {502, 503, 504, 429}
_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds


class DevinClient:
    """Client for the Devin API — dispatches coding tasks and polls results."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.devin_api_key
        if not self.api_key:
            raise ValueError(
                "Devin API key is required. "
                "Set API_CORE_DEVIN_API_KEY as an environment variable."
            )
        self.base_url = settings.devin_api_base
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self._client.aclose()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        """Execute an HTTP request with exponential backoff retry on transient errors."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await getattr(self._client, method)(
                    url, headers=self.headers, **kwargs
                )
                if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Retryable %d from %s %s, retrying in %.1fs (attempt %d/%d)",
                        resp.status_code, method.upper(), url,
                        delay, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code in (401, 403):
                    raise httpx.HTTPStatusError(
                        f"Authentication failed ({resp.status_code}) for {method.upper()} {url}. "
                        f"Check that API_CORE_DEVIN_API_KEY is set correctly.",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "%s on %s %s, retrying in %.1fs (attempt %d/%d)",
                        type(exc).__name__, method.upper(), url,
                        delay, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise type(exc)(
                        f"{type(exc).__name__} on {method.upper()} {url} "
                        f"after {_MAX_RETRIES + 1} attempts (last delay: {_BASE_DELAY * (2 ** attempt):.1f}s)"
                    ) from exc
        # Should not reach here, but satisfy type checker
        raise last_exc  # type: ignore[misc]

    async def create_session(
        self,
        prompt: str,
        idempotency_key: str | None = None,
        wave_context: dict | None = None,
    ) -> dict:
        """Create a new Devin session with a task prompt.

        Returns the session response including session_id.
        """
        payload = {"prompt": prompt}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if wave_context:
            payload["wave_context"] = wave_context
        resp = await self._request_with_retry(
            "post",
            f"{self.base_url}/sessions",
            json=payload,
        )
        data = resp.json()
        logger.info("Devin session created: %s", data.get("session_id"))
        return data

    async def send_message(
        self,
        session_id: str,
        message: str,
        wave_context: dict | None = None,
    ) -> dict:
        """Send follow-up context to an existing Devin session."""
        payload = {"message": message}
        if wave_context:
            payload["wave_context"] = wave_context
        resp = await self._request_with_retry(
            "post",
            f"{self.base_url}/sessions/{session_id}/messages",
            json=payload,
        )
        return resp.json()

    async def get_session(self, session_id: str) -> dict:
        """Poll the status of a Devin session.

        Returns session data including status, pull_request info, etc.
        """
        resp = await self._request_with_retry(
            "get",
            f"{self.base_url}/sessions/{session_id}",
        )
        return resp.json()

    async def list_sessions(
        self,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List Devin sessions.

        Handles common response envelope variants:
        - [{"session_id": "..."}]
        - {"sessions": [...]}
        - {"data": [...]}
        - {"results": [...]}
        """
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status

        resp = await self._request_with_retry(
            "get",
            f"{self.base_url}/sessions",
            params=params,
        )
        payload = resp.json()
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("sessions", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []
