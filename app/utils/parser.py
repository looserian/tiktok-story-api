"""
parser.py - Transforms the raw TikTok Story API JSON into a clean,
            structured response dict.

All field access is defensive (using .get()) so that missing keys
never raise an exception and the endpoint always returns a valid response.
"""

from __future__ import annotations

import logging

from typing import Any

logger = logging.getLogger(__name__)


def _extract_url(val: Any) -> str | None:
    """
    Extract a clean string URL from either a string or a TikTok URL object.

    TikTok API payloads may represent URLs as plain strings or dict objects
    containing a list of mirror URLs under ``urlList`` / ``url_list``.
    """
    if isinstance(val, str) and val:
        return val
    if isinstance(val, dict):
        url_list = val.get("urlList") or val.get("url_list") or []
        if url_list and isinstance(url_list[0], str) and url_list[0]:
            return url_list[0]
    return None


def _extract_audio_url(item: dict) -> str | None:
    """
    Return the best direct audio URL from a TikTok item dict, or ``None``.

    TikTok stores background music under the ``music`` key at the top level of
    each item (applies to both image and video stories).  The URL itself is
    nested a level deeper — the exact field name varies across API versions::

        item.music.playUrl.urlList[0]     # most common (camelCase)
        item.music.play_url.url_list[0]   # snake_case variant
        item.music.playUrl (str)          # older: direct string, not object

    We try all known paths in priority order and return the first non-empty URL.
    If TikTok provides no audio the function returns ``None``.
    """
    music: dict = item.get("music") or {}
    if not music:
        return None

    # ── Path 1: music.playUrl is an object or string ─────────────────────────
    play_url = _extract_url(music.get("playUrl"))
    if play_url:
        return play_url

    # ── Path 2: music.play_url (snake_case) is an object or string ──────────
    play_url_snake = _extract_url(music.get("play_url"))
    if play_url_snake:
        return play_url_snake

    return None


def parse_story_response(data: dict) -> dict:
    """
    Parse the raw TikTok Story API payload into a simplified response.

    Args:
        data: The raw JSON dict intercepted from TikTok's
              ``/api/story/item_list/`` endpoint.

    Returns:
        A cleaned dict with account info and a list of story objects.
        Never raises — missing fields are replaced with ``None`` / empty
        defaults so callers always receive a well-formed response.
    """
    item_list: list[dict] = (
        data.get("itemList")
        or data.get("items")
        or data.get("aweme_list")
        or []
    )

    logger.info(
        "parse_story_response: raw itemList from TikTok  count=%d",
        len(item_list),
    )

    # ── Account info ─────────────────────────────────────────────────────────
    # Grab author + authorStats from the first available item.
    username: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    followers: int | None = None
    following: int | None = None
    likes: int | None = None
    videos: int | None = None

    if item_list:
        first_item = item_list[0]

        author: dict = first_item.get("author") or {}
        username = author.get("uniqueId")
        nickname = author.get("nickname")

        # Avatar — prefer the larger "avatarLarger" URL, fall back to "avatarMedium"
        avatar = (
            _extract_url(author.get("avatarLarger"))
            or _extract_url(author.get("avatarMedium"))
            or _extract_url(author.get("avatarThumb"))
        )

        stats: dict = first_item.get("authorStats") or {}
        followers = stats.get("followerCount")
        following = stats.get("followingCount")
        likes = stats.get("heartCount") or stats.get("diggCount")
        videos = stats.get("videoCount")

    # ── Stories ───────────────────────────────────────────────────────────────
    stories: list[dict] = []

    for item in item_list:
        item_id = str(item.get("id")) if item.get("id") is not None else None
        create_time = item.get("createTime")

        # story.ExpiredAt may be nested under "story" key
        story_meta: dict = item.get("story") or {}
        expires_at = story_meta.get("ExpiredAt")

        try:
            if "imagePost" in item:
                # ── Image story ──────────────────────────────────────────────
                image_post: dict = item.get("imagePost") or {}
                raw_images: list[dict] = image_post.get("images") or []

                images: list[str] = []
                for img in raw_images:
                    try:
                        url_val = (
                            img.get("imageURL")
                            or img.get("display_image")
                            or img.get("image_url")
                            or img
                        )
                        extracted = _extract_url(url_val)
                        if extracted:
                            images.append(extracted)
                    except Exception as img_exc:  # noqa: BLE001
                        logger.warning(
                            "parse_story_response: skipping malformed image entry — %s",
                            img_exc,
                        )

                audio_url: str | None = _extract_audio_url(item)
                if audio_url:
                    logger.debug(
                        "parse_story_response: image story %s has audio", item_id
                    )

                stories.append(
                    {
                        "id": item_id,
                        "type": "image",
                        "created_at": create_time,
                        "expires_at": expires_at,
                        "images": images,
                        "audio_url": audio_url,
                    }
                )

            else:
                # ── Video story ──────────────────────────────────────────────
                video: dict = item.get("video") or {}
                item_stats: dict = item.get("stats") or {}

                stories.append(
                    {
                        "id": item_id,
                        "type": "video",
                        "created_at": create_time,
                        "expires_at": expires_at,
                        "video_url": _extract_url(video.get("playAddr") or video.get("play_addr")),
                        "download_url": _extract_url(video.get("downloadAddr") or video.get("download_addr")),
                        "cover": _extract_url(video.get("cover")),
                        "duration": video.get("duration"),
                        "views": item_stats.get("playCount"),
                        "likes": item_stats.get("diggCount"),
                        "audio_url": None,
                    }
                )

        except Exception as item_exc:  # noqa: BLE001
            logger.warning(
                "parse_story_response: skipping malformed item id=%s — %s",
                item_id,
                item_exc,
            )

    logger.info(
        "parse_story_response: final story count returned by API  count=%d",
        len(stories),
    )

    return {
        "success": True,
        "username": username,
        "nickname": nickname,
        "avatar": avatar,
        "followers": followers,
        "following": following,
        "likes": likes,
        "videos": videos,
        "story_count": len(stories),
        "stories": stories,
    }
