"""
tiktok_client.py - Direct async HTTP client for TikTok's internal JSON APIs.

Architecture
============

  resolve_user_credentials(username)   ← new multi-stage resolver
  ──────────────────────────────────
  Layer 1 — In-memory TTL cache (24 h)
      Returns immediately if the username was resolved recently.

  Layer 2 — JSON user-detail API
      GET /api/user/detail/?uniqueId={username}&aid=1988
      Enhanced headers: full Chrome UA, profile-page Referer, spoofed
      ``ttwid`` + ``msToken`` cookies.  Content-Type is inspected before
      calling ``.json()`` so an HTML challenge page never crashes the process.

  Layer 3 — HTML profile page + RegEx
      GET https://www.tiktok.com/@{username}  (raw HTML)
      Four patterns tried in order:
        "authorId":"(\\d+)"   "userId":"(\\d+)"   "id":"(\\d+)"   "secUid":"([^"]+)"

  Total failure → TikTokBlockedError with a user-friendly message.
  The exception is caught in scraper.py → fetch_page_info() which returns
  {"success": false, "error": "...", "status_code": 502} without crashing.

  fetch_stories_for_user(username)
  ────────────────────────────────
  Phase 2: paginate /api/story/item_list/ using the resolved author_id.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TIKTOK_BASE      = "https://www.tiktok.com"
_USER_DETAIL_PATH = "/api/user/detail/"
_STORY_API_PATH   = "/api/story/item_list/"

# TikTok Web app-ID — required parameter on all internal API calls.
_AID = "1988"

# Chrome 124 on Windows — matches the sec-ch-ua hint values below.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── Cookie spoof ──────────────────────────────────────────────────────────────
# Both ttwid and msToken are required by TikTok's bot-detection layer.
# These are plausible-looking dummy values; they are not real session tokens
# but help the request pass surface-level bot filters.
_SPOOF_TTWID = (
    "ttwid=1%7CfakeBase64EncodedTTWidValue%7C1700000000%7C"
    "abcdef1234567890abcdef1234567890abcdef12"
)
_SPOOF_MS_TOKEN = (
    "msToken=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB"
)
_SPOOF_COOKIES = f"{_SPOOF_TTWID}; {_SPOOF_MS_TOKEN}"

# Base headers shared by every TikTok internal API call.
# NOTE: Accept-Encoding is intentionally OMITTED here.
# httpx automatically adds "Accept-Encoding: gzip, br" and decompresses the
# response body before exposing resp.text / resp.json().  If we set the header
# manually, httpx still tries to decompress but the round-trip can produce
# binary artifacts (\x00\x00…) when the server uses Brotli — so we let httpx
# own the header end-to-end.
_BASE_API_HEADERS: dict[str, str] = {
    "User-Agent":        _UA,
    "Accept":            "application/json, text/plain, */*",
    "Accept-Language":   "en-US,en;q=0.9",
    "Cache-Control":     "no-cache",
    "Pragma":            "no-cache",
    "Sec-Ch-Ua":         '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":  "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest":    "empty",
    "Sec-Fetch-Mode":    "cors",
    "Sec-Fetch-Site":    "same-origin",
}

# Separate headers for HTML navigation requests (profile page fetches).
# Accept is overridden to match what a browser sends for a document request.
_BASE_HTML_HEADERS: dict[str, str] = {
    "User-Agent":                _UA,
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Cache-Control":             "no-cache",
    "Pragma":                    "no-cache",
    "Sec-Ch-Ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile":          "?0",
    "Sec-Ch-Ua-Platform":        '"Windows"',
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Upgrade-Insecure-Requests": "1",
}

# ── Regex patterns for the HTML-profile fallback ──────────────────────────────
# Applied in order; the first successful match wins.
_HTML_ID_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'"authorId"\s*:\s*"(\d+)"'),   # most common in recent SSR layouts
    re.compile(r'"userId"\s*:\s*"(\d+)"'),      # alternative key name
    re.compile(r'"id"\s*:\s*"(\d{6,})"'),       # generic numeric id (≥ 6 digits)
]
_HTML_SEC_UID_PATTERN = re.compile(r'"secUid"\s*:\s*"([^"]{20,})"')

# Cache TTL in seconds (24 hours).
_CACHE_TTL_SECONDS = 86_400


# ── Typed exceptions ──────────────────────────────────────────────────────────

class UserNotFoundError(Exception):
    """The TikTok user does not exist or is not accessible."""


class StoriesNotFoundError(Exception):
    """The user exists but currently has no active stories."""


class TikTokBlockedError(Exception):
    """TikTok returned a blocking, rate-limiting, or server-error response."""


# ── In-memory credential cache ────────────────────────────────────────────────

@dataclass
class _CachedCredentials:
    """Cached result of a successful username resolution."""
    author_id: str
    sec_uid:   str
    metadata:  dict = field(default_factory=dict)   # raw 'user' dict from TikTok
    cached_at: float = field(default_factory=time.monotonic)

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.cached_at) < _CACHE_TTL_SECONDS


# username (lowercased) → cached credentials
_credential_cache: dict[str, _CachedCredentials] = {}


def _cache_get(username: str) -> Optional[_CachedCredentials]:
    """Return a fresh cache entry for *username*, or None."""
    entry = _credential_cache.get(username.lower())
    if entry is None:
        return None
    if not entry.is_fresh():
        del _credential_cache[username.lower()]
        return None
    return entry


def _cache_set(
    username: str,
    author_id: str,
    sec_uid: str,
    metadata: dict | None = None,
) -> None:
    """Store credentials in the in-memory cache."""
    _credential_cache[username.lower()] = _CachedCredentials(
        author_id=author_id,
        sec_uid=sec_uid,
        metadata=metadata or {},
    )


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


def _is_json_content_type(resp: httpx.Response) -> bool:
    """
    Return True only when the response Content-Type signals JSON.

    TikTok occasionally serves an HTML security/challenge page with HTTP 200
    and a ``text/html`` Content-Type instead of the expected JSON payload.
    Checking Content-Type before calling ``.json()`` prevents a crash.
    """
    ct = resp.headers.get("content-type", "").lower()
    return "application/json" in ct or "text/javascript" in ct


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


# ── Layer 2: JSON user-detail API ─────────────────────────────────────────────

async def _layer2_json_api(
    username: str,
    client: httpx.AsyncClient,
) -> tuple[str, str, dict] | None:
    """
    Query TikTok's hidden user-detail JSON endpoint.

    Returns ``(author_id, sec_uid, user_dict)`` on success, or ``None`` if the
    response is not usable (challenge page, bad JSON, etc.).  Raises
    ``UserNotFoundError`` directly when TikTok confirms the user does not exist
    (HTTP 404 or statusCode 10201/10202/10203/10204) — there is no value in
    trying layer 3 for a confirmed-absent user.
    """
    url = f"{TIKTOK_BASE}{_USER_DETAIL_PATH}"
    params = {"uniqueId": username, "aid": _AID}
    headers = {
        **_BASE_API_HEADERS,
        "Referer": f"{TIKTOK_BASE}/@{username}",
        "Cookie":  _SPOOF_COOKIES,
    }

    logger.info("resolve_user_credentials [L2-JSON]: GET %s?uniqueId=%s", url, username)

    try:
        resp = await client.get(url, params=params, headers=headers)
    except httpx.RequestError as exc:
        logger.warning(
            "resolve_user_credentials [L2-JSON]: network error for '@%s': %s",
            username, exc,
        )
        return None

    logger.debug(
        "resolve_user_credentials [L2-JSON]: status=%d  content-type=%r  username=%r",
        resp.status_code,
        resp.headers.get("content-type", ""),
        username,
    )

    # HTTP 404 → user definitely does not exist; propagate immediately.
    if resp.status_code == 404:
        raise UserNotFoundError(
            f"TikTok returned HTTP 404 for user detail of '@{username}'."
        )

    # Non-200 or non-JSON → challenge/block; fall through to layer 3.
    if resp.status_code != 200 or not _is_json_content_type(resp):
        logger.warning(
            "resolve_user_credentials [L2-JSON]: non-JSON/non-200 for '@%s' "
            "(status=%d  content-type=%r).  Raw preview: %r",
            username,
            resp.status_code,
            resp.headers.get("content-type", ""),
            resp.text[:400].replace("\n", " "),
        )
        return None

    # Parse JSON safely.
    try:
        body: dict = resp.json()
    except Exception as json_exc:
        logger.warning(
            "resolve_user_credentials [L2-JSON]: JSON decode failed for '@%s': %s.  "
            "Raw preview: %r",
            username, json_exc,
            resp.text[:400].replace("\n", " "),
        )
        return None

    # TikTok internal status code.
    try:
        _check_tiktok_status(body, f"resolve_user_credentials/L2('@{username}')")
    except UserNotFoundError:
        raise  # confirmed not found — no point in trying HTML
    except TikTokBlockedError as exc:
        logger.warning(
            "resolve_user_credentials [L2-JSON]: status-code block for '@%s': %s",
            username, exc,
        )
        return None

    # Extract fields.
    user_info: dict = body.get("userInfo") or {}
    user: dict      = user_info.get("user") or {}
    author_id: str | None = user.get("id")
    sec_uid:   str | None = user.get("secUid")

    if not author_id:
        logger.warning(
            "resolve_user_credentials [L2-JSON]: 'id' field missing for '@%s'.",
            username,
        )
        return None

    logger.info(
        "resolve_user_credentials [L2-JSON]: resolved  username=%r  "
        "author_id=%s  sec_uid=%s",
        username, author_id, (sec_uid or "")[:20] + "…" if sec_uid else "",
    )
    return author_id, sec_uid or "", user


# ── Layer 3: HTML profile page + RegEx ───────────────────────────────────────

async def _layer3_html_regex(
    username: str,
    client: httpx.AsyncClient,
) -> tuple[str, str, dict] | None:
    """
    Fetch ``https://www.tiktok.com/@{username}`` and extract identifiers
    from TikTok's server-side-rendered JSON embedded in ``<script>`` tags.

    Returns ``(author_id, sec_uid, {})`` on success, ``None`` if no ID is found.
    Raises ``UserNotFoundError`` on HTTP 404.

    Uses ``resp.text`` (not ``resp.content``) so httpx's automatic decompression
    converts the raw gzip/Brotli bytes to a proper Unicode string before regex
    matching — binary artifacts like ``\x00\x00`` are never seen by the patterns.
    """
    profile_url = f"{TIKTOK_BASE}/@{username}"
    # _BASE_HTML_HEADERS already has the right Accept / Sec-Fetch-Dest values
    # for a browser navigation request.  Add the home-page Referer on top.
    headers = {**_BASE_HTML_HEADERS, "Referer": TIKTOK_BASE + "/"}

    logger.info("resolve_user_credentials [L3-HTML]: GET %s", profile_url)

    try:
        resp = await client.get(profile_url, headers=headers)
    except httpx.RequestError as exc:
        logger.warning(
            "resolve_user_credentials [L3-HTML]: network error for '@%s': %s",
            username, exc,
        )
        return None

    logger.debug(
        "resolve_user_credentials [L3-HTML]: status=%d  username=%r",
        resp.status_code, username,
    )

    if resp.status_code == 404:
        raise UserNotFoundError(
            f"TikTok profile page returned HTTP 404 for '@{username}'."
        )

    if resp.status_code != 200:
        logger.warning(
            "resolve_user_credentials [L3-HTML]: unexpected HTTP %d for '@%s'.  "
            "Raw preview: %r",
            resp.status_code, username,
            resp.text[:400].replace("\n", " "),
        )
        return None

    html = resp.text

    # ── Try each ID pattern in order ─────────────────────────────────────────
    author_id: str | None = None
    for pattern in _HTML_ID_PATTERNS:
        match = pattern.search(html)
        if match:
            author_id = match.group(1)
            logger.info(
                "resolve_user_credentials [L3-HTML]: found author_id=%s "
                "via pattern %r  username=%r",
                author_id, pattern.pattern, username,
            )
            break

    if not author_id:
        snippet = html[:600].replace("\n", " ")
        logger.warning(
            "resolve_user_credentials [L3-HTML]: no author_id found in HTML  "
            "username=%r  snippet=%r",
            username, snippet,
        )
        return None

    # ── Try to extract secUid too (best-effort) ───────────────────────────────
    sec_uid = ""
    sec_uid_match = _HTML_SEC_UID_PATTERN.search(html)
    if sec_uid_match:
        sec_uid = sec_uid_match.group(1)
        logger.info(
            "resolve_user_credentials [L3-HTML]: found sec_uid  username=%r  "
            "sec_uid=%s…",
            username, sec_uid[:20],
        )

    return author_id, sec_uid, {}


# ── Public multi-stage resolver ───────────────────────────────────────────────

async def resolve_user_credentials(
    username: str,
    client: httpx.AsyncClient,
) -> tuple[str, str]:
    """
    Resolve *username* → ``(author_id, sec_uid)`` using a 3-layer strategy.

    Layer 1 — In-memory TTL cache (24 h)
        Returns immediately if the username was successfully resolved recently,
        eliminating redundant HTTP requests for the same user.

    Layer 2 — JSON user-detail API
        GET /api/user/detail/?uniqueId={username}&aid=1988
        Sends spoofed ``ttwid`` + ``msToken`` cookies and a profile-page
        Referer so the request looks like a legitimate SPA XHR.  Content-Type
        is checked before parsing to handle HTML challenge pages gracefully.

    Layer 3 — HTML profile + RegEx
        GET https://www.tiktok.com/@{username}
        Four regex patterns are tried in priority order so minor page-layout
        changes are absorbed without a code update.

    On success the resolved ``(author_id, sec_uid)`` is written to the cache
    before returning.

    Args:
        username: TikTok username without the leading ``@``.
        client:   A live ``httpx.AsyncClient`` to reuse.

    Returns:
        ``(author_id, sec_uid)`` — both are strings.  ``sec_uid`` may be an
        empty string if the HTML fallback succeeded but the secUid regex did
        not match.

    Raises:
        UserNotFoundError:   User does not exist, is private, or is banned.
        TikTokBlockedError:  All layers failed (anti-bot active / IP throttled).
    """
    key = username.lower()

    # ── Layer 1: in-memory cache ──────────────────────────────────────────────
    cached = _cache_get(key)
    if cached is not None:
        logger.info(
            "resolve_user_credentials [L1-cache]: HIT  username=%r  author_id=%s",
            username, cached.author_id,
        )
        return cached.author_id, cached.sec_uid

    logger.info("resolve_user_credentials [L1-cache]: MISS  username=%r", username)

    # ── Layer 2: JSON API ─────────────────────────────────────────────────────
    result = await _layer2_json_api(username, client)

    # ── Layer 3: HTML regex (only if layer 2 did not produce a result) ────────
    if result is None:
        logger.info(
            "resolve_user_credentials: L2 failed for '@%s', trying L3 HTML.", username
        )
        result = await _layer3_html_regex(username, client)

    # ── Total failure ─────────────────────────────────────────────────────────
    if result is None:
        raise TikTokBlockedError(
            "TikTok anti-bot active. Failed to resolve user profile."
        )

    author_id, sec_uid, metadata = result

    # Populate cache so the next call for the same user is instant.
    _cache_set(key, author_id, sec_uid, metadata)
    logger.info(
        "resolve_user_credentials: cached  username=%r  author_id=%s", username, author_id
    )

    return author_id, sec_uid


# ── Backwards-compatible thin wrapper ────────────────────────────────────────

async def resolve_author_id(username: str, client: httpx.AsyncClient) -> str:
    """
    Backwards-compatible wrapper around ``resolve_user_credentials``.

    Returns only the ``author_id`` string.  All internal callers that already
    use this function continue to work without modification.
    """
    author_id, _ = await resolve_user_credentials(username, client)
    return author_id


# ── Direct story fetcher (author_id already known) ────────────────────────────

async def fetch_tiktok_stories_direct(author_id: str) -> dict:
    """
    Paginate ``/api/story/item_list/`` when the caller already has a numeric
    ``author_id`` and wants to skip the username-resolution step entirely.

    This is the fast path used by the ``/stories`` route when the client
    supplies ``?author_id=<id>`` directly (e.g. from a cached n8n variable).

    Endpoint::

        GET /api/story/item_list/?author_id={author_id}&count=30
                                  &cursor={cursor}&aid=1988

    Args:
        author_id: Numeric TikTok author_id string (e.g. ``"6797910539677074437"``).

    Returns:
        Same merged dict as ``fetch_stories_for_user`` —
        ``{"itemList": [...], ...}``.

    Raises:
        StoriesNotFoundError: No active stories for this author_id.
        TikTokBlockedError:   TikTok rate-limited or blocked the request.
    """
    story_url     = f"{TIKTOK_BASE}{_STORY_API_PATH}"
    story_headers = {**_BASE_API_HEADERS, "Referer": TIKTOK_BASE + "/"}

    all_items: list[dict] = []
    envelope:  dict | None = None
    cursor:    int | str = 0
    has_more:  bool = True
    page_num:  int  = 1

    async with _make_client() as client:
        while has_more:
            params: dict[str, str] = {
                "author_id": author_id,
                "count":     "30",
                "cursor":    str(cursor),
                "aid":       _AID,
            }

            logger.info(
                "fetch_tiktok_stories_direct: page %d  cursor=%s  author_id=%s",
                page_num, cursor, author_id,
            )

            try:
                resp = await client.get(
                    story_url, params=params, headers=story_headers
                )
            except httpx.RequestError as exc:
                raise TikTokBlockedError(
                    f"fetch_tiktok_stories_direct: network error on page {page_num} "
                    f"for author_id={author_id!r}: {exc}"
                ) from exc

            logger.debug(
                "fetch_tiktok_stories_direct: response  status=%d  page=%d",
                resp.status_code, page_num,
            )

            if resp.status_code in (403, 429):
                raise TikTokBlockedError(
                    f"TikTok blocked story API for author_id={author_id!r} "
                    f"(HTTP {resp.status_code})."
                )
            if resp.status_code >= 500:
                raise TikTokBlockedError(
                    f"TikTok story API server error for author_id={author_id!r} "
                    f"(HTTP {resp.status_code})."
                )
            if resp.status_code != 200:
                raise TikTokBlockedError(
                    f"Unexpected HTTP {resp.status_code} from story API "
                    f"for author_id={author_id!r}."
                )

            try:
                body: dict = resp.json()
            except Exception as exc:
                raise TikTokBlockedError(
                    f"fetch_tiktok_stories_direct: non-JSON response on page "
                    f"{page_num} for author_id={author_id!r}: {exc}"
                ) from exc

            _check_tiktok_status(
                body, f"fetch_tiktok_stories_direct(author_id={author_id!r}) page {page_num}"
            )

            page_items: list[dict] = body.get("itemList") or []
            all_items.extend(page_items)

            cursor   = body.get("cursor") or body.get("minCursor") or 0
            has_more = bool(body.get("has_more") or body.get("hasMore"))
            envelope = {k: v for k, v in body.items() if k != "itemList"}

            logger.info(
                "fetch_tiktok_stories_direct: page %d done  "
                "page_items=%d  cursor=%s  has_more=%s  total=%d",
                page_num, len(page_items), cursor, has_more, len(all_items),
            )

            page_num += 1

            if not page_items:
                logger.info(
                    "fetch_tiktok_stories_direct: empty page — stopping pagination"
                )
                break

    logger.info(
        "fetch_tiktok_stories_direct: done  author_id=%s  total_items=%d",
        author_id, len(all_items),
    )

    if not all_items:
        raise StoriesNotFoundError(
            f"No active stories found for author_id={author_id!r}."
        )

    merged: dict = {}
    if envelope:
        merged.update(envelope)
    merged["itemList"] = all_items
    return merged


# ── Story fetcher ─────────────────────────────────────────────────────────────

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
        author_id, _ = await resolve_user_credentials(username, client)

        # ── Step 2: paginate the story API ────────────────────────────────────
        story_url    = f"{TIKTOK_BASE}{_STORY_API_PATH}"
        referer      = f"{TIKTOK_BASE}/@{username}"
        story_headers = {**_BASE_API_HEADERS, "Referer": referer}

        all_items: list[dict] = []
        envelope:  dict | None = None
        cursor:    int | str = 0
        has_more:  bool = True
        page_num:  int  = 1

        while has_more:
            params: dict[str, str] = {
                "author_id": author_id,
                "count":     "30",
                "cursor":    str(cursor),
                "aid":       _AID,
            }

            logger.info(
                "fetch_stories_for_user: page %d  cursor=%s  username=%r",
                page_num, cursor, username,
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
                resp.status_code, page_num,
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
            cursor   = body.get("cursor") or body.get("minCursor") or 0
            has_more = bool(body.get("has_more") or body.get("hasMore"))

            # Save envelope metadata (everything except itemList) so the final
            # merged response looks like one complete API page.
            envelope = {k: v for k, v in body.items() if k != "itemList"}

            logger.info(
                "fetch_stories_for_user: page %d done  "
                "page_items=%d  cursor=%s  has_more=%s  total=%d",
                page_num, len(page_items), cursor, has_more, len(all_items),
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
            username, len(all_items),
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
