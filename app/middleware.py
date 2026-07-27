"""
middleware.py - Request logging middleware for the TikTok Story API.

Emits one structured INFO log line per request:

    INFO  API request  username=rtrt2805  ip=1.2.3.4  apikey=****1234  time=2.31s  success=True

Fields:
    username  — value of the ``username`` query parameter, or ``"-"`` if absent.
    ip        — client IP; honours ``X-Forwarded-For`` for reverse-proxy setups.
    apikey    — the supplied key with all but the last 4 characters masked.
    time      — wall-clock duration of the request in seconds (2 d.p.).
    success   — True when the HTTP status is < 400, False otherwise.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """
    Return the real client IP address.

    Checks ``X-Forwarded-For`` first (set by load balancers / reverse proxies),
    then falls back to the direct connection address.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # The header may contain a comma-separated list; the first entry is the
        # originating client.
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _mask_key(key: str | None) -> str:
    """
    Mask an API key for safe logging.

    Keeps the last 4 characters visible so operators can identify which key
    was used without exposing the full secret.

    Examples:
        ``"supersecretkey1234"`` → ``"**************1234"``
        ``"short"``              → ``"*1234"`` (last-4 always shown)
        ``None``                 → ``"-"``
    """
    if not key:
        return "-"
    visible = key[-4:]
    return ("*" * max(0, len(key) - 4)) + visible


def _extract_key_from_request(request: Request) -> str | None:
    """Extract the raw API key from the request headers (mirrors auth.py logic)."""
    # X-API-Key takes priority
    x_api_key = request.headers.get("X-API-Key")
    if x_api_key:
        return x_api_key.strip()

    # Fallback: Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    return None


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that logs every HTTP request with structured fields.

    Registered in ``main.py`` via ``app.add_middleware(LoggingMiddleware)``.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        start = time.perf_counter()

        # Gather context before handing off to the route layer.
        username = request.query_params.get("username", "-")
        client_ip = _get_client_ip(request)
        raw_key = _extract_key_from_request(request)
        masked_key = _mask_key(raw_key)

        # Process the request.
        response: Response = await call_next(request)

        elapsed = time.perf_counter() - start
        success = response.status_code < 400

        logger.info(
            "API request  username=%s  ip=%s  apikey=%s  time=%.2fs  success=%s",
            username,
            client_ip,
            masked_key,
            elapsed,
            success,
        )

        return response
