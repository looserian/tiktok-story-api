"""
models.py - Pydantic request/response models for the TikTok Story API.
"""

from typing import Any
from pydantic import BaseModel


class RootResponse(BaseModel):
    """Response model for the root endpoint."""
    name: str
    version: str


class HealthResponse(BaseModel):
    """Response model for the health check endpoint."""
    status: str


class Story(BaseModel):
    """
    Represents a single TikTok story item.
    Extend this model when the scraper is implemented.
    """
    id: str | None = None
    url: str | None = None
    thumbnail: str | None = None
    duration: int | None = None  # seconds
    metadata: dict[str, Any] | None = None


class StoriesResponse(BaseModel):
    """Response model for the /stories endpoint (Phase 1 — kept for compatibility)."""
    success: bool
    username: str
    stories: list[Story]


class PageInfo(BaseModel):
    """Metadata returned by the Playwright page visit."""
    title: str | None
    url: str
    html_length: int | None = None


class NetworkRequest(BaseModel):
    """A single captured network request/response pair."""
    url: str
    method: str
    status: int
    resource_type: str


class StoriesPageResponse(BaseModel):
    """Response model for the /stories endpoint (Phase 2 / Phase 3)."""
    success: bool
    username: str
    page: PageInfo
    network: list[NetworkRequest] = []


class ErrorResponse(BaseModel):
    """Generic error response model."""
    detail: str


class ParsedStory(BaseModel):
    """A single cleaned story item (image or video)."""

    id: str | None = None
    type: str | None = None          # "image" | "video"
    created_at: int | None = None
    expires_at: int | None = None
    # Image stories
    images: list[str] | None = None
    # Video stories
    video_url: str | None = None
    download_url: str | None = None
    cover: str | None = None
    duration: int | None = None
    views: int | None = None
    likes: int | None = None


class ParsedStoriesResponse(BaseModel):
    """Cleaned response returned by the /stories endpoint."""

    success: bool
    username: str | None = None
    nickname: str | None = None
    avatar: str | None = None
    followers: int | None = None
    following: int | None = None
    likes: int | None = None
    videos: int | None = None
    story_count: int = 0
    stories: list[ParsedStory] = []
