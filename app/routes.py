"""
routes.py - API route definitions for the TikTok Story API.
"""

from fastapi import APIRouter, Depends, Query

from app.auth import verify_api_key
from app.models import HealthResponse, RootResponse, PageInfo, StoriesPageResponse
from app.scraper import fetch_page_info
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
    response_model=StoriesPageResponse,
    summary="Get TikTok stories",
    description=(
        "Navigates to the TikTok profile page for the given username using a "
        "headless Chromium browser and returns basic page metadata. "
        "HTML story scraping is not yet implemented (Phase 3). "
        "Requires a valid Bearer token in the Authorization header."
    ),
    dependencies=[Depends(verify_api_key)],  # 🔒 Protected
)
async def get_stories(
    username: str = Query(..., description="TikTok username to fetch stories for"),
) -> StoriesPageResponse:
    """
    Protected endpoint — launches a Playwright Chromium browser, navigates
    to https://www.tiktok.com/@{username}, and returns the page title and
    final URL.

    Story scraping will be added in Phase 3.
    """
    result = await fetch_page_info(username)
    return StoriesPageResponse(
        success=result["success"],
        username=username,
        page=PageInfo(
            title=result.get("title"),
            url=result["url"],
            html_length=result.get("html_length"),
        ),
    )
