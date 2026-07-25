"""
routes.py - API route definitions for the TikTok Story API.
"""

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.models import HealthResponse, RootResponse, StoriesResponse
from app.scraper import fetch_stories
from app.config import settings

router = APIRouter()


@router.get(
    "/",
    response_model=RootResponse,
    summary="API info",
    description="Returns the API name and current version. No authentication required.",
)
async def root() -> RootResponse:
    """Public endpoint — returns basic API metadata."""
    return RootResponse(name=settings.app_name, version=settings.app_version)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Liveness probe. Returns {'status': 'ok'} when the service is running.",
)
async def health() -> HealthResponse:
    """Public health-check endpoint — no authentication required."""
    return HealthResponse(status="ok")


@router.get(
    "/stories",
    response_model=StoriesResponse,
    summary="Get TikTok stories",
    description=(
        "Returns TikTok stories for the specified username. "
        "Requires a valid Bearer token in the Authorization header. "
        "Scraping is not yet implemented — always returns an empty list."
    ),
    dependencies=[Depends(verify_api_key)],  # 🔒 Protected
)
async def get_stories(
    username: str = Query(..., description="TikTok username to fetch stories for"),
) -> StoriesResponse:
    """
    Protected endpoint — fetches stories for a given TikTok username.

    Currently delegates to the scraper stub, which returns an empty list.
    """
    stories = await fetch_stories(username)
    return StoriesResponse(success=True, username=username, stories=stories)
