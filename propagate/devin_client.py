"""Real Devin API client for creating sessions and polling status."""

from __future__ import annotations

import os
import logging

import httpx

logger = logging.getLogger(__name__)

DEVIN_API_BASE = "https://api.devin.ai/v1"


class DevinClient:
    """Client for the Devin API — dispatches coding tasks and polls results."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("DEVIN_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "DEVIN_API_KEY is required. Set it as an environment variable."
            )
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
                f"{DEVIN_API_BASE}/sessions",
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
                f"{DEVIN_API_BASE}/sessions/{session_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def send_message(self, session_id: str, message: str) -> dict:
        """Send a follow-up message to a Devin session."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DEVIN_API_BASE}/sessions/{session_id}/messages",
                headers=self.headers,
                json={"message": message},
            )
            resp.raise_for_status()
            return resp.json()
