"""Real Devin API client for creating sessions and polling status."""

from __future__ import annotations

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class DevinClient:
    """Client for the Devin API — dispatches coding tasks and polls results."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.devin_api_key
        if not self.api_key:
            raise ValueError(
                "DEVIN_API_KEY is required. Set it as an environment variable."
            )
        self.base_url = settings.devin_api_base
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def create_session(self, prompt: str) -> dict:
        """Create a new Devin session with a task prompt.

        Returns the session response including session_id.
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/sessions",
                headers=self.headers,
                json={"prompt": prompt},
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Devin session created: %s", data.get("session_id"))
            return data

    async def get_session(self, session_id: str) -> dict:
        """Poll the status of a Devin session.

        Returns session data including status, pull_request info, etc.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/sessions/{session_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()
