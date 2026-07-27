"""
routes.py - API route definitions for the TikTok Story API.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import verify_api_key
from app.models import HealthResponse, RootResponse, ParsedStoriesResponse
from app.scraper import fetch_page_info
from app.config import settings
from app.utils.parser import parse_story_response

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
    response_model=ParsedStoriesResponse,
    summary="Get TikTok stories",
    description=(
        "Navigates to the TikTok profile page for the given username using a "
        "headless Chromium browser, intercepts the Story API response, and "
        "returns a cleaned, structured list of stories with account info. "
        "Requires a valid Bearer token in the Authorization header."
    ),
    dependencies=[Depends(verify_api_key)],  # Protected
)
async def get_stories(
    username: str = Query(..., description="TikTok username to fetch stories for"),
) -> ParsedStoriesResponse:
    """
    Protected endpoint — launches a Playwright Chromium browser, navigates
    to https://www.tiktok.com/@{username}, intercepts the Story API network
    response, parses it, and returns a clean structured payload.
    """
    result = await fetch_page_info(username)

    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail=result.get("error", "Failed to fetch TikTok page"),
        )

    story_json: dict | None = result.get("story_json")

    if story_json is None:
        # TikTok did not fire the Story API during this session.
        return ParsedStoriesResponse(
            success=False,
            username=username,
            story_count=0,
            stories=[],
        )

    parsed = parse_story_response(story_json)
    return ParsedStoriesResponse(**parsed)

