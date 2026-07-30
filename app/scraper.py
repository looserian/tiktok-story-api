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

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Request,
    Response,
)

logger = logging.getLogger(__name__)

TIKTOK_BASE_URL = "https://www.tiktok.com"

# Debug output directory — created automatically on first use.
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"

# How long to wait after page load before capturing debug artifacts (seconds).
DEBUG_SETTLE_SECONDS = 8

# URL keywords that identify interesting API calls worth capturing.
NETWORK_KEYWORDS = ("api", "story", "stories", "feed", "post", "item", "aweme")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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

    Debug artifacts written to ``screenshots/``):

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
                ],
                "story_json": <dict | None>,
                # Internal: live browser objects for download reuse.
                # Present only when success=True.
                "_browser":  <Browser>,
                "_context":  <BrowserContext>,
            }

        On failure the dict will have ``"success": False`` and an
        ``"error"`` key with a human-readable message.

    Important
    ---------
    When ``success=True`` the caller receives a live ``_browser`` and
    ``_context``.  The caller is responsible for closing them (via
    ``browser.close()``) after the data has been consumed.  The download
    helper ``download_story_media`` does this automatically after streaming
    is complete.
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

    # Accumulated items from all intercepted /api/story/item_list/ pages.
    # We keep every item seen so that multiple auto-triggered pages are merged.
    _all_items: list[dict] = []

    # Envelope fields from the *last* intercepted page (status_code, etc.).
    # We overwrite on each page so the final merged JSON looks like one response.
    _envelope: dict | None = None

    # Base URL of the story endpoint (without query params) so we can call it
    # directly when paginating.  Captured from the first matching request.
    _story_base_url: str | None = None

    # Cursor and has_more from the last intercepted page.
    _cursor: int = 0
    _has_more: bool = False

    def _on_request(request: Request) -> None:
        """Store resource type keyed by URL for later correlation."""
        nonlocal _story_base_url
        _request_meta[request.url] = request.resource_type
        # Capture the base URL of the story endpoint on first match so we can
        # re-use it for manual pagination after the page has loaded.
        if _story_base_url is None and "/api/story/item_list/" in request.url:
            # Strip query-string — we'll supply our own params.
            _story_base_url = request.url.split("?")[0]
            logger.debug("fetch_page_info: story base URL captured → %s", _story_base_url)

    async def _on_response(response: Response) -> None:
        """Accumulate story items from every intercepted story-list page."""
        nonlocal _envelope, _cursor, _has_more
        url = response.url
        # Only inspect the Story API
        if "/api/story/item_list/" in url:
            logger.info("fetch_page_info: story API page intercepted  url=%s", url)

            try:
                body = await response.json()
            except Exception as e:
                logger.warning("fetch_page_info: couldn't parse story JSON — %s", e)
                return

            page_items: list[dict] = body.get("itemList") or []
            _all_items.extend(page_items)

            # Track pagination state from this page.
            _cursor = body.get("cursor") or body.get("minCursor") or 0
            _has_more = bool(body.get("has_more") or body.get("hasMore"))

            logger.info(
                "fetch_page_info: intercepted page  items=%d  cursor=%s  has_more=%s",
                len(page_items),
                _cursor,
                _has_more,
            )

            # Save envelope metadata (minus the items) for building the merged response.
            _envelope = {k: v for k, v in body.items() if k != "itemList"}

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

    # We hold pw / browser / context alive deliberately so the caller can
    # reuse the warmed-up session for a subsequent media download.
    # The caller MUST call browser.close() when done.
    try:
        pw = await async_playwright().start()
        browser: Browser = await pw.chromium.launch(headless=True)
        logger.debug("fetch_page_info: Chromium launched")

        context: BrowserContext = await browser.new_context(
            user_agent=_UA,
            locale="en-US",
        )
        page = await context.new_page()

        # ── Register network listeners BEFORE navigation ─────────────────────
        page.on("request", _on_request)
        page.on("response", _on_response)
        logger.debug("fetch_page_info: network listeners registered")

        # ── Navigate ─────────────────────────────────────────────────────────
        logger.info("fetch_page_info: navigating to %s", target_url)
        await page.goto(target_url, wait_until="load")
        logger.info("fetch_page_info: page load event fired")

        # ── Settle wait ──────────────────────────────────────────────────────
        logger.info(
            "fetch_page_info: waiting %d seconds for dynamic content to settle",
            DEBUG_SETTLE_SECONDS,
        )
        await asyncio.sleep(DEBUG_SETTLE_SECONDS)

        # ── Paginate: fetch remaining pages via the existing session ─────────
        # After the page has loaded, TikTok may have only auto-fetched page 1.
        # If has_more is still true we explicitly request subsequent pages via
        # context.request (which shares the same cookies) until exhausted.
        if _story_base_url and _has_more:
            logger.info(
                "fetch_page_info: starting manual pagination  cursor=%s  "
                "items_so_far=%d",
                _cursor,
                len(_all_items),
            )
            _page_num = 2
            _current_cursor = _cursor
            _continue = _has_more

            while _continue:
                logger.info(
                    "fetch_page_info: fetching page %d  cursor=%s",
                    _page_num,
                    _current_cursor,
                )
                try:
                    api_resp = await context.request.get(
                        _story_base_url,
                        params={
                            "cursor": str(_current_cursor),
                            "count": "20",
                        },
                        headers={
                            "Referer": target_url,
                            "Origin": TIKTOK_BASE_URL,
                        },
                    )

                    if not api_resp.ok:
                        logger.warning(
                            "fetch_page_info: pagination request returned HTTP %d — stopping",
                            api_resp.status,
                        )
                        break

                    page_body: dict = await api_resp.json()
                    page_items: list[dict] = page_body.get("itemList") or []
                    _all_items.extend(page_items)

                    _current_cursor = (
                        page_body.get("cursor")
                        or page_body.get("minCursor")
                        or 0
                    )
                    _continue = bool(
                        page_body.get("has_more") or page_body.get("hasMore")
                    )

                    logger.info(
                        "fetch_page_info: page %d done  items=%d  cursor=%s  has_more=%s  total_so_far=%d",
                        _page_num,
                        len(page_items),
                        _current_cursor,
                        _continue,
                        len(_all_items),
                    )
                    _page_num += 1

                    # Safety: stop if a page returned no items to avoid an
                    # infinite loop when TikTok returns has_more=true but an
                    # empty list (sometimes happens at the real end).
                    if not page_items:
                        logger.info(
                            "fetch_page_info: empty page returned — stopping pagination"
                        )
                        break

                except Exception as page_exc:  # noqa: BLE001
                    logger.warning(
                        "fetch_page_info: pagination error on page %d — %s",
                        _page_num,
                        page_exc,
                    )
                    break

        logger.info(
            "fetch_page_info: pagination complete  total_items_from_tiktok=%d",
            len(_all_items),
        )

        # ── Build the merged story_json ───────────────────────────────────────
        # Combine the envelope (from last intercepted page) with all accumulated
        # items so the parser sees a single dict with the full itemList.
        _story_json: dict | None = None
        if _envelope is not None:
            _story_json = {**_envelope, "itemList": _all_items}
        elif _all_items:
            # Fallback: no envelope captured but items were collected.
            _story_json = {"itemList": _all_items}

        # Save merged response for debugging.
        if _story_json is not None:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            story_path = SCREENSHOTS_DIR / "story_response.json"
            story_path.write_text(
                json.dumps(_story_json, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info(
                "fetch_page_info: saved merged story response  total_items=%d",
                len(_all_items),
            )

        # ── Gather page info ─────────────────────────────────────────────────
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

        # ── Save debug artifacts ─────────────────────────────────────────────
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

        # Leave page open (keeps session warm) — the caller decides when to
        # close the browser.
        return {
            "success": True,
            "title": title,
            "url": current_url,
            "html_length": html_length,
            "network": network_log,
            # Merged Story API payload containing all pages.
            # None if TikTok never fired the endpoint.
            "story_json": _story_json,
            # ── Live browser objects for session reuse ────────────────────────
            # Caller must call _browser.close() when finished.
            "_browser": browser,
            "_context": context,
            "_pw": pw,
        }

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
    media_url: str,
    context: BrowserContext,
    browser: Browser,
    pw,
    username: str,
    chunk_size: int = 65536,
) -> AsyncIterator[bytes]:
    """
    Download TikTok media bytes through the **existing** Playwright
    ``BrowserContext`` that was used to fetch the stories.

    By reusing the same context the download request inherits the session
    cookies, browser fingerprint, and any JS-set tokens that TikTok's CDN
    expects — avoiding the HTTP 403 that a fresh httpx / requests call
    would receive.

    Workflow
    --------
    1. Use the caller-supplied *context* (already warmed up by
       ``fetch_page_info``) to make a ``context.request.get()`` call.
    2. Yield the response body in *chunk_size* chunks so FastAPI's
       ``StreamingResponse`` can forward them without buffering everything
       in RAM.
    3. Close the browser (and the Playwright instance) when done so we
       don't leak browser processes.

    Args:
        media_url:  The internal TikTok CDN/playback URL to download.
        context:    The live ``BrowserContext`` from ``fetch_page_info``.
        browser:    The live ``Browser`` instance (will be closed after use).
        pw:         The live ``Playwright`` instance (will be stopped after use).
        username:   TikTok username — used only for the ``Referer`` header.
        chunk_size: Byte size of each yielded chunk (default 64 KiB).

    Yields:
        Raw ``bytes`` chunks of the media file.

    Raises:
        RuntimeError: On any Playwright or HTTP-level failure.
    """
    profile_url = f"{TIKTOK_BASE_URL}/@{username}"
    logger.info(
        "download_story_media: fetching via existing session  username=%r  media_url=%s",
        username,
        media_url,
    )

    try:
        api_request = context.request

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
        logger.info("download_story_media: received %d bytes", len(body))

        # Yield in chunks so callers can stream the response.
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]

    except PlaywrightError as exc:
        logger.error("download_story_media: Playwright error — %s", exc)
        raise RuntimeError(f"Browser error while downloading media: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("download_story_media: unexpected error")
        raise RuntimeError(f"Unexpected error while downloading media: {exc}") from exc
    finally:
        # Always release the browser resources after the stream is exhausted
        # (or on error), regardless of whether the download succeeded.
        try:
            await browser.close()
            logger.debug("download_story_media: browser closed")
        except Exception:
            pass
        try:
            await pw.stop()
            logger.debug("download_story_media: playwright stopped")
        except Exception:
            pass
