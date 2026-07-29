"""
tiktok_client.py - Direct async HTTP client for TikTok's internal Story API.

Replaces the Playwright/Chromium scraper with two plain httpx GET requests:

  Phase 1 — resolve_sec_uid()
      GET https://www.tiktok.com/@{username}
      → parse the __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON blob embedded in
        TikTok's SSR HTML to extract the user's ``secUid`` identifier.
        No JavaScript execution required.

  Phase 2 — fetch_stories_for_user()
      GET https://www.tiktok.com/api/story/item_list/
          ?secUid={secUid}&count=30&cursor={cursor}
      → paginate until has_more is falsy, accumulate all itemList entries,
        return a merged dict shaped like a single TikTok API response page.

All requests use browser-mimicking headers (User-Agent, Sec-Ch-Ua, Sec-Fetch-*)
to blend in with organic Chrome traffic. No cookies or authentication tokens
are sent, keeping every request 100 % anonymous.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TIKTOK_BASE = "https://www.tiktok.com"
_STORY_API_PATH = "/api/story/item_list/"

# Chrome 124 on Windows — matches the UA used in the old Playwright session.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Headers for the profile page request (document navigation).
_PROFILE_HEADERS: dict[str, str] = {
    "User-Agent": _UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Headers for the story API XHR (same-origin CORS request).
_STORY_API_HEADERS: dict[str, str] = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Regex to locate the SSR JSON blob in TikTok's HTML.
# The blob is embedded as:
#   <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
#     { ... }
#   </script>
_UDR_RE = re.compile(
    r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


# ── Typed exceptions ──────────────────────────────────────────────────────────

class UserNotFoundError(Exception):
    """The TikTok profile page was reached but the user does not exist."""


class StoriesNotFoundError(Exception):
    """The user exists but currently has no active stories."""


class TikTokBlockedError(Exception):
    """TikTok returned a blocking, rate-limiting, or server-error response."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_client() -> httpx.AsyncClient:
    """
    Return a shared httpx.AsyncClient instance with sensible defaults.

    - 30 s total timeout, 10 s connect timeout.
    - HTTP/2 enabled (TikTok supports it and it reduces fingerprint risk).
    - follow_redirects=True so profile URLs that redirect are handled silently.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        http2=True,
        follow_redirects=True,
    )


def _parse_sec_uid_from_html(html: str, username: str) -> str:
    """
    Locate and extract the ``secUid`` field from TikTok's SSR JSON blob.

    TikTok embeds a ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` JSON object in every
    profile page.  The relevant path inside that object is::

        .__DEFAULT_SCOPE__
          .webapp.user-detail
            .userInfo
              .user
                .secUid   ← what we need

    Raises:
        UserNotFoundError: If the blob is absent or the secUid key is missing.
        TikTokBlockedError: If the blob is present but cannot be parsed as JSON
            (e.g. TikTok served a CAPTCHA or bot-challenge page instead).
    """
    match = _UDR_RE.search(html)
    if not match:
        raise UserNotFoundError(
            f"__UNIVERSAL_DATA_FOR_REHYDRATION__ blob not found for '@{username}'. "
            "The account may not exist, or TikTok served a bot-challenge page."
        )

    try:
        udr: dict = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TikTokBlockedError(
            f"Failed to parse TikTok SSR JSON for '@{username}': {exc}"
        ) from exc

    # Traverse the known key path.
    default_scope: dict = udr.get("__DEFAULT_SCOPE__") or {}
    user_detail: dict = default_scope.get("webapp.user-detail") or {}
    user_info: dict = user_detail.get("userInfo") or {}
    user: dict = user_info.get("user") or {}
    sec_uid: str | None = user.get("secUid")

    if not sec_uid:
        raise UserNotFoundError(
            f"secUid not found in TikTok SSR data for '@{username}'. "
            "The account may be private, suspended, or the username is invalid."
        )

    return sec_uid


# ── Public API ────────────────────────────────────────────────────────────────

async def resolve_sec_uid(username: str, client: httpx.AsyncClient) -> str:
    """
    Fetch the TikTok profile page and return the user's ``secUid``.

    Args:
        username: TikTok username without the leading ``@``.
        client:   A live ``httpx.AsyncClient`` to reuse for the request.

    Returns:
        The ``secUid`` string (a long opaque identifier used by TikTok's APIs).

    Raises:
        UserNotFoundError:   Profile page returned 404, or secUid is absent.
        TikTokBlockedError:  HTTP 403, 429, 5xx, or unparseable response.
    """
    url = f"{TIKTOK_BASE}/@{username}"
    logger.info("resolve_sec_uid: GET %s", url)

    try:
        resp = await client.get(url, headers=_PROFILE_HEADERS)
    except httpx.RequestError as exc:
        raise TikTokBlockedError(
            f"Network error fetching TikTok profile for '@{username}': {exc}"
        ) from exc

    logger.debug(
        "resolve_sec_uid: response  status=%d  url=%s",
        resp.status_code,
        str(resp.url),
    )

    if resp.status_code == 404:
        raise UserNotFoundError(
            f"TikTok returned HTTP 404 for '@{username}'. The account does not exist."
        )
    if resp.status_code in (403, 429):
        raise TikTokBlockedError(
            f"TikTok blocked the profile request for '@{username}' "
            f"(HTTP {resp.status_code})."
        )
    if resp.status_code >= 500:
        raise TikTokBlockedError(
            f"TikTok server error while fetching '@{username}' "
            f"(HTTP {resp.status_code})."
        )
    if resp.status_code != 200:
        raise TikTokBlockedError(
            f"Unexpected HTTP {resp.status_code} from TikTok profile for '@{username}'."
        )

    sec_uid = _parse_sec_uid_from_html(resp.text, username)
    logger.info(
        "resolve_sec_uid: resolved  username=%r  secUid=%s…",
        username,
        sec_uid[:24],
    )
    return sec_uid


async def fetch_stories_for_user(username: str) -> dict:
    """
    Full pipeline: resolve ``secUid`` → fetch and paginate the story API.

    Opens a single ``httpx.AsyncClient``, reuses it for both the profile page
    and all story API pages (connection pooling, shared TLS session).

    Args:
        username: TikTok username without the leading ``@``.

    Returns:
        A merged dict shaped like a single TikTok ``/api/story/item_list/``
        response, with ``itemList`` containing **all** accumulated stories::

            {
                "itemList": [ { ... }, ... ],   # all stories, all pages
                "status_code": 0,               # from last API page envelope
                ...                             # other TikTok envelope fields
            }

    Raises:
        UserNotFoundError:   User does not exist or secUid cannot be resolved.
        StoriesNotFoundError: User exists but has no active stories.
        TikTokBlockedError:  TikTok rate-limited or blocked the request.
    """
    async with _make_client() as client:
        # ── Step 1: resolve secUid ────────────────────────────────────────────
        sec_uid = await resolve_sec_uid(username, client)

        # ── Step 2: paginate the story API ───────────────────────────────────
        story_url = f"{TIKTOK_BASE}{_STORY_API_PATH}"
        referer = f"{TIKTOK_BASE}/@{username}"

        all_items: list[dict] = []
        envelope: dict | None = None
        cursor: int | str = 0
        has_more: bool = True
        page_num: int = 1

        while has_more:
            params: dict[str, str] = {
                "secUid": sec_uid,
                "count": "30",
                "cursor": str(cursor),
            }

            api_headers = {**_STORY_API_HEADERS, "Referer": referer}

            logger.info(
                "fetch_stories_for_user: page %d  cursor=%s  username=%r",
                page_num,
                cursor,
                username,
            )

            try:
                resp = await client.get(story_url, params=params, headers=api_headers)
            except httpx.RequestError as exc:
                raise TikTokBlockedError(
                    f"Network error fetching story page {page_num} for '@{username}': {exc}"
                ) from exc

            logger.debug(
                "fetch_stories_for_user: story API response  status=%d  page=%d",
                resp.status_code,
                page_num,
            )

            if resp.status_code in (403, 429):
                raise TikTokBlockedError(
                    f"TikTok blocked story API request for '@{username}' "
                    f"(HTTP {resp.status_code})."
                )
            if resp.status_code >= 500:
                raise TikTokBlockedError(
                    f"TikTok story API server error for '@{username}' "
                    f"(HTTP {resp.status_code})."
                )
            if resp.status_code != 200:
                raise TikTokBlockedError(
                    f"Unexpected HTTP {resp.status_code} from story API "
                    f"for '@{username}'."
                )

            try:
                body: dict = resp.json()
            except Exception as exc:
                raise TikTokBlockedError(
                    f"Story API returned non-JSON on page {page_num}: {exc}"
                ) from exc

            # Accumulate items from this page.
            page_items: list[dict] = body.get("itemList") or []
            all_items.extend(page_items)

            # Update pagination state.
            cursor = body.get("cursor") or body.get("minCursor") or 0
            has_more = bool(body.get("has_more") or body.get("hasMore"))

            # Save envelope metadata (everything except itemList) for the final
            # merged response.  We overwrite on each page so the envelope always
            # reflects the last page's metadata (status_code, etc.).
            envelope = {k: v for k, v in body.items() if k != "itemList"}

            logger.info(
                "fetch_stories_for_user: page %d done  "
                "page_items=%d  cursor=%s  has_more=%s  total=%d",
                page_num,
                len(page_items),
                cursor,
                has_more,
                len(all_items),
            )

            page_num += 1

            # Safety guard: TikTok sometimes returns has_more=true with an empty
            # itemList at the real end of the list.
            if not page_items:
                logger.info(
                    "fetch_stories_for_user: empty page — stopping pagination"
                )
                break

        # ── Step 3: validate & merge ─────────────────────────────────────────
        logger.info(
            "fetch_stories_for_user: pagination complete  username=%r  total_items=%d",
            username,
            len(all_items),
        )

        if not all_items:
            raise StoriesNotFoundError(
                f"User '@{username}' has no active stories."
            )

        merged: dict = {}
        if envelope:
            merged.update(envelope)
        merged["itemList"] = all_items

        return merged
