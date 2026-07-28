"""
scraper.py - TikTok page loader using async Playwright / Chromium.

Phase 3: Network interception — records all API-related requests made while
the TikTok profile page loads. Story scraping will be added in a later phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import async_playwright, Error as PlaywrightError, Request, Response

logger = logging.getLogger(__name__)

TIKTOK_BASE_URL = "https://www.tiktok.com"

# Debug output directory — created automatically on first use.
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"

# How long to wait after page load before capturing debug artifacts (seconds).
DEBUG_SETTLE_SECONDS = 8

# URL keywords that identify interesting API calls worth capturing.
NETWORK_KEYWORDS = ("api", "story", "stories", "feed", "post", "item", "aweme")


def _is_api_url(url: str) -> bool:
    """Return True if *url* contains at least one of the tracked keywords."""
    lower = url.lower()
    return any(kw in lower for kw in NETWORK_KEYWORDS)


async def fetch_page_info(username: str) -> dict:
    """
    Launch a headless Chromium browser, navigate to the TikTok profile page
    for *username*, intercept all network traffic, and return page metadata
    together with a filtered list of API-related requests.

    Captured network data is also written to ``screenshots/network.json``.

    Debug artifacts written to ``screenshots/``:

    - ``screenshots/debug.png``   — full-page screenshot
    - ``screenshots/debug.html``  — raw page HTML
    - ``screenshots/network.json``— filtered network requests (Phase 3)

    Args:
        username: TikTok username (without the leading ``@``).

    Returns:
        A dict with the following keys::

            {
                "success": True,
                "title":   "<page title>",
                "url":     "<final url after any redirects>",
                "html_length": <int>,
                "network": [
                    {"url": "...", "method": "GET", "status": 200, "resource_type": "xhr"},
                    ...
                ]
            }

        On failure the dict will have ``"success": False`` and an
        ``"error"`` key with a human-readable message.
    """
    target_url = f"{TIKTOK_BASE_URL}/@{username}"
    logger.info(
        "fetch_page_info: starting browser  username=%r  url=%s",
        username,
        target_url,
    )

    # ── In-memory stores for intercepted traffic ─────────────────────────────
    # Maps request URL → resource type (filled by the "request" event).
    _request_meta: dict[str, str] = {}

    # Final filtered list of captured network entries.
    network_log: list[dict] = []

    # Raw JSON payload from TikTok's Story API (None until captured).
    _story_json: dict | None = None

    def _on_request(request: Request) -> None:
        """Store resource type keyed by URL for later correlation."""
        _request_meta[request.url] = request.resource_type

    async def _on_response(response: Response) -> None:
        """Correlate response with stored request meta and filter by keyword."""
        nonlocal _story_json
        url = response.url
        # Only inspect the Story API
        if "/api/story/item_list/" in url:

            logger.info("Story API detected!")

            try:
                body = await response.json()
                logger.info("Story API JSON received")
                # Store for the caller so it can be parsed without re-reading disk.
                _story_json = body

            except Exception as e:
                logger.warning(f"Couldn't parse JSON: {e}")
                body = await response.text()

            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

            story_path = SCREENSHOTS_DIR / "story_response.json"

            story_path.write_text(
                json.dumps(body, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            logger.info("Saved Story API response")
        if not _is_api_url(url):
            return
        entry = {
            "url": url,
            "method": response.request.method,
            "status": response.status,
            "resource_type": _request_meta.get(url, "unknown"),
        }
        network_log.append(entry)
        logger.debug(
            "fetch_page_info: captured  method=%s  status=%d  url=%s",
            entry["method"],
            entry["status"],
            url,
        )

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            logger.debug("fetch_page_info: Chromium launched")

            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                page = await context.new_page()

                # ── Register network listeners BEFORE navigation ─────────────
                page.on("request", _on_request)
                page.on("response", _on_response)
                logger.debug("fetch_page_info: network listeners registered")

                # ── Navigate ─────────────────────────────────────────────────
                logger.info("fetch_page_info: navigating to %s", target_url)
                await page.goto(target_url, wait_until="load")
                logger.info("fetch_page_info: page load event fired")

                # ── Settle wait ──────────────────────────────────────────────
                logger.info(
                    "fetch_page_info: waiting %d seconds for dynamic content to settle",
                    DEBUG_SETTLE_SECONDS,
                )
                await asyncio.sleep(DEBUG_SETTLE_SECONDS)

                # ── Gather page info ─────────────────────────────────────────
                title = await page.title()
                current_url = page.url
                html_content = await page.content()
                html_length = len(html_content)

                logger.info(
                    "fetch_page_info: page ready  title=%r  url=%s  html_length=%d  "
                    "network_entries=%d",
                    title,
                    current_url,
                    html_length,
                    len(network_log),
                )

                # ── Save debug artifacts ─────────────────────────────────────
                SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

                screenshot_path = SCREENSHOTS_DIR / "debug.png"
                html_path = SCREENSHOTS_DIR / "debug.html"
                network_path = SCREENSHOTS_DIR / "network.json"

                await page.screenshot(path=str(screenshot_path), full_page=True)
                logger.info("fetch_page_info: screenshot saved → %s", screenshot_path)

                html_path.write_text(html_content, encoding="utf-8")
                logger.info("fetch_page_info: HTML saved → %s", html_path)

                network_path.write_text(
                    json.dumps(network_log, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info(
                    "fetch_page_info: network log saved → %s  (%d entries)",
                    network_path,
                    len(network_log),
                )

                return {
                    "success": True,
                    "title": title,
                    "url": current_url,
                    "html_length": html_length,
                    "network": network_log,
                    # Raw Story API payload — None if TikTok never fired the endpoint.
                    "story_json": _story_json,
                }

            finally:
                # Always close the browser, even if an exception occurred.
                await browser.close()
                logger.debug("fetch_page_info: browser closed")

    except PlaywrightError as exc:
        logger.error(
            "fetch_page_info: Playwright error  username=%r — %s",
            username,
            exc,
        )
        return {
            "success": False,
            "title": None,
            "url": target_url,
            "html_length": 0,
            "network": [],
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "fetch_page_info: unexpected error  username=%r",
            username,
        )
        return {
            "success": False,
            "title": None,
            "url": target_url,
            "html_length": 0,
            "network": [],
            "error": str(exc),
        }


async def download_story_media(
    username: str,
    media_url: str,
    chunk_size: int = 65536,
) -> AsyncIterator[bytes]:
    """
    Download TikTok media bytes using a Playwright browser session so that
    TikTok receives its expected cookies and request headers.

    Workflow
    --------
    1. Launch headless Chromium with the same UA used by ``fetch_page_info``.
    2. Navigate to the user's TikTok profile page so the browser establishes
       a valid session (cookies, CORS tokens, etc.).
    3. Use Playwright's ``APIRequestContext`` (which shares the browser
       context's cookie jar) to fetch *media_url* with a proper ``Referer``
       and ``Range`` header.
    4. Yield the response body in *chunk_size* chunks so FastAPI's
       ``StreamingResponse`` can forward them without buffering everything
       in RAM.

    Args:
        username:   TikTok username (without the leading ``@``).
        media_url:  The internal TikTok CDN/playback URL to download.
        chunk_size: Byte size of each yielded chunk (default 64 KiB).

    Yields:
        Raw ``bytes`` chunks of the media file.

    Raises:
        RuntimeError: On any Playwright or HTTP-level failure.
    """
    profile_url = f"{TIKTOK_BASE_URL}/@{username}"
    logger.info(
        "download_story_media: starting  username=%r  media_url=%s",
        username,
        media_url,
    )

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )

                # ── Warm up the session: navigate to profile so TikTok sets
                #    its cookies and the APIRequestContext inherits them.
                page = await context.new_page()
                logger.info(
                    "download_story_media: warming session  url=%s", profile_url
                )
                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as nav_exc:
                    # Non-fatal — session may still be usable.
                    logger.warning(
                        "download_story_media: profile nav warning — %s", nav_exc
                    )

                # Brief settle so TikTok JS can set its session cookies.
                await asyncio.sleep(3)
                await page.close()

                # ── Fetch the media via the cookie-bearing API context ────────
                api_request = context.request
                logger.info("download_story_media: fetching media bytes")

                response = await api_request.get(
                    media_url,
                    headers={
                        "Referer": profile_url,
                        "Origin": TIKTOK_BASE_URL,
                    },
                )

                if not response.ok:
                    raise RuntimeError(
                        f"TikTok CDN returned HTTP {response.status} for media URL."
                    )

                body: bytes = await response.body()
                logger.info(
                    "download_story_media: received %d bytes", len(body)
                )

                # Yield in chunks so callers can stream the response.
                for offset in range(0, len(body), chunk_size):
                    yield body[offset : offset + chunk_size]

            finally:
                await browser.close()
                logger.debug("download_story_media: browser closed")

    except PlaywrightError as exc:
        logger.error("download_story_media: Playwright error — %s", exc)
        raise RuntimeError(f"Browser error while downloading media: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("download_story_media: unexpected error")
        raise RuntimeError(f"Unexpected error while downloading media: {exc}") from exc
