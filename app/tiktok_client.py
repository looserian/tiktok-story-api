"""
tiktok_client.py - Direct async HTTP client for TikTok's internal JSON APIs.

Two-phase approach, both phases use plain JSON GET requests — no HTML
parsing, no regex, no browser automation:

  Phase 1 — resolve_author_id()
      GET https://www.tiktok.com/api/user/detail/?uniqueId={username}&aid=1988
      → extract the numeric ``author_id`` from TikTok's user-detail endpoint.
        Fails fast with a typed exception if the user is not found or the
        request is blocked before any story fetch is attempted.

  Phase 2 — fetch_stories_for_user()
      GET https://www.tiktok.com/api/story/item_list/
          ?author_id={author_id}&count=30&cursor={cursor}&aid=1988
      → paginate until ``has_more`` is falsy, accumulate all ``itemList``
        entries, return a merged dict shaped like a single API response page.

All requests use browser-mimicking headers (User-Agent, Referer,
Accept-Language) to blend in with organic Chrome traffic.  No cookies or
authentication tokens are sent, keeping every request 100 % anonymous.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TIKTOK_BASE = "https://www.tiktok.com"
_USER_DETAIL_PATH = "/api/user/detail/"
_STORY_API_PATH = "/api/story/item_list/"

# TikTok Web app-ID — required parameter on all internal API calls.
_AID = "1988"

# Chrome 124 on Windows.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Base headers shared by every TikTok internal API call.
# The ``Referer`` key is intentionally absent here — each call sets it
# individually to the most appropriate value.
_BASE_API_HEADERS: dict[str, str] = {
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


# ── Typed exceptions ──────────────────────────────────────────────────────────

class UserNotFoundError(Exception):
    """The TikTok user does not exist or is not accessible."""


class StoriesNotFoundError(Exception):
    """The user exists but currently has no active stories."""


class TikTokBlockedError(Exception):
    """TikTok returned a blocking, rate-limiting, or server-error response."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_client() -> httpx.AsyncClient:
    """
    Return an ``httpx.AsyncClient`` configured with sensible defaults.

    - 30 s total timeout, 10 s connect timeout.
    - HTTP/2 enabled (TikTok supports it; reduces fingerprint risk).
    - follow_redirects=True for transparent redirect handling.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        http2=True,
        follow_redirects=True,
    )


def _check_tiktok_status(body: dict, context: str) -> None:
    """
    Inspect TikTok's internal ``statusCode`` field and raise a typed exception
    if it indicates an error condition.

    TikTok's JSON APIs always embed their own status code inside the response
    body even when HTTP 200 is returned, for example::

        {"statusCode": 10202, "userInfo": {}}   # user not found
        {"statusCode": 0,     "userInfo": {...}} # success

    Known non-zero codes:
        10201 / 10202 — user does not exist or is not found
        10203         — user has been banned
        10204         — private account
    """
    status = body.get("statusCode") or body.get("status_code") or 0

    if status == 0:
        return  # success

    # User-not-found variants
    if status in (10201, 10202):
        raise UserNotFoundError(
            f"{context}: TikTok reports user not found (statusCode={status})."
        )

    # Private / banned account
    if status in (10203, 10204):
        raise UserNotFoundError(
            f"{context}: TikTok account is private or banned (statusCode={status})."
        )

    # Any other non-zero code is treated as a temporary block / unknown error.
    raise TikTokBlockedError(
        f"{context}: TikTok returned non-zero statusCode={status}."
    )


# ── Public API ────────────────────────────────────────────────────────────────

async def resolve_author_id(username: str, client: httpx.AsyncClient) -> str:
    """
    Call TikTok's user-detail JSON endpoint and return the numeric ``author_id``.

    Endpoint::

        GET /api/user/detail/?uniqueId={username}&aid=1988

    The response shape is::

        {
          "statusCode": 0,
          "userInfo": {
            "user": {
              "id": "123456789",      ← what we return
              "uniqueId": "username",
              "secUid": "MS4wLjAB...",
              ...
            },
            "stats": { ... }
          }
        }

    Args:
        username: TikTok username without the leading ``@``.
        client:   A live ``httpx.AsyncClient`` to reuse.

    Returns:
        The numeric ``id`` string (author_id) for the user.

    Raises:
        UserNotFoundError:   User does not exist, is private, or is banned.
        TikTokBlockedError:  HTTP-level block / rate-limit / server error.
    """
    url = f"{TIKTOK_BASE}{_USER_DETAIL_PATH}"
    params = {"uniqueId": username, "aid": _AID}
    headers = {**_BASE_API_HEADERS, "Referer": f"{TIKTOK_BASE}/"}

    logger.info(
        "resolve_author_id: GET %s?uniqueId=%s", url, username
    )

    try:
        resp = await client.get(url, params=params, headers=headers)
    except httpx.RequestError as exc:
        raise TikTokBlockedError(
            f"resolve_author_id: network error for '@{username}': {exc}"
        ) from exc

    logger.debug(
        "resolve_author_id: response  status=%d  username=%r",
        resp.status_code,
        username,
    )

    # ── HTTP-level errors ─────────────────────────────────────────────────────
    if resp.status_code == 404:
        raise UserNotFoundError(
            f"TikTok returned HTTP 404 for user detail of '@{username}'."
        )
    if resp.status_code in (403, 429):
        raise TikTokBlockedError(
            f"TikTok blocked user-detail request for '@{username}' "
            f"(HTTP {resp.status_code})."
        )
    if resp.status_code >= 500:
        raise TikTokBlockedError(
            f"TikTok server error on user-detail for '@{username}' "
            f"(HTTP {resp.status_code})."
        )
    if resp.status_code != 200:
        raise TikTokBlockedError(
            f"Unexpected HTTP {resp.status_code} from user-detail "
            f"for '@{username}'."
        )

    # ── Parse JSON ────────────────────────────────────────────────────────────
    try:
        body: dict = resp.json()
    except Exception as exc:
        raise TikTokBlockedError(
            f"resolve_author_id: non-JSON response for '@{username}': {exc}"
        ) from exc

    # ── TikTok internal status code ───────────────────────────────────────────
    _check_tiktok_status(body, f"resolve_author_id('@{username}')")

    # ── Extract author_id ─────────────────────────────────────────────────────
    user_info: dict = body.get("userInfo") or {}
    user: dict = user_info.get("user") or {}
    author_id: str | None = user.get("id")

    if not author_id:
        raise UserNotFoundError(
            f"resolve_author_id: 'id' field missing in TikTok user-detail "
            f"response for '@{username}'. "
            "The account may not exist or may be inaccessible."
        )

    logger.info(
        "resolve_author_id: resolved  username=%r  author_id=%s",
        username,
        author_id,
    )
    return author_id


async def fetch_stories_for_user(username: str) -> dict:
    """
    Full pipeline: resolve ``author_id`` → fetch and paginate the story API.

    Opens a single ``httpx.AsyncClient`` and reuses it for both the user-detail
    call and all story-API pages (connection pooling, shared TLS session).

    Endpoint::

        GET /api/story/item_list/?author_id={author_id}&count=30
                                  &cursor={cursor}&aid=1988

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
        UserNotFoundError:    User does not exist or author_id cannot be resolved.
        StoriesNotFoundError: User exists but has no active stories.
        TikTokBlockedError:   TikTok rate-limited or blocked the request.
    """
    async with _make_client() as client:
        # ── Step 1: resolve author_id ─────────────────────────────────────────
        author_id = await resolve_author_id(username, client)

        # ── Step 2: paginate the story API ────────────────────────────────────
        story_url = f"{TIKTOK_BASE}{_STORY_API_PATH}"
        referer = f"{TIKTOK_BASE}/@{username}"
        story_headers = {**_BASE_API_HEADERS, "Referer": referer}

        all_items: list[dict] = []
        envelope: dict | None = None
        cursor: int | str = 0
        has_more: bool = True
        page_num: int = 1

        while has_more:
            params: dict[str, str] = {
                "author_id": author_id,
                "count": "30",
                "cursor": str(cursor),
                "aid": _AID,
            }

            logger.info(
                "fetch_stories_for_user: page %d  cursor=%s  username=%r",
                page_num,
                cursor,
                username,
            )

            try:
                resp = await client.get(
                    story_url, params=params, headers=story_headers
                )
            except httpx.RequestError as exc:
                raise TikTokBlockedError(
                    f"fetch_stories_for_user: network error on page {page_num} "
                    f"for '@{username}': {exc}"
                ) from exc

            logger.debug(
                "fetch_stories_for_user: story API response  status=%d  page=%d",
                resp.status_code,
                page_num,
            )

            # ── HTTP-level errors ─────────────────────────────────────────────
            if resp.status_code in (403, 429):
                raise TikTokBlockedError(
                    f"TikTok blocked story API for '@{username}' "
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

            # ── Parse JSON ────────────────────────────────────────────────────
            try:
                body: dict = resp.json()
            except Exception as exc:
                raise TikTokBlockedError(
                    f"fetch_stories_for_user: non-JSON response on page "
                    f"{page_num} for '@{username}': {exc}"
                ) from exc

            # ── TikTok internal status code ───────────────────────────────────
            _check_tiktok_status(
                body, f"fetch_stories_for_user('@{username}') page {page_num}"
            )

            # ── Accumulate items ──────────────────────────────────────────────
            page_items: list[dict] = body.get("itemList") or []
            all_items.extend(page_items)

            # Update pagination state.
            cursor = body.get("cursor") or body.get("minCursor") or 0
            has_more = bool(body.get("has_more") or body.get("hasMore"))

            # Save envelope metadata (everything except itemList) so the final
            # merged response looks like one complete API page.
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
                    "fetch_stories_for_user: empty page received — stopping pagination"
                )
                break

        # ── Step 3: validate & merge ──────────────────────────────────────────
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
