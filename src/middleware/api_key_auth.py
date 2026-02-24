"""Simple API key middleware for service-to-service auth."""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.config import settings

_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key when API_CORE_API_KEY is configured."""

    async def dispatch(self, request: Request, call_next) -> Response:
        expected_key = settings.api_key
        if not expected_key:
            if settings.debug:
                return await call_next(request)
            return JSONResponse(
                status_code=500,
                content={"detail": "API_CORE_API_KEY not configured"},
            )

        if request.method == "OPTIONS" or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        provided_key = request.headers.get("X-API-Key", "")
        if not provided_key or not secrets.compare_digest(provided_key, expected_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing X-API-Key"},
            )

        return await call_next(request)
