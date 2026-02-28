"""Fire-and-forget webhook emission to notification-service.

Failures are logged but never raised — notification delivery must not
block the core remediation pipeline.
"""

from __future__ import annotations

import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


async def emit_webhook(path: str, payload: dict) -> None:
    """POST payload to notification-service at the given path.

    Silent on failure — logs the error and returns.
    """
    base = settings.notification_webhook_url
    if not base:
        logger.debug("notification_webhook_url not configured — skipping webhook")
        return

    url = f"{base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Webhook delivered to %s — %s", url, resp.json())
    except Exception as exc:
        logger.warning("Webhook to %s failed (non-fatal): %s", url, exc)
