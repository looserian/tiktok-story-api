"""
tiktok_client.py - Direct async HTTP client for TikTok's internal JSON APIs.

Two-phase approach:

  Phase 1 — resolve_author_id()
      Primary:  GET https://www.tiktok.com/api/user/detail/?uniqueId={username}&aid=1988
                with enhanced headers (msToken cookie spoof, full Chrome UA, Referer).
                Content-Type is inspected before calling .json() — a challenge/HTML
                page is caught safely and triggers the fallback.
      Fallback: GET https://www.tiktok.com/@{username} as raw HTML, then parse
                ``author_id`` / ``secUid`` from embedded <script> JSON via regex.
      → Returns the numeric ``author_id`` string on success, or raises a typed
        exception if both tiers fail.

  Phase 2 — fetch_stories_for_user()
      GET https://www.tiktok.com/api/story/item_list/
          ?author_id={author_id}&count=30&cursor={cursor}&aid=1988
      → paginate until ``has_more`` is falsy, accumulate all ``itemList``
        entries, return a merged dict shaped like a single API response page.

All requests use browser-mimicking headers (User-Agent, Referer,
Accept-Language) to blend in with organic Chrome traffic.
"""

from __future__ import annotations

import logging
import re

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

# Enhanced user-detail headers: spoof a plausible msToken cookie value and
# provide the profile page as Referer so the request looks like an XHR made
# by TikTok's own SPA after a user navigated to a profile.
_USER_DETAIL_COOKIE = (
    "msToken=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB"
)

# Regex patterns used by the HTML fallback to extract identifiers from the
# server-side rendered JSON embedded in TikTok profile pages.
_RE_AUTHOR_ID = re.compile(r'"authorId"\s*:\s*"(\d+)"')
_RE_USER_ID   = re.compile(r'"userId"\s*:\s*"(\d+)"')
_RE_SEC_UID   = re.compile(r'"secUid"\s*:\s*"([^"]{20,})"')


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

def _is_json_content_type(resp: httpx.Response) -> bool:
    """
    Return True only when the response Content-Type signals JSON.

    TikTok occasionally serves an HTML security/challenge page with HTTP 200
    and a ``text/html`` Content-Type instead of the expected JSON payload.
    Checking Content-Type before calling ``.json()`` prevents a crash.
    """
    ct = resp.headers.get("content-type", "").lower()
    return "application/json" in ct or "text/javascript" in ct


async def _resolve_via_html_fallback(
    username: str,
    client: httpx.AsyncClient,
) -> str:
    """
    Fallback: fetch ``https://www.tiktok.com/@{username}`` as raw HTML and
    extract ``author_id`` (or ``userId``) from the embedded server-side JSON
    using regular expressions.

    TikTok's SSR page embeds multiple ``<script>`` blocks that contain the full
    user JSON.  The ``authorId`` / ``userId`` numeric field is reliably present
    in at least one of them.

    Args:
        username: TikTok username without the leading ``@``.
        client:   A live ``httpx.AsyncClient`` to reuse.

    Returns:
        The numeric author_id string.

    Raises:
        UserNotFoundError:  Profile page returned 404 or no ID found in HTML.
        TikTokBlockedError: Network error or non-200/404 HTTP status.
    """
    profile_url = f"{TIKTOK_BASE}/@{username}"
    html_headers = {
        **_BASE_API_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": TIKTOK_BASE + "/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }

    logger.info(
        "resolve_author_id [HTML fallback]: GET %s", profile_url
    )

    try:
        resp = await client.get(profile_url, headers=html_headers)
    except httpx.RequestError as exc:
        raise TikTokBlockedError(
            f"resolve_author_id HTML fallback: network error for '@{username}': {exc}"
        ) from exc

    logger.debug(
        "resolve_author_id [HTML fallback]: status=%d  username=%r",
        resp.status_code,
        username,
    )

    if resp.status_code == 404:
        raise UserNotFoundError(
            f"TikTok profile page returned HTTP 404 for '@{username}'."
        )
    if resp.status_code != 200:
        raise TikTokBlockedError(
            f"resolve_author_id HTML fallback: unexpected HTTP {resp.status_code} "
            f"for '@{username}'."
        )

    html = resp.text

    # Try authorId first (most common in newer page layouts), then userId.
    for pattern in (_RE_AUTHOR_ID, _RE_USER_ID):
        match = pattern.search(html)
        if match:
            author_id = match.group(1)
            logger.info(
                "resolve_author_id [HTML fallback]: found  username=%r  author_id=%s",
                username,
                author_id,
            )
            return author_id

    # Log a snippet to help debug future page-layout changes.
    snippet = html[:500].replace("\n", " ")
    logger.warning(
        "resolve_author_id [HTML fallback]: no author_id in HTML  "
        "username=%r  snippet=%r",
        username,
        snippet,
    )
    raise UserNotFoundError(
        f"resolve_author_id: could not extract author_id from TikTok profile page "
        f"for '@{username}'. The account may be private or the page layout changed."
    )


async def resolve_author_id(username: str, client: httpx.AsyncClient) -> str:
    """
    Resolve a TikTok username to its numeric ``author_id`` using a 2-tier strategy.

    Tier 1 — JSON API (primary)
    ---------------------------
    GET /api/user/detail/?uniqueId={username}&aid=1988

    Enhanced headers are sent (``msToken`` cookie spoof, profile-page Referer)
    to reduce the chance of receiving a challenge page.  Before calling
    ``.json()``, the response Content-Type is inspected: if TikTok returned
    HTML (challenge / CAPTCHA page) instead of JSON, the tier-1 attempt is
    abandoned and tier 2 is tried immediately.

    Tier 2 — HTML regex fallback
    ----------------------------
    GET https://www.tiktok.com/@{username}  (raw HTML)

    The ``authorId`` / ``userId`` numeric field is extracted directly from the
    server-side rendered JSON embedded in the page's ``<script>`` blocks.

    On success the numeric ``id`` string is returned.  If both tiers fail a
    typed exception is raised so the caller can surface the right HTTP status
    code and a human-readable error message.

    Response shape (tier 1 success)::

        {
          "statusCode": 0,
          "userInfo": {
            "user": {
              "id": "123456789",      ← returned
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
        TikTokBlockedError:  Both tiers failed due to rate-limiting / blocking.
    """
    url = f"{TIKTOK_BASE}{_USER_DETAIL_PATH}"
    params = {"uniqueId": username, "aid": _AID}

    # ── Tier 1: JSON API ──────────────────────────────────────────────────────
    # Use enhanced headers to look like a legitimate XHR from TikTok's SPA:
    #   • Referer set to the user's own profile page (most natural origin).
    #   • A plausible msToken cookie value to reduce bot-detection scores.
    tier1_headers = {
        **_BASE_API_HEADERS,
        "Referer": f"{TIKTOK_BASE}/@{username}",
        "Cookie": _USER_DETAIL_COOKIE,
    }

    logger.info(
        "resolve_author_id [tier-1]: GET %s?uniqueId=%s", url, username
    )

    tier1_failed = False  # set to True if we must fall through to tier 2

    try:
        resp = await client.get(url, params=params, headers=tier1_headers)
    except httpx.RequestError as exc:
        logger.warning(
            "resolve_author_id [tier-1]: network error for '@%s': %s — trying HTML fallback",
            username, exc,
        )
        tier1_failed = True
        resp = None  # type: ignore[assignment]

    if resp is not None:
        logger.debug(
            "resolve_author_id [tier-1]: status=%d  content-type=%r  username=%r",
            resp.status_code,
            resp.headers.get("content-type", ""),
            username,
        )

        # ── HTTP-level hard errors → do not fall back, raise immediately ──────
        if resp.status_code == 404:
            raise UserNotFoundError(
                f"TikTok returned HTTP 404 for user detail of '@{username}'."
            )

        # Non-200 OR non-JSON content-type → fall through to HTML fallback.
        if resp.status_code != 200 or not _is_json_content_type(resp):
            raw_preview = resp.text[:300].replace("\n", " ")
            logger.warning(
                "resolve_author_id [tier-1]: non-JSON or non-200 response for '@%s' "
                "(status=%d  content-type=%r) — falling back to HTML.  Preview: %r",
                username,
                resp.status_code,
                resp.headers.get("content-type", ""),
                raw_preview,
            )
            tier1_failed = True

        else:
            # ── Parse JSON safely ─────────────────────────────────────────────
            try:
                body: dict = resp.json()
            except Exception as json_exc:
                raw_preview = resp.text[:300].replace("\n", " ")
                logger.warning(
                    "resolve_author_id [tier-1]: JSON decode failed for '@%s': %s "
                    "— falling back to HTML.  Preview: %r",
                    username, json_exc, raw_preview,
                )
                tier1_failed = True
            else:
                # ── TikTok internal status code ───────────────────────────────
                try:
                    _check_tiktok_status(body, f"resolve_author_id('@{username}')")
                except UserNotFoundError:
                    raise  # propagate directly — no point in trying HTML
                except TikTokBlockedError:
                    logger.warning(
                        "resolve_author_id [tier-1]: TikTok status error for '@%s' "
                        "— falling back to HTML.",
                        username,
                    )
                    tier1_failed = True

                if not tier1_failed:
                    # ── Extract author_id ─────────────────────────────────────
                    user_info: dict = body.get("userInfo") or {}
                    user: dict = user_info.get("user") or {}
                    author_id: str | None = user.get("id")

                    if not author_id:
                        logger.warning(
                            "resolve_author_id [tier-1]: 'id' field missing for '@%s' "
                            "— falling back to HTML.",
                            username,
                        )
                        tier1_failed = True
                    else:
                        logger.info(
                            "resolve_author_id [tier-1]: resolved  username=%r  author_id=%s",
                            username, author_id,
                        )
                        return author_id

    # ── Tier 2: HTML regex fallback ───────────────────────────────────────────
    if tier1_failed:
        logger.info(
            "resolve_author_id: tier-1 failed for '@%s', attempting HTML fallback.",
            username,
        )
        # _resolve_via_html_fallback raises UserNotFoundError or TikTokBlockedError
        # on failure, which propagate naturally to the caller.
        return await _resolve_via_html_fallback(username, client)

    # Should be unreachable, but satisfies type-checkers.
    raise TikTokBlockedError(
        f"resolve_author_id: failed to resolve '@{username}' via any strategy. "
        "Account may be private or IP throttled."
    )


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
