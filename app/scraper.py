"""
scraper.py - TikTok story scraper (stub for future implementation).

This module will use Playwright + Chromium to scrape TikTok stories.
Playwright and its browser are installed in the Docker image and ready to use.
"""

from __future__ import annotations

from app.models import Story


async def fetch_stories(username: str) -> list[Story]:
    """
    Fetch TikTok stories for the given username.

    NOTE: Scraping is not yet implemented. This function returns an
    empty list as a placeholder until the scraper logic is built.

    Future implementation outline:
        1. Launch a Playwright Chromium browser (headless=True).
        2. Navigate to https://www.tiktok.com/@{username}.
        3. Detect and extract story elements from the DOM / network responses.
        4. Parse media URLs, thumbnails, and metadata.
        5. Return a populated list[Story].

    Args:
        username: TikTok username to scrape stories for.

    Returns:
        A list of Story objects (currently always empty).
    """
    # TODO: implement Playwright-based scraping here
    return []
