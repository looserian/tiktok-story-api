"""
routes.py - API route definitions for the TikTok Story API.

All protected endpoints require authentication via one of:
  • X-API-Key: <key>
  • Authorization: Bearer <key>
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import verify_api_key
from app.config import settings
from app.models import (
    ErrorResponse,
    HealthResponse,
    LatestStoryResponse,
    ParsedStory,
    ParsedStoriesResponse,
    RootResponse,
)
from app.scraper import fetch_page_info
from app.utils.parser import parse_story_response
from app.utils.story_store import get_last_story_id, set_last_story_id

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Shared response-example shortcuts ────────────────────────────────────────

_401 = {
    "model": ErrorResponse,
    "description": "Invalid or missing API key.",
    "content": {
        "application/json": {
            "example": {"success": False, "error": "Invalid API key."}
        }
    },
}

_502 = {
    "model": ErrorResponse,
    "description": "TikTok was unreachable or temporarily blocked the request.",
    "content": {
        "application/json": {
            "example": {
                "success": False,
                "error": "TikTok temporarily blocked the request. Try again in a few minutes.",
            }
        }
    },
}

_404 = {
    "model": ErrorResponse,
    "description": "The user has no active stories, or the account is private / not found.",
    "content": {
        "application/json": {
            "example": {"success": False, "error": "No stories found for this user."}
        }
    },
}


# ── Public endpoints ──────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=RootResponse,
    summary="API info",
    description=(
        "Returns the API name, version, and current status. "
        "No authentication required."
    ),
    tags=["Meta"],
    responses={200: {"model": RootResponse}},
)
async def root() -> RootResponse:
    """Public endpoint — returns basic API metadata."""
    return RootResponse(
        name=settings.app_name,
        version=settings.app_version,
        status="running",
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description=(
        "Liveness probe used by Docker, Kubernetes, and load balancers. "
        "Returns ``{\"status\": \"ok\"}`` when the service is running normally. "
        "No authentication required."
    ),
    tags=["Meta"],
    responses={200: {"model": HealthResponse}},
)
async def health() -> HealthResponse:
    """Public health-check endpoint — no authentication required."""
    return HealthResponse(status="ok", version=settings.app_version)


# ── Protected endpoints ───────────────────────────────────────────────────────

@router.get(
    "/stories",
    response_model=ParsedStoriesResponse,
    summary="Fetch TikTok stories for a username",
    description=(
        "Launches a headless Chromium browser via Playwright, navigates to the "
        "TikTok profile page of the given **username**, and intercepts the "
        "private Story API response (`/api/story/item_list/`). "
        "The raw payload is cleaned and returned as structured JSON.\n\n"
        "**Authentication** — supply your API key via one of:\n"
        "- `X-API-Key: <your_key>` header\n"
        "- `Authorization: Bearer <your_key>` header\n\n"
        "**Typical latency** — 10–20 seconds (browser cold-start + TikTok load time)."
    ),
    tags=["Stories"],
    responses={
        200: {
            "model": ParsedStoriesResponse,
            "description": "Stories fetched and parsed successfully.",
        },
        401: _401,
        404: _404,
        502: _502,
    },
    dependencies=[Depends(verify_api_key)],  # 🔒 Protected
)
async def get_stories(
    username: str = Query(
        ...,
        description="TikTok username to fetch stories for (without the leading `@`).",
        examples=["rtrt2805"],
        min_length=1,
        max_length=64,
    ),
) -> ParsedStoriesResponse:
    """
    Protected endpoint — fetches and parses TikTok stories for *username*.

    Error conditions returned as structured JSON:

    | HTTP | Condition |
    |------|-----------|
    | 401  | Missing or invalid API key |
    | 404  | No stories found / private account / user not found |
    | 502  | TikTok blocked the request or the browser failed |
    | 500  | Unexpected internal error |
    """
    logger.info("get_stories: starting  username=%r", username)

    result = await fetch_page_info(username)

    # ── Browser / Playwright failure ──────────────────────────────────────────
    if not result.get("success"):
        error_msg = result.get("error", "")
        logger.error("get_stories: scraper failed  username=%r  error=%s", username, error_msg)
        raise HTTPException(
            status_code=502,
            detail={
                "success": False,
                "error": (
                    "TikTok temporarily blocked the request. Try again in a few minutes."
                    if not error_msg
                    else f"Browser error: {error_msg}"
                ),
            },
        )

    # ── Story API was never intercepted ──────────────────────────────────────
    story_json: dict | None = result.get("story_json")
    if story_json is None:
        logger.warning("get_stories: no story API response  username=%r", username)
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": (
                    "No stories found for this user. "
                    "The account may be private, have no active stories, or the username is invalid."
                ),
            },
        )

    # ── Parse and return ──────────────────────────────────────────────────────
    parsed = parse_story_response(story_json)

    # Extra guard: if parser returned empty stories, surface a 404.
    if not parsed.get("stories"):
        logger.warning("get_stories: parser returned 0 stories  username=%r", username)
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": "No active stories found for this user.",
            },
        )

    logger.info(
        "get_stories: done  username=%r  story_count=%d",
        username,
        parsed.get("story_count", 0),
    )
    return ParsedStoriesResponse(**parsed)


# ── Latest-story endpoint (with duplicate detection) ─────────────────────────

@router.get(
    "/stories/latest",
    response_model=LatestStoryResponse,
    summary="Fetch the newest story with duplicate detection",
    description=(
        "Returns only the **newest** story for *username* and remembers its ID "
        "in `data/last_stories.json`.\n\n"
        "- `new_story: true` — the story ID is different from the last call "
        "(or this is the first call for this username).\n"
        "- `new_story: false` — the same story was already returned before; "
        "no update is written to the file.\n\n"
        "**Authentication** — supply your API key via one of:\n"
        "- `X-API-Key: <your_key>` header\n"
        "- `Authorization: Bearer <your_key>` header\n\n"
        "**Typical latency** — 10–20 seconds (browser cold-start + TikTok load time)."
    ),
    tags=["Stories"],
    responses={
        200: {
            "model": LatestStoryResponse,
            "description": "Newest story returned successfully.",
        },
        401: _401,
        404: _404,
        502: _502,
    },
    dependencies=[Depends(verify_api_key)],  # 🔒 Protected
)
async def get_latest_story(
    username: str = Query(
        ...,
        description="TikTok username to fetch the latest story for (without the leading `@`).",
        examples=["rtrt2805"],
        min_length=1,
        max_length=64,
    ),
) -> LatestStoryResponse:
    """
    Protected endpoint — returns the newest story for *username* and tracks
    whether the story ID has changed since the last request.

    Duplicate-detection logic
    -------------------------
    1. Fetch all stories and sort by ``created_at`` descending.
    2. Take the newest story.
    3. Compare its ID against the value stored in ``data/last_stories.json``:

       - **Username not in file** (first call):
         Save the ID → return ``new_story: true``.
       - **Saved ID equals newest ID** (no change):
         Do NOT update the file → return ``new_story: false``.
       - **Saved ID differs** (new story posted):
         Update the file → return ``new_story: true``.

    Error conditions returned as structured JSON:

    | HTTP | Condition |
    |------|-----------|
    | 401  | Missing or invalid API key |
    | 404  | No stories found / private account / user not found |
    | 502  | TikTok blocked the request or the browser failed |
    | 500  | Unexpected internal error |
    """
    logger.info("get_latest_story: starting  username=%r", username)

    result = await fetch_page_info(username)

    # ── Browser / Playwright failure ──────────────────────────────────────────
    if not result.get("success"):
        error_msg = result.get("error", "")
        logger.error(
            "get_latest_story: scraper failed  username=%r  error=%s", username, error_msg
        )
        raise HTTPException(
            status_code=502,
            detail={
                "success": False,
                "error": (
                    "TikTok temporarily blocked the request. Try again in a few minutes."
                    if not error_msg
                    else f"Browser error: {error_msg}"
                ),
            },
        )

    # ── Story API was never intercepted ──────────────────────────────────────
    story_json: dict | None = result.get("story_json")
    if story_json is None:
        logger.warning("get_latest_story: no story API response  username=%r", username)
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "No active stories"},
        )

    # ── Parse all stories ─────────────────────────────────────────────────────
    parsed = parse_story_response(story_json)
    stories: list[dict] = parsed.get("stories") or []

    if not stories:
        logger.warning("get_latest_story: parser returned 0 stories  username=%r", username)
        raise HTTPException(
            status_code=404,
            detail={"success": False, "error": "No active stories"},
        )

    # ── Sort by created_at descending → pick the newest story ─────────────────
    # Stories without a created_at are sorted to the end (treated as oldest).
    stories_sorted = sorted(
        stories,
        key=lambda s: s.get("created_at") or 0,
        reverse=True,
    )
    newest: dict = stories_sorted[0]
    newest_id: str | None = newest.get("id")

    logger.info(
        "get_latest_story: newest story  username=%r  story_id=%r", username, newest_id
    )

    # ── Duplicate detection ───────────────────────────────────────────────────
    saved_id: str | None = get_last_story_id(username)

    if saved_id is None:
        # Case 1 — username seen for the first time: save and report new.
        logger.info(
            "get_latest_story: first time seeing  username=%r — saving story_id=%r",
            username,
            newest_id,
        )
        if newest_id:
            set_last_story_id(username, newest_id)
        is_new = True

    elif saved_id == newest_id:
        # Case 2 — same story as last time: do NOT update the file.
        logger.info(
            "get_latest_story: no new story  username=%r  story_id=%r", username, newest_id
        )
        is_new = False

    else:
        # Case 3 — new story posted: update the saved ID.
        logger.info(
            "get_latest_story: new story detected  username=%r  old=%r  new=%r",
            username,
            saved_id,
            newest_id,
        )
        if newest_id:
            set_last_story_id(username, newest_id)
        is_new = True

    # ── Build and return the response ─────────────────────────────────────────
    # Use the parsed username from the story payload (authoritative source),
    # falling back to the query parameter if absent.
    response_username: str = parsed.get("username") or username

    return LatestStoryResponse(
        success=True,
        username=response_username,
        new_story=is_new,
        latest_story=ParsedStory(**newest),
    )
