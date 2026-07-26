"""
scraper.py - TikTok page loader using async Playwright / Chromium.

Phase 2: Browser navigation and page-info extraction.
HTML scraping will be added in a later phase.
"""

from __future__ import annotations

import logging

from playwright.async_api import async_playwright, Error as PlaywrightError

logger = logging.getLogger(__name__)

TIKTOK_BASE_URL = "https://www.tiktok.com"


async def fetch_page_info(username: str) -> dict:
    """
    Launch a headless Chromium browser, navigate to the TikTok profile page
    for *username*, wait for the page to fully load, then return basic page
    metadata.

    This function does **not** scrape any HTML content — that is reserved for
    a future phase.

    Args:
        username: TikTok username (without the leading ``@``).

    Returns:
        A dict with the following keys:

        .. code-block:: json

            {
                "success": true,
                "title": "<page title>",
                "url": "<final url after any redirects>"
            }

        On failure the dict will have ``"success": false`` and an
        ``"error"`` key with a human-readable message.
    """
    target_url = f"{TIKTOK_BASE_URL}/@{username}"
    logger.info("fetch_page_info: starting browser for username=%r url=%s", username, target_url)

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

                logger.info("fetch_page_info: navigating to %s", target_url)
                await page.goto(target_url, wait_until="load")

                title = await page.title()
                current_url = page.url

                logger.info(
                    "fetch_page_info: page loaded — title=%r url=%s",
                    title,
                    current_url,
                )

                return {
                    "success": True,
                    "title": title,
                    "url": current_url,
                }

            finally:
                # Always close the browser, even if an exception occurred.
                await browser.close()
                logger.debug("fetch_page_info: browser closed")

    except PlaywrightError as exc:
        logger.error("fetch_page_info: Playwright error for username=%r — %s", username, exc)
        return {
            "success": False,
            "title": None,
            "url": target_url,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("fetch_page_info: unexpected error for username=%r", username)
        return {
            "success": False,
            "title": None,
            "url": target_url,
            "error": str(exc),
        }
