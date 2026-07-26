"""
scraper.py - TikTok page loader using async Playwright / Chromium.

Phase 2: Browser navigation, page-info extraction, and debug artifacts.
HTML story scraping will be added in a later phase.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError

logger = logging.getLogger(__name__)

TIKTOK_BASE_URL = "https://www.tiktok.com"

# Debug output directory — created automatically on first use.
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"

# How long to wait after page load before capturing debug artifacts (seconds).
DEBUG_SETTLE_SECONDS = 8


async def fetch_page_info(username: str) -> dict:
    """
    Launch a headless Chromium browser, navigate to the TikTok profile page
    for *username*, wait for the page to fully load, then capture debug
    artifacts (screenshot + raw HTML) and return basic page metadata.

    Debug artifacts are written to the ``screenshots/`` directory at the
    project root:

    - ``screenshots/debug.png``  — full-page screenshot
    - ``screenshots/debug.html`` — raw page HTML

    Args:
        username: TikTok username (without the leading ``@``).

    Returns:
        A dict with the following keys::

            {
                "success": True,
                "title":   "<page title>",
                "url":     "<final url after any redirects>",
                "html_length": <int>
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

                # ── Navigate ────────────────────────────────────────────────
                logger.info("fetch_page_info: navigating to %s", target_url)
                await page.goto(target_url, wait_until="load")
                logger.info("fetch_page_info: page load event fired")

                # ── Settle wait ─────────────────────────────────────────────
                logger.info(
                    "fetch_page_info: waiting %d seconds for dynamic content to settle",
                    DEBUG_SETTLE_SECONDS,
                )
                await asyncio.sleep(DEBUG_SETTLE_SECONDS)

                # ── Gather page info ────────────────────────────────────────
                title = await page.title()
                current_url = page.url
                html_content = await page.content()
                html_length = len(html_content)

                logger.info(
                    "fetch_page_info: page ready  title=%r  url=%s  html_length=%d",
                    title,
                    current_url,
                    html_length,
                )

                # ── Save debug artifacts ────────────────────────────────────
                SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

                screenshot_path = SCREENSHOTS_DIR / "debug.png"
                html_path = SCREENSHOTS_DIR / "debug.html"

                await page.screenshot(path=str(screenshot_path), full_page=True)
                logger.info("fetch_page_info: screenshot saved → %s", screenshot_path)

                html_path.write_text(html_content, encoding="utf-8")
                logger.info("fetch_page_info: HTML saved → %s", html_path)

                return {
                    "success": True,
                    "title": title,
                    "url": current_url,
                    "html_length": html_length,
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
            "error": str(exc),
        }
