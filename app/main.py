"""
main.py - FastAPI application entry point for the TikTok Story API.

Responsibilities:
  • Create and configure the FastAPI instance (Swagger metadata, tags).
  • Register CORS middleware.
  • Register the request-logging middleware.
  • Register global exception handlers (structured JSON errors).
  • Mount the API router.
  • Configure the Python logging system.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.errors import register_exception_handlers
from app.middleware import LoggingMiddleware
from app.routes import router

# ── Logging configuration ─────────────────────────────────────────────────────
# Configure the root logger once at import time so every module's logger
# inherits the correct level and format automatically.

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)
logger.info("Starting %s v%s", settings.app_name, settings.app_version)


# ── OpenAPI tag metadata ──────────────────────────────────────────────────────

_TAGS_METADATA = [
    {
        "name": "Stories",
        "description": (
            "Core story-fetching endpoints. "
            "Requires a valid API key supplied via **X-API-Key** header "
            "or **Authorization: Bearer** token."
        ),
    },
    {
        "name": "Meta",
        "description": "Service health and version information. No authentication required.",
    },
]


# ── Application factory ───────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "## TikTok Story API\n\n"
        "A production-ready REST API that resolves TikTok usernames to internal "
        "``secUid`` identifiers and calls TikTok's private Story API directly via "
        "``httpx`` — no browser automation, no heavy Chromium dependency.\n\n"
        "### Authentication\n"
        "All `/stories` requests must include a valid API key via one of:\n"
        "- `X-API-Key: <your_key>` header\n"
        "- `Authorization: Bearer <your_key>` header\n\n"
        "### Latency\n"
        "Each request makes 1–2 direct HTTP calls to TikTok's API and typically "
        "completes in **1–3 seconds**.\n\n"
        "### Error Format\n"
        "All error responses share the same envelope:\n"
        "```json\n"
        "{\"success\": false, \"error\": \"<message>\"}\n"
        "```"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=_TAGS_METADATA,
    contact={
        "name": "TikTok Story API Support",
    },
    license_info={
        "name": "Private — All rights reserved",
    },
)


# ── Middleware ────────────────────────────────────────────────────────────────

# NOTE: Starlette applies middleware in reverse registration order.
# LoggingMiddleware is registered last so it wraps everything (runs outermost).

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Restrict in production as needed.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(LoggingMiddleware)


# ── Exception handlers ────────────────────────────────────────────────────────

register_exception_handlers(app)


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(router)
