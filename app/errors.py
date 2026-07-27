"""
errors.py - Centralised error response helpers and FastAPI exception handlers.

Every error path in this API returns the same JSON envelope:
    {
        "success": false,
        "error":   "<human-readable message>"
    }

This module registers two global exception handlers on the FastAPI ``app``
instance so that even unhandled exceptions are caught and formatted correctly.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


# ── Helper ────────────────────────────────────────────────────────────────────

def error_response(message: str, status_code: int = 500) -> JSONResponse:
    """
    Build a standardised error ``JSONResponse``.

    Args:
        message:     Human-readable error description.
        status_code: HTTP status code. Defaults to 500.

    Returns:
        A ``JSONResponse`` with body ``{"success": false, "error": message}``.
    """
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": message},
    )


# ── Exception handlers ────────────────────────────────────────────────────────

async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Override FastAPI's default HTTP exception handler.

    If the exception detail is already a dict (e.g. from auth.py), use it
    directly.  Otherwise wrap the plain string in the standard envelope.
    """
    if isinstance(exc.detail, dict):
        content = exc.detail
    else:
        content = {"success": False, "error": str(exc.detail)}

    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=getattr(exc, "headers", None) or {},
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all handler for any exception that escapes the route layer.

    Logs the full traceback at ERROR level and returns a generic 500 response
    so internal details are never leaked to the client.
    """
    logger.exception(
        "Unhandled exception on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error."},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Attach all exception handlers to the given FastAPI ``app``.

    Call this once during application startup in ``main.py``.
    """
    app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)  # type: ignore[arg-type]
