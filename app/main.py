"""
main.py - FastAPI application entry point for the TikTok Story API.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import router

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "A production-ready REST API for fetching TikTok stories. "
        "Protected endpoints require `Authorization: Bearer <API_KEY>`."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# Allow all origins for development; restrict in production as needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(router)
