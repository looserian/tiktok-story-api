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

import asyncio
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TIKTOK_BASE      = "https://www.tiktok.com"
_USER_DETAIL_PATH = "/api/user/detail/"
_STORY_API_PATH   = "/api/story/item_list/"
_USER_STORY_PATH  = "/api/user/story/"         # secondary fallback endpoint

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

_MS_TOKEN_ALPHABET = string.ascii_letters + string.digits


def _make_cookies_dict() -> dict[str, str]:
    """
    Build a browser-realistic cookies dict for each story-API request.

    - ``ttwid``   — fixed plausible URL-encoded pipe-separated string.
    - ``msToken`` — 107-character random alphanumeric string, freshly generated
      per call so repeated requests don't look like a replayed session.

    Returns a plain dict so callers can pass it directly to
    ``httpx.AsyncClient(cookies=...)`` or merge into existing cookies.
    """
    ms_token = "".join(random.choices(_MS_TOKEN_ALPHABET, k=107))
    return {
        "ttwid":   "1%7CfakeBase64EncodedTTWidValue%7C1700000000%7Cabcdef1234567890abcdef1234567890abcdef12",
        "msToken": ms_token,
    }


# Keep the old name as an alias so _layer2_json_api (which still builds its
# own cookie string) is unaffected.
def _make_story_cookies() -> str:
    """Legacy string form — used by old callers.  Prefer _make_cookies_dict()."""
    d = _make_cookies_dict()
    return f"ttwid={d['ttwid']}; msToken={d['msToken']}"

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

async def fetch_tiktok_stories_direct(
    author_id: str,
    sec_uid: str = "",
    username: str = "",
) -> dict:
    """
    Fetch all stories for a known ``author_id`` / ``sec_uid`` pair.

    Uses a two-endpoint strategy with a mandatory full parameter set.

    Primary — ``/api/story/item_list/``
    ------------------------------------
    All mandatory query params sent on every page request::

        author_id        — numeric user ID
        user_id          — same as author_id (TikTok requires both keys)
        secUid           — TikTok secUid
        count            — 30
        cursor           — pagination cursor (starts at 0)
        story_type       — 0
        mode             — 1
        aid              — 1988
        app_name         — tiktok_web
        device_platform  — web_pc
        client_type      — inbox
        msToken          — fresh 107-char random alphanumeric per request
        WebIdLastTime    — current UNIX timestamp

    Item extraction checks all known response key variants::

        items  →  itemList  →  aweme_list

    statusCode 10201 on primary → trigger secondary immediately (not a crash).

    Secondary fallback — ``/api/user/story/``
    ------------------------------------------
    Triggered when primary returns 0 items OR statusCode 10201.
    Params: author_id, secUid, aid, app_name, device_platform.

    HTTP 400 / 403 handling
    -----------------------
    For the PRIMARY endpoint: log body at WARNING then try secondary.
    For the SECONDARY endpoint: log body and return zero-stories.
    All non-200 bodies are logged in full (up to 800 chars).

    Cookies
    -------
    Each request gets its own ``httpx.AsyncClient`` instance with fresh
    ``ttwid`` + ``msToken`` cookies injected via ``httpx`` cookies kwarg
    (real cookie jar, not a raw header string).

    Args:
        author_id: Numeric TikTok author_id string.
        sec_uid:   TikTok secUid.  Strongly recommended; many accounts return
                   0 stories or statusCode 10201 without it.

    Returns:
        Merged dict with ``"itemList"`` key containing all collected stories,
        or ``{"itemList": [], "status_code": 0}`` when both endpoints confirm
        zero active stories.

    Raises:
        StoriesNotFoundError: Both endpoints confirm user has no active stories.
        TikTokBlockedError:   Network error or unexpected HTTP status code.
    """

    # ── Inner helpers scoped to this call ─────────────────────────────────────

    def _build_primary_params(cursor: int | str, ms_token: str) -> dict[str, str]:
        """Full mandatory param dict for /api/story/item_list/."""
        p: dict[str, str] = {
            "author_id":       author_id,
            "user_id":         author_id,   # TikTok requires both keys
            "count":           "30",
            "cursor":          str(cursor),
            "story_type":      "0",
            "mode":            "1",
            "aid":             _AID,
            "app_name":        "tiktok_web",
            "device_platform": "web_pc",
            "client_type":     "inbox",
            "msToken":         ms_token,
            "WebIdLastTime":   str(int(time.time())),
        }
        if sec_uid:
            p["secUid"] = sec_uid
        return p

    def _build_secondary_params() -> dict[str, str]:
        """Param dict for /api/user/story/ fallback."""
        p: dict[str, str] = {
            "author_id":       author_id,
            "aid":             _AID,
            "app_name":        "tiktok_web",
            "device_platform": "web_pc",
        }
        if sec_uid:
            p["secUid"] = sec_uid
        return p

    def _extract_items(body: dict) -> list[dict]:
        """Extract story items regardless of which key TikTok used."""
        return (
            body.get("items")
            or body.get("itemList")
            or body.get("aweme_list")
            or []
        )

    story_base_headers = {
        **_BASE_API_HEADERS,
        "Referer": f"{TIKTOK_BASE}/",
    }

    async def _do_request(
        url: str,
        params: dict[str, str],
        label: str,
        *,
        try_secondary_on_400: bool = False,
    ) -> dict | None:
        """
        Fire one authenticated GET request.

        - Injects fresh cookies via ``httpx.AsyncClient(cookies=...)``.
        - Logs the FULL response body (up to 800 chars) on any non-200 status.
        - Returns parsed JSON dict on HTTP 200.
        - Returns ``None`` on 400/403 (caller decides whether to try secondary).
        - Raises ``TikTokBlockedError`` on 429 / 5xx / other non-200.
        """
        cookies = _make_cookies_dict()
        async with _make_client() as client:
            try:
                resp = await client.get(
                    url,
                    params=params,
                    headers=story_base_headers,
                    cookies=cookies,
                )
            except httpx.RequestError as exc:
                raise TikTokBlockedError(f"{label}: network error — {exc}") from exc

        logger.debug("%s: HTTP %d", label, resp.status_code)

        if resp.status_code in (400, 403):
            logger.warning(
                "%s: HTTP %d — raw body: %r",
                label, resp.status_code, resp.text[:800],
            )
            return None   # caller will try secondary or give up

        if resp.status_code == 429:
            raise TikTokBlockedError(f"{label}: rate-limited (HTTP 429).")
        if resp.status_code >= 500:
            raise TikTokBlockedError(
                f"{label}: server error (HTTP {resp.status_code}).  "
                f"Body: {resp.text[:400]}"
            )
        if resp.status_code != 200:
            logger.warning(
                "%s: unexpected HTTP %d — body: %r",
                label, resp.status_code, resp.text[:800],
            )
            raise TikTokBlockedError(
                f"{label}: unexpected HTTP {resp.status_code}."
            )

        try:
            return resp.json()
        except Exception as exc:
            raise TikTokBlockedError(
                f"{label}: non-JSON response — {exc}"
            ) from exc

    # ── Secondary endpoint helper ───────────────────────────────────────────────

    secondary_url = f"{TIKTOK_BASE}{_USER_STORY_PATH}"

    async def _try_secondary(reason: str) -> list[dict]:
        """
        Hit /api/user/story/ and return its item list (may be empty).
        Swallows errors from the secondary gracefully — they are logged
        but never propagated so the caller can still return zero-stories.
        """
        logger.info(
            "fetch_tiktok_stories_direct: %s — trying secondary /api/user/story/",
            reason,
        )
        try:
            body2 = await _do_request(
                secondary_url,
                _build_secondary_params(),
                label="fetch_tiktok_stories_direct/secondary",
            )
        except TikTokBlockedError as exc:
            logger.warning(
                "fetch_tiktok_stories_direct [secondary]: error — %s", exc
            )
            return []

        if body2 is None:
            logger.info(
                "fetch_tiktok_stories_direct [secondary]: 400/403 — giving up."
            )
            return []

        # Check internal status code; 10201 on secondary means genuinely no stories.
        sc = body2.get("statusCode") or body2.get("status_code") or 0
        if sc not in (0, None):
            logger.warning(
                "fetch_tiktok_stories_direct [secondary]: statusCode=%s — body: %r",
                sc, str(body2)[:400],
            )
            return []

        items = _extract_items(body2)
        logger.info(
            "fetch_tiktok_stories_direct [secondary]: found %d items", len(items)
        )
        return items

    # ── Primary endpoint: /api/story/item_list/ ────────────────────────────────
    primary_url = f"{TIKTOK_BASE}{_STORY_API_PATH}"

    all_items:  list[dict] = []
    envelope:   dict | None = None
    cursor:     int | str = 0
    has_more:   bool = True
    page_num:   int  = 1
    use_secondary = False   # set to True when primary signals we should fall back

    while has_more and not use_secondary:
        ms_token = "".join(random.choices(_MS_TOKEN_ALPHABET, k=107))
        params   = _build_primary_params(cursor, ms_token)

        logger.info(
            "fetch_tiktok_stories_direct [primary]: page %d  cursor=%s  "
            "author_id=%s  sec_uid=%s",
            page_num, cursor, author_id,
            (sec_uid[:20] + "…") if sec_uid else "(none)",
        )

        body = await _do_request(
            primary_url, params,
            label=f"fetch_tiktok_stories_direct/primary page {page_num}",
        )

        if body is None:
            # 400 / 403 from primary — log already done; try secondary.
            logger.info(
                "fetch_tiktok_stories_direct [primary]: 400/403 on page %d — "
                "falling back to secondary.",
                page_num,
            )
            use_secondary = True
            break

        # Check TikTok's internal status code.
        sc = body.get("statusCode") or body.get("status_code") or 0
        if sc == 10201:
            # "Missing required fields" or "user not found" — try secondary.
            logger.warning(
                "fetch_tiktok_stories_direct [primary]: statusCode=10201 on "
                "page %d (author_id=%s) — falling back to secondary.",
                page_num, author_id,
            )
            use_secondary = True
            break
        elif sc != 0:
            # Any other non-zero internal code — log it, then also try secondary.
            logger.warning(
                "fetch_tiktok_stories_direct [primary]: non-zero statusCode=%s "
                "on page %d — body: %r",
                sc, page_num, str(body)[:400],
            )
            use_secondary = True
            break

        page_items = _extract_items(body)
        all_items.extend(page_items)

        cursor   = body.get("cursor") or body.get("minCursor") or 0
        has_more = bool(body.get("has_more") or body.get("hasMore"))
        envelope = {k: v for k, v in body.items()
                    if k not in ("items", "itemList", "aweme_list")}

        logger.info(
            "fetch_tiktok_stories_direct [primary]: page %d done  "
            "page_items=%d  has_more=%s  total=%d",
            page_num, len(page_items), has_more, len(all_items),
        )

        page_num += 1

        if not page_items:
            logger.info(
                "fetch_tiktok_stories_direct [primary]: empty page — stopping"
            )
            break

    # ── Secondary fallback ────────────────────────────────────────────────────────
    if not all_items:   # covers both use_secondary=True and genuine 0 pages
        secondary_items = await _try_secondary(
            reason=("primary HTTP 400/403 or statusCode error"
                    if use_secondary else "primary returned 0 items")
        )
        if secondary_items:
            all_items = secondary_items
            # envelope stays None; the merged dict will only have itemList

    logger.info(
        "fetch_tiktok_stories_direct: complete  author_id=%s  total_items=%d",
        author_id, len(all_items),
    )

    if not all_items:
        if username:
            logger.info(
                "fetch_tiktok_stories_direct: Level 1 httpx returned 0 items for author_id=%s. "
                "Triggering Level 2 Playwright interceptor fallback for username=%r",
                author_id, username,
            )
            try:
                return await fetch_stories_via_playwright_interceptor(username)
            except StoriesNotFoundError:
                raise
            except Exception as exc:
                logger.warning(
                    "fetch_tiktok_stories_direct: Level 2 Playwright interceptor failed for username=%r: %s",
                    username, exc,
                )
        raise StoriesNotFoundError(
            f"No active stories found for author_id={author_id!r}."
        )

    merged: dict = {}
    if envelope:
        merged.update(envelope)
    merged["itemList"] = all_items
    return merged


# ── Playwright Interceptor Fallback (Level 2) ─────────────────────────────────

async def fetch_stories_via_playwright_interceptor(username: str) -> dict:
    """
    Fallback Layer (Level 2): Launch headless Chromium with stealth flags,
    navigate to TikTok profile page, intercept story XHR responses
    (matching /api/story/item_list/ or /api/user/story/), and capture raw items.

    Enforces a strict 15-second execution timeout to prevent hangs.
    """
    profile_url = f"{TIKTOK_BASE}/@{username}"
    logger.info(
        "fetch_stories_via_playwright_interceptor: starting for username=%r", username
    )

    async def _interceptor_task() -> dict:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            logger.error("Playwright package not installed: %s", exc)
            raise TikTokBlockedError(
                "Playwright interceptor unavailable: playwright is not installed."
            ) from exc

        async with async_playwright() as p:
            browser = None
            context = None
            page = None
            captured_payload: dict = {}
            captured_items: list[dict] = []
            response_event = asyncio.Event()

            try:
                # Stealth chromium launch args
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-infobars",
                        "--window-position=0,0",
                        "--ignore-certificate-errors",
                        "--ignore-certificate-errors-spki-list",
                        f"--user-agent={_UA}",
                    ],
                )
                context = await browser.new_context(
                    user_agent=_UA,
                    viewport={"width": 1280, "height": 720},
                    device_scale_factor=1,
                    locale="en-US",
                )

                # Stealth init script to mask navigator.webdriver
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                page = await context.new_page()

                # Network response listener
                async def handle_response(response):
                    url = response.url
                    if "/api/story/item_list" in url or "/api/user/story" in url:
                        logger.info(
                            "Playwright interceptor: intercepted response url=%s status=%d",
                            url, response.status,
                        )
                        if response.status == 200:
                            try:
                                json_data = await response.json()
                                items = (
                                    json_data.get("items")
                                    or json_data.get("itemList")
                                    or json_data.get("aweme_list")
                                    or []
                                )
                                logger.info(
                                    "Playwright interceptor: extracted %d items from response",
                                    len(items),
                                )
                                if items:
                                    nonlocal captured_payload, captured_items
                                    captured_payload = json_data
                                    captured_items = items
                                    response_event.set()
                            except Exception as exc:
                                logger.warning(
                                    "Playwright interceptor: JSON parse error for %s: %s",
                                    url, exc,
                                )

                page.on("response", handle_response)

                logger.info("Playwright interceptor: navigating to %s", profile_url)
                try:
                    await page.goto(profile_url, wait_until="domcontentloaded", timeout=12000)
                except Exception as goto_exc:
                    logger.warning("Playwright interceptor: page.goto warning for '@%s': %s", username, goto_exc)

                if not captured_items:
                    try:
                        await asyncio.wait_for(response_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.info("Playwright interceptor: timeout waiting for response event")

            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

            if captured_items:
                merged: dict = {}
                if captured_payload:
                    envelope = {
                        k: v for k, v in captured_payload.items()
                        if k not in ("items", "itemList", "aweme_list")
                    }
                    merged.update(envelope)
                merged["itemList"] = captured_items
                merged["status_code"] = 0
                return merged

            raise StoriesNotFoundError(
                f"Playwright interceptor: No active stories found for '@{username}'."
            )

    try:
        # Enforce 15-second execution timeout
        return await asyncio.wait_for(_interceptor_task(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.error(
            "fetch_stories_via_playwright_interceptor: timed out (15s limit) for username=%r",
            username,
        )
        raise TikTokBlockedError(
            f"Playwright interceptor timed out after 15 seconds for user '@{username}'."
        )
    except (StoriesNotFoundError, TikTokBlockedError):
        raise
    except Exception as exc:
        logger.exception(
            "fetch_stories_via_playwright_interceptor failed for username=%r", username
        )
        raise TikTokBlockedError(
            f"Playwright interceptor failed for user '@{username}': {exc}"
        ) from exc


# ── Story fetcher ─────────────────────────────────────────────────────────────

async def fetch_stories_for_user(username: str) -> dict:
    """
    Full pipeline:
      Level 1 — Direct REST HTTP calls (httpx)
      Level 2 — Stealth Playwright XHR interceptor fallback

    Args:
        username: TikTok username without the leading ``@``.

    Returns:
        A merged dict with ``itemList`` containing all accumulated stories.

    Raises:
        UserNotFoundError:    User does not exist.
        StoriesNotFoundError: User exists but has no active stories.
        TikTokBlockedError:   TikTok rate-limited or blocked both levels.
    """
    # ── Level 1: Direct HTTP calls (httpx) ──────────────────────────────────
    try:
        async with _make_client() as client:
            author_id, sec_uid = await resolve_user_credentials(username, client)
            result = await fetch_tiktok_stories_direct(
                author_id, sec_uid=sec_uid, username=username
            )
            if result and result.get("itemList"):
                return result
    except UserNotFoundError:
        raise
    except Exception as exc:
        logger.warning(
            "fetch_stories_for_user: Level 1 httpx failed for '@%s': %s. "
            "Falling back to Level 2 Playwright interceptor.",
            username, exc,
        )

    # ── Level 2: Stealth Playwright Interceptor ─────────────────────────────
    logger.info(
        "fetch_stories_for_user: Triggering Level 2 Stealth Playwright Interceptor for '@%s'",
        username,
    )
    return await fetch_stories_via_playwright_interceptor(username)
