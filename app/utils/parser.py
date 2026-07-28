"""
parser.py - Transforms the raw TikTok Story API JSON into a clean,
            structured response dict.

All field access is defensive (using .get()) so that missing keys
never raise an exception and the endpoint always returns a valid response.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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

    # ── Path 1: music.playUrl is an object with urlList ───────────────────────
    play_url_obj = music.get("playUrl")
    if isinstance(play_url_obj, dict):
        url_list: list[str] = play_url_obj.get("urlList") or play_url_obj.get("url_list") or []
        if url_list and isinstance(url_list[0], str) and url_list[0]:
            return url_list[0]

    # ── Path 2: music.play_url (snake_case) is an object with url_list ────────
    play_url_obj_snake = music.get("play_url")
    if isinstance(play_url_obj_snake, dict):
        url_list = (
            play_url_obj_snake.get("url_list")
            or play_url_obj_snake.get("urlList")
            or []
        )
        if url_list and isinstance(url_list[0], str) and url_list[0]:
            return url_list[0]

    # ── Path 3: music.playUrl is a plain string (older API shape) ────────────
    if isinstance(play_url_obj, str) and play_url_obj:
        return play_url_obj

    # ── Path 4: music.play_url is a plain string ──────────────────────────────
    if isinstance(play_url_obj_snake, str) and play_url_obj_snake:
        return play_url_obj_snake

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
    item_list: list[dict] = data.get("itemList") or []

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
            author.get("avatarLarger")
            or author.get("avatarMedium")
            or author.get("avatarThumb")
        )

        stats: dict = first_item.get("authorStats") or {}
        followers = stats.get("followerCount")
        following = stats.get("followingCount")
        likes = stats.get("heartCount") or stats.get("diggCount")
        videos = stats.get("videoCount")

    # ── Stories ───────────────────────────────────────────────────────────────
    stories: list[dict] = []

    for item in item_list:
        item_id = item.get("id")
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
                        url_list: list[str] = (
                            (img.get("imageURL") or {}).get("urlList") or []
                        )
                        if url_list:
                            images.append(url_list[0])
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
                        "video_url": video.get("playAddr"),
                        "download_url": video.get("downloadAddr"),
                        "cover": video.get("cover"),
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
