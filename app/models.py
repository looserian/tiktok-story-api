"""
models.py - Pydantic request/response models for the TikTok Story API.

Every endpoint returns one of these typed models — no raw dicts are ever
serialised directly to the client.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


# ── Generic ───────────────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """
    Standard error envelope returned by every failed request.

    Example::

        {"success": false, "error": "Invalid API key."}
    """

    success: bool = Field(False, examples=[False])
    error: str = Field(..., examples=["Invalid API key."])

    model_config = {
        "json_schema_extra": {
            "examples": [{"success": False, "error": "Invalid API key."}]
        }
    }


# ── Utility endpoints ─────────────────────────────────────────────────────────

class RootResponse(BaseModel):
    """Response model for ``GET /``."""

    name: str = Field(..., examples=["TikTok Story API"])
    version: str = Field(..., examples=["1.0.0"])
    status: str = Field(..., examples=["running"])

    model_config = {
        "json_schema_extra": {
            "examples": [{"name": "TikTok Story API", "version": "1.0.0", "status": "running"}]
        }
    }


class HealthResponse(BaseModel):
    """Response model for ``GET /health``."""

    status: str = Field(..., examples=["ok"])
    version: str = Field(..., examples=["1.0.0"])

    model_config = {
        "json_schema_extra": {
            "examples": [{"status": "ok", "version": "1.0.0"}]
        }
    }


# ── Stories ───────────────────────────────────────────────────────────────────

class ParsedStory(BaseModel):
    """
    A single TikTok story item — either an image slide-show or a video.

    All fields are optional because TikTok's API schema may vary and we apply
    defensive parsing.
    """

    id: str | None = Field(None, examples=["7380000000000000000"])
    type: str | None = Field(None, examples=["image"])          # "image" | "video"
    created_at: int | None = Field(None, examples=[1714000000])
    expires_at: int | None = Field(None, examples=[1714086400])

    # ── Image story fields ────────────────────────────────────────────────────
    images: list[str] | None = Field(
        None,
        examples=[["https://p16.tiktokcdn.com/obj/example.webp"]],
    )

    # ── Audio (image stories with background music) ───────────────────────────
    # Present on image stories that have a TikTok sound attached.
    # Always ``null`` for video stories (the audio is part of the video stream).
    audio_url: str | None = Field(
        None,
        description=(
            "Direct URL to the background audio track for image stories. "
            "``null`` when the story has no audio or for video stories."
        ),
        examples=["https://sf16-ies-music.tiktokcdn.com/obj/musically-maliva-obj/example.mp3"],
    )

    # ── Video story fields ────────────────────────────────────────────────────
    video_url: str | None = Field(None, examples=["https://v19.tiktok.com/..."])
    download_url: str | None = Field(None, examples=["https://v19.tiktok.com/..."])
    cover: str | None = Field(None, examples=["https://p16.tiktokcdn.com/..."])
    duration: int | None = Field(None, examples=[15])
    views: int | None = Field(None, examples=[42000])
    likes: int | None = Field(None, examples=[3200])


class ParsedStoriesResponse(BaseModel):
    """
    Full response returned by ``GET /stories``.

    Contains account information extracted from the first story item, plus
    the complete list of parsed story objects.
    """

    success: bool = Field(..., examples=[True])
    username: str | None = Field(None, examples=["rtrt2805"])
    nickname: str | None = Field(None, examples=["RT"])
    avatar: str | None = Field(None, examples=["https://p16.tiktokcdn.com/..."])
    followers: int | None = Field(None, examples=[12500])
    following: int | None = Field(None, examples=[340])
    likes: int | None = Field(None, examples=[95000])
    videos: int | None = Field(None, examples=[87])
    story_count: int = Field(0, examples=[5])
    stories: list[ParsedStory] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "success": True,
                    "username": "rtrt2805",
                    "nickname": "RT",
                    "avatar": "https://p16.tiktokcdn.com/avatar.webp",
                    "followers": 12500,
                    "following": 340,
                    "likes": 95000,
                    "videos": 87,
                    "story_count": 2,
                    "stories": [
                        {
                            "id": "7380000000000000001",
                            "type": "image",
                            "created_at": 1714000000,
                            "expires_at": 1714086400,
                            "images": ["https://p16.tiktokcdn.com/img.webp"],
                        },
                        {
                            "id": "7380000000000000002",
                            "type": "video",
                            "created_at": 1714000100,
                            "expires_at": 1714086500,
                            "video_url": "https://v19.tiktok.com/play.mp4",
                            "download_url": "https://v19.tiktok.com/dl.mp4",
                            "cover": "https://p16.tiktokcdn.com/cover.webp",
                            "duration": 15,
                            "views": 42000,
                            "likes": 3200,
                        },
                    ],
                }
            ]
        }
    }


# ── Latest story (duplicate-detection endpoint) ───────────────────────────────

class LatestStoryResponse(BaseModel):
    """
    Response model for ``GET /stories/latest``.

    Includes a ``new_story`` flag that indicates whether the newest story
    has changed since the last time this endpoint was called for *username*.
    """

    success: bool = Field(..., examples=[True])
    username: str = Field(..., examples=["rtrt2805"])
    new_story: bool = Field(
        ...,
        description=(
            "``true`` if this story ID has not been seen before for this username, "
            "``false`` if it was already returned in a previous call."
        ),
        examples=[True],
    )
    latest_story: ParsedStory = Field(..., description="The newest story object.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "success": True,
                    "username": "rtrt2805",
                    "new_story": True,
                    "latest_story": {
                        "id": "7380000000000000002",
                        "type": "video",
                        "created_at": 1714000100,
                        "expires_at": 1714086500,
                        "video_url": "https://v19.tiktok.com/play.mp4",
                        "download_url": "https://v19.tiktok.com/dl.mp4",
                        "cover": "https://p16.tiktokcdn.com/cover.webp",
                        "duration": 15,
                        "views": 42000,
                        "likes": 3200,
                    },
                }
            ]
        }
    }
