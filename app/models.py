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


class StoriesPageResponse(BaseModel):
    """Response model for the /stories endpoint (Phase 2)."""
    success: bool
    username: str
    page: PageInfo


class ErrorResponse(BaseModel):
    """Generic error response model."""
    detail: str
