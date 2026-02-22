"""Middleware that logs every API request for usage telemetry (Datadog-style)."""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.database import async_session
from src.models.usage_request import UsageRequest

logger = logging.getLogger(__name__)


class UsageTelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        caller = request.headers.get("X-Caller-Service", "unknown")

        # Get the matched route template from FastAPI
        route = request.scope.get("route")
        route_template = route.path if route else request.url.path

        # Skip health checks and docs from telemetry
        if route_template in ("/health", "/docs", "/openapi.json", "/redoc"):
            return response

        try:
            async with async_session() as db:
                record = UsageRequest(
                    caller_service=caller,
                    route_template=route_template,
                    method=request.method,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 2),
                )
                db.add(record)
                await db.commit()
        except Exception:
            logger.warning("Failed to record usage telemetry", exc_info=True)

        return response
