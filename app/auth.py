"""
auth.py - API key authentication dependency for FastAPI.

Accepts the key via EITHER:
  • X-API-Key: <key>              (header — preferred for new clients)
  • Authorization: Bearer <key>   (header — backwards-compatible with n8n / existing clients)

Keys are validated against the comma-separated API_KEYS setting.
On failure → HTTP 401 with a structured JSON body.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from fastapi.security.utils import get_authorization_scheme_param

from app.config import settings

logger = logging.getLogger(__name__)


def _extract_key(request: Request) -> str | None:
    """
    Pull the API key from the request, trying both auth mechanisms.

    Priority:
      1. ``X-API-Key`` header
      2. ``Authorization: Bearer <token>`` header

    Returns the raw key string, or ``None`` if neither is present.
    """
    # 1. X-API-Key header (preferred)
    x_api_key = request.headers.get("X-API-Key")
    if x_api_key:
        return x_api_key.strip()

    # 2. Authorization: Bearer <token>
    auth_header = request.headers.get("Authorization", "")
    scheme, token = get_authorization_scheme_param(auth_header)
    if scheme.lower() == "bearer" and token:
        return token.strip()

    return None


async def verify_api_key(request: Request) -> str:
    """
    FastAPI dependency that validates the supplied API key.

    Raises:
        HTTPException 401: When the key is missing or not in API_KEYS.

    Returns:
        The validated key string (available downstream for logging).
    """
    key = _extract_key(request)

    if key is None:
        logger.warning("auth: missing API key  ip=%s", request.client.host if request.client else "unknown")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "success": False,
                "error": "API key is missing. Supply it via X-API-Key header or Authorization: Bearer <key>.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if key not in settings.get_key_set():
        # Log only the last 4 chars to avoid leaking secrets.
        masked = ("*" * max(0, len(key) - 4)) + key[-4:]
        logger.warning("auth: invalid API key  key=%s  ip=%s", masked, request.client.host if request.client else "unknown")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "success": False,
                "error": "Invalid API key.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return key
