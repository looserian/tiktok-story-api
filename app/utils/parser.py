"""
parser.py - Transforms the raw TikTok Story API JSON into a clean,
            structured response dict.

All field access is defensive (using .get()) so that missing keys
never raise an exception and the endpoint always returns a valid response.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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

                stories.append(
                    {
                        "id": item_id,
                        "type": "image",
                        "created_at": create_time,
                        "expires_at": expires_at,
                        "images": images,
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
                    }
                )

        except Exception as item_exc:  # noqa: BLE001
            logger.warning(
                "parse_story_response: skipping malformed item id=%s — %s",
                item_id,
                item_exc,
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
