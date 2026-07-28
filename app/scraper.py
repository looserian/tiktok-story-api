"""
scraper.py - TikTok page loader using async Playwright / Chromium.

Fetches stories by:
  1. Navigating to the user's profile page.
  2. Clicking the story avatar ring to open the story viewer.
  3. Navigating through every story with the "next" arrow so TikTok fires
     /api/story/item_list/ for each one.
  4. Keeping the network listener active throughout, accumulating every
     item_list response.
  5. Deduplicating by story ID and falling back to cursor pagination for
     any pages not auto-triggered by the viewer.
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

# Seconds to wait for the story API to fire after the initial page load.
INITIAL_SETTLE_SECONDS = 6

# Seconds to wait after clicking "next" on a story before checking for new API calls.
STORY_NAV_WAIT_SECONDS = 2

# Seconds of silence (no new item_list responses) before we conclude the
# viewer has exhausted all stories and stop navigating.
SILENCE_TIMEOUT_SECONDS = 4

# Maximum number of "next" clicks to make in the story viewer to avoid
# an infinite loop in case TikTok's UI behaves unexpectedly.
MAX_STORY_NAV_CLICKS = 60

# URL keywords that identify interesting API calls worth capturing.
NETWORK_KEYWORDS = ("api", "story", "stories", "feed", "post", "item", "aweme")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# CSS selectors to try when locating the story ring on the profile page.
_STORY_RING_SELECTORS = [
    # Common pattern: avatar wrapped in a story-ring container
    "[data-e2e='user-avatar'] >> .. >> .story-avatar-ring",
    "[data-e2e='user-avatar-with-story']",
    ".avatar-with-ring",
    # Fallback: any element whose aria-label mentions "story"
    "[aria-label*='story' i]",
    "[aria-label*='Story' i]",
    # Generic: the profile avatar image itself (clicking it opens stories on most layouts)
    "[data-e2e='user-avatar']",
    "header img[class*='avatar']",
    # Last resort: any <a> that points to a story URL
    "a[href*='/story/']",
]

# CSS selectors for the "next story" button inside the story viewer.
_NEXT_STORY_SELECTORS = [
    "[data-e2e='arrow-right']",
    "[data-e2e='story-next']",
    "button[aria-label*='next' i]",
    "button[aria-label*='Next' i]",
    ".arrow-right",
    "button.tiktok-x4iix5-ButtonBasic:last-of-type",
]


def _is_api_url(url: str) -> bool:
    """Return True if *url* contains at least one of the tracked keywords."""
    lower = url.lower()
    return any(kw in lower for kw in NETWORK_KEYWORDS)


async def _try_click(page, selectors: list[str], description: str) -> bool:
    """
    Try each selector in *selectors* in order. Click the first visible match.

    Returns True if a click was performed, False if nothing matched.
    """
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                await el.click()
                logger.info("_try_click: clicked %s  selector=%r", description, sel)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("_try_click: selector %r failed — %s", sel, exc)
    logger.warning("_try_click: no clickable element found for %s", description)
    return False


async def fetch_page_info(username: str) -> dict:
    """
    Launch a headless Chromium browser, navigate to the TikTok profile for
    *username*, open the story viewer, navigate through every story, and
    return all intercepted story items merged and deduplicated.

    Strategy
    --------
    1. Navigate to ``/@{username}`` and wait for the page to stabilise.
    2. Click the story avatar ring to open the story viewer.
    3. Repeatedly click the "next" arrow in the viewer, collecting every
       ``/api/story/item_list/`` response the viewer fires.
    4. Stop navigating when no new responses arrive for
       ``SILENCE_TIMEOUT_SECONDS`` or ``MAX_STORY_NAV_CLICKS`` is reached.
    5. If the last intercepted page had ``has_more=true``, fall back to
       explicit cursor-based pagination via ``context.request.get()``.
    6. Deduplicate all collected items by story ``id``.

    Returns
    -------
    On success::

        {
            "success": True,
            "title":      "<page title>",
            "url":        "<final URL>",
            "html_length": <int>,
            "network":    [...],
            "story_json": {"itemList": [...all deduplicated items...], ...envelope},
            "_browser":   <Browser>,   # caller must close
            "_context":   <BrowserContext>,
            "_pw":        <Playwright>,
        }

    On failure::

        {"success": False, "error": "<message>", ...}

    Important
    ---------
    When ``success=True`` the caller receives live browser objects and is
    responsible for closing them.  ``download_story_media`` does this
    automatically inside its ``finally`` block.
    """
    target_url = f"{TIKTOK_BASE_URL}/@{username}"
    logger.info(
        "fetch_page_info: starting  username=%r  url=%s",
        username,
        target_url,
    )

    # ── Shared mutable state (accessed from async callbacks) ─────────────────
    _request_meta: dict[str, str] = {}
    network_log: list[dict] = []

    # story_id → raw item dict  (insertion-ordered, dedup by ID)
    _items_by_id: dict[str, dict] = {}

    # Envelope from the last intercepted page (everything except itemList).
    _envelope: dict | None = None

    # Base URL of the story endpoint (query-string stripped) for pagination.
    _story_base_url: str | None = None

    # Pagination state from the most recently intercepted page.
    _cursor: int = 0
    _has_more: bool = False

    # Monotonically-increasing counter — incremented on every new item_list
    # response so the silence-detection loop can tell whether progress is made.
    _intercept_generation: int = 0

    def _on_request(request: Request) -> None:
        nonlocal _story_base_url
        _request_meta[request.url] = request.resource_type
        if _story_base_url is None and "/api/story/item_list/" in request.url:
            _story_base_url = request.url.split("?")[0]
            logger.debug(
                "fetch_page_info: story endpoint captured  base=%s", _story_base_url
            )

    async def _on_response(response: Response) -> None:
        nonlocal _envelope, _cursor, _has_more, _intercept_generation
        url = response.url

        if "/api/story/item_list/" in url:
            try:
                body = await response.json()
            except Exception as exc:
                logger.warning(
                    "fetch_page_info: could not parse item_list JSON  url=%s — %s",
                    url,
                    exc,
                )
                return

            page_items: list[dict] = body.get("itemList") or []
            new_count = 0
            for itm in page_items:
                sid = str(itm.get("id") or "")
                if sid and sid not in _items_by_id:
                    _items_by_id[sid] = itm
                    new_count += 1

            _cursor = body.get("cursor") or body.get("minCursor") or 0
            _has_more = bool(body.get("has_more") or body.get("hasMore"))
            _envelope = {k: v for k, v in body.items() if k != "itemList"}
            _intercept_generation += 1

            logger.info(
                "fetch_page_info: item_list intercepted"
                "  url=%s"
                "  page_items=%d  new_unique=%d"
                "  cursor=%s  has_more=%s"
                "  total_unique=%d"
                "  generation=%d",
                url,
                len(page_items),
                new_count,
                _cursor,
                _has_more,
                len(_items_by_id),
                _intercept_generation,
            )

        # Always log all matching API traffic.
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
            "fetch_page_info: network  method=%s  status=%d  url=%s",
            entry["method"],
            entry["status"],
            url,
        )

    # ── Browser startup ───────────────────────────────────────────────────────
    try:
        pw = await async_playwright().start()
        browser: Browser = await pw.chromium.launch(headless=True)
        logger.debug("fetch_page_info: Chromium launched")

        context: BrowserContext = await browser.new_context(
            user_agent=_UA,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Register listeners BEFORE navigation so we never miss a response.
        page.on("request", _on_request)
        page.on("response", _on_response)
        logger.debug("fetch_page_info: network listeners registered")

        # ── Phase 1: Navigate to profile ─────────────────────────────────────
        logger.info("fetch_page_info: navigating to %s", target_url)
        await page.goto(target_url, wait_until="load")
        logger.info("fetch_page_info: page load event fired — settling %ds", INITIAL_SETTLE_SECONDS)
        await asyncio.sleep(INITIAL_SETTLE_SECONDS)

        logger.info(
            "fetch_page_info: after initial settle  unique_items=%d  has_more=%s",
            len(_items_by_id),
            _has_more,
        )

        # ── Phase 2: Open story viewer by clicking the story ring ─────────────
        story_ring_clicked = await _try_click(page, _STORY_RING_SELECTORS, "story ring")

        if story_ring_clicked:
            logger.info("fetch_page_info: story viewer opened — waiting for first item_list")
            # Give the viewer time to fire its first API call.
            await asyncio.sleep(STORY_NAV_WAIT_SECONDS)

            # ── Phase 3: Navigate through stories in the viewer ───────────────
            # We keep clicking "next" until either:
            #   a) No new item_list response arrives for SILENCE_TIMEOUT_SECONDS, or
            #   b) MAX_STORY_NAV_CLICKS is reached.
            click_count = 0
            last_seen_generation = _intercept_generation

            while click_count < MAX_STORY_NAV_CLICKS:
                clicked = await _try_click(page, _NEXT_STORY_SELECTORS, "next story")
                click_count += 1

                if not clicked:
                    logger.info(
                        "fetch_page_info: next-story button not found after %d clicks"
                        " — assuming end of viewer",
                        click_count,
                    )
                    break

                # Wait a moment then check whether a new API call arrived.
                await asyncio.sleep(STORY_NAV_WAIT_SECONDS)

                if _intercept_generation > last_seen_generation:
                    logger.info(
                        "fetch_page_info: new item_list after click %d"
                        "  unique_items=%d",
                        click_count,
                        len(_items_by_id),
                    )
                    last_seen_generation = _intercept_generation
                else:
                    # No new response yet — wait for the silence window.
                    logger.debug(
                        "fetch_page_info: no new item_list on click %d"
                        " — waiting %ds for silence timeout",
                        click_count,
                        SILENCE_TIMEOUT_SECONDS,
                    )
                    await asyncio.sleep(SILENCE_TIMEOUT_SECONDS)

                    if _intercept_generation == last_seen_generation:
                        logger.info(
                            "fetch_page_info: silence timeout reached after click %d"
                            "  unique_items=%d — stopping viewer navigation",
                            click_count,
                            len(_items_by_id),
                        )
                        break
                    # A late response arrived during the silence window — continue.
                    last_seen_generation = _intercept_generation

            logger.info(
                "fetch_page_info: viewer navigation done"
                "  clicks=%d  unique_items=%d",
                click_count,
                len(_items_by_id),
            )

        else:
            # Could not find the story ring — the profile may have no active
            # stories, be private, or the DOM changed.  We rely on whatever
            # item_list responses were intercepted during the page load.
            logger.warning(
                "fetch_page_info: story ring not found"
                " — relying on auto-intercepted responses only"
                "  unique_items=%d",
                len(_items_by_id),
            )

        # ── Phase 4: Cursor pagination for any remaining pages ────────────────
        # If the last intercepted page still reports has_more=true, request
        # subsequent pages directly via context.request (same session/cookies).
        if _story_base_url and _has_more:
            logger.info(
                "fetch_page_info: starting cursor pagination"
                "  cursor=%s  unique_items_so_far=%d",
                _cursor,
                len(_items_by_id),
            )
            _page_num = 1
            _current_cursor = _cursor
            _continue = True

            while _continue:
                _page_num += 1
                logger.info(
                    "fetch_page_info: cursor page %d  cursor=%s",
                    _page_num,
                    _current_cursor,
                )
                try:
                    api_resp = await context.request.get(
                        _story_base_url,
                        params={"cursor": str(_current_cursor), "count": "20"},
                        headers={
                            "Referer": target_url,
                            "Origin": TIKTOK_BASE_URL,
                        },
                    )

                    if not api_resp.ok:
                        logger.warning(
                            "fetch_page_info: cursor page %d returned HTTP %d — stopping",
                            _page_num,
                            api_resp.status,
                        )
                        break

                    page_body: dict = await api_resp.json()
                    page_items: list[dict] = page_body.get("itemList") or []

                    new_count = 0
                    for itm in page_items:
                        sid = str(itm.get("id") or "")
                        if sid and sid not in _items_by_id:
                            _items_by_id[sid] = itm
                            new_count += 1

                    _current_cursor = (
                        page_body.get("cursor") or page_body.get("minCursor") or 0
                    )
                    _continue = bool(
                        page_body.get("has_more") or page_body.get("hasMore")
                    )

                    logger.info(
                        "fetch_page_info: cursor page %d done"
                        "  page_items=%d  new_unique=%d"
                        "  cursor=%s  has_more=%s"
                        "  total_unique=%d",
                        _page_num,
                        len(page_items),
                        new_count,
                        _current_cursor,
                        _continue,
                        len(_items_by_id),
                    )

                    if not page_items:
                        logger.info(
                            "fetch_page_info: cursor page %d empty — stopping pagination",
                            _page_num,
                        )
                        break

                except Exception as page_exc:  # noqa: BLE001
                    logger.warning(
                        "fetch_page_info: cursor page %d error — %s",
                        _page_num,
                        page_exc,
                    )
                    break

        # ── Phase 5: Build merged story_json ──────────────────────────────────
        all_items: list[dict] = list(_items_by_id.values())

        logger.info(
            "fetch_page_info: collection complete"
            "  total_unique_stories_from_tiktok=%d",
            len(all_items),
        )

        _story_json: dict | None = None
        if _envelope is not None:
            _story_json = {**_envelope, "itemList": all_items}
        elif all_items:
            _story_json = {"itemList": all_items}

        # Save merged debug file.
        if _story_json is not None:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            story_path = SCREENSHOTS_DIR / "story_response.json"
            story_path.write_text(
                json.dumps(_story_json, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info(
                "fetch_page_info: debug file saved  total_items=%d", len(all_items)
            )

        # ── Gather page metadata ──────────────────────────────────────────────
        title = await page.title()
        current_url = page.url
        html_content = await page.content()
        html_length = len(html_content)

        logger.info(
            "fetch_page_info: done"
            "  title=%r  url=%s  html_length=%d"
            "  network_entries=%d  unique_stories=%d",
            title,
            current_url,
            html_length,
            len(network_log),
            len(all_items),
        )

        # Save remaining debug artifacts.
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

        await page.screenshot(
            path=str(SCREENSHOTS_DIR / "debug.png"), full_page=True
        )
        (SCREENSHOTS_DIR / "debug.html").write_text(html_content, encoding="utf-8")
        (SCREENSHOTS_DIR / "network.json").write_text(
            json.dumps(network_log, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("fetch_page_info: debug artifacts saved")

        # Leave browser alive — caller closes it (or download_story_media does).
        return {
            "success": True,
            "title": title,
            "url": current_url,
            "html_length": html_length,
            "network": network_log,
            "story_json": _story_json,
            # ── Live objects for session reuse ────────────────────────────────
            "_browser": browser,
            "_context": context,
            "_pw": pw,
        }

    except PlaywrightError as exc:
        logger.error(
            "fetch_page_info: Playwright error  username=%r — %s", username, exc
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
        logger.exception("fetch_page_info: unexpected error  username=%r", username)
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
