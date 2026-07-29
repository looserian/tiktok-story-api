"""
scraper.py - Adapter shim: maps tiktok_client results to the legacy interface
             expected by routes.py.

The Playwright/Chromium browser automation has been fully removed. This module
now delegates all TikTok I/O to tiktok_client.py (pure httpx) and returns the
same dict shapes that routes.py has always consumed, so no route logic changes
are required.

Public surface (unchanged from the Playwright era):
  • fetch_page_info(username)        → dict
  • download_story_media(...)        → AsyncIterator[bytes]
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from app.tiktok_client import (
    TIKTOK_BASE,
    StoriesNotFoundError,
    TikTokBlockedError,
    UserNotFoundError,
    fetch_stories_for_user,
)

logger = logging.getLogger(__name__)

# User-Agent reused for media downloads.
_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def fetch_page_info(username: str) -> dict:
    """
    Resolve username → fetch all stories via direct httpx calls.

    Adapts the tiktok_client result to the dict contract that routes.py expects:

    Success::

        {
            "success": True,
            "story_json": { "itemList": [...], ... },
        }

    Failure::

        {
            "success":     False,
            "error":       "<human-readable message>",
            "status_code": 404 | 502,   # routes use this to pick the right HTTP status
        }

    Note: No live browser objects are included.  Callers no longer need to
    close a browser context after calling this function.

    Args:
        username: TikTok username without the leading ``@``.

    Returns:
        Always returns a dict — never raises.
    """
    logger.info("fetch_page_info: starting  username=%r", username)

    try:
        story_json = await fetch_stories_for_user(username)
        logger.info(
            "fetch_page_info: success  username=%r  items=%d",
            username,
            len(story_json.get("itemList") or []),
        )
        return {"success": True, "story_json": story_json}

    except UserNotFoundError as exc:
        logger.warning(
            "fetch_page_info: user not found  username=%r — %s", username, exc
        )
        return {"success": False, "error": str(exc), "status_code": 404}

    except StoriesNotFoundError as exc:
        logger.info(
            "fetch_page_info: no active stories  username=%r — %s", username, exc
        )
        return {"success": False, "error": str(exc), "status_code": 404}

    except TikTokBlockedError as exc:
        # Preserve the exact error message raised by the resolver so that the
        # "TikTok anti-bot active. Failed to resolve user profile." string
        # (emitted when all 3 layers fail) reaches the n8n caller unchanged.
        logger.warning(
            "fetch_page_info: TikTok blocked  username=%r — %s", username, exc
        )
        return {"success": False, "error": str(exc), "status_code": 502}

    except Exception as exc:  # noqa: BLE001
        logger.exception("fetch_page_info: unexpected error  username=%r", username)
        return {"success": False, "error": str(exc), "status_code": 502}


async def download_story_media(
    media_url: str,
    username: str,
    chunk_size: int = 65536,
) -> AsyncIterator[bytes]:
    """
    Stream TikTok media bytes to the caller via a direct httpx request.

    Uses browser-mimicking headers (``Referer``, ``User-Agent``) to satisfy
    TikTok's CDN.  Because no session cookies are available in the anonymous
    httpx context, some CDN URLs may return HTTP 403 — this is a known
    limitation of the cookie-free approach.  In that case the caller should
    surface an error message instructing the consumer to use the raw URL
    from the ``/stories`` JSON payload instead.

    Args:
        media_url:  Direct TikTok CDN URL to download.
        username:   TikTok username — used to build the ``Referer`` header.
        chunk_size: Byte size of each yielded chunk (default 64 KiB).

    Yields:
        Raw ``bytes`` chunks of the media file.

    Raises:
        RuntimeError: On HTTP failure or network error.
    """
    profile_url = f"{TIKTOK_BASE}/@{username}"
    logger.info(
        "download_story_media: starting  username=%r  url=%s",
        username,
        media_url,
    )

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            http2=True,
            follow_redirects=True,
        ) as client:
            async with client.stream(
                "GET",
                media_url,
                headers={
                    "User-Agent": _DOWNLOAD_UA,
                    "Referer": profile_url,
                    "Origin": TIKTOK_BASE,
                    "Sec-Fetch-Dest": "video",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Site": "cross-site",
                },
            ) as response:
                if response.status_code == 403:
                    raise RuntimeError(
                        "TikTok CDN returned HTTP 403. "
                        "The URL may have expired or requires session cookies. "
                        "Use the raw media URL from the /stories response directly."
                    )
                if not (200 <= response.status_code < 300):
                    raise RuntimeError(
                        f"TikTok CDN returned HTTP {response.status_code}."
                    )

                logger.info(
                    "download_story_media: streaming  username=%r", username
                )
                async for chunk in response.aiter_bytes(chunk_size):
                    yield chunk

    except RuntimeError:
        raise
    except httpx.RequestError as exc:
        logger.error("download_story_media: network error — %s", exc)
        raise RuntimeError(
            f"Network error while downloading media: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("download_story_media: unexpected error")
        raise RuntimeError(
            f"Unexpected error while downloading media: {exc}"
        ) from exc
