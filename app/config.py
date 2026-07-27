"""
config.py - Application configuration loaded from environment variables.

All settings are read from a .env file (or real env vars in production).
Pydantic-Settings handles validation and type coercion automatically.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Central settings object.

    Environment variables (case-insensitive):
        API_KEYS    Comma-separated list of valid API keys.
                    Example: API_KEYS=key1,key2,key3
        LOG_LEVEL   Python logging level string. Default: INFO.
        APP_NAME    Human-readable service name.
        APP_VERSION Semantic version string shown in / and /health.
    """

    # ── Authentication ────────────────────────────────────────────────────────
    # Stored as a raw comma-separated string; use get_key_set() for lookups.
    api_keys: str = "changeme"

    # ── Application metadata ──────────────────────────────────────────────────
    app_name: str = "TikTok Story API"
    app_version: str = "1.0.0"

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    @field_validator("api_keys", mode="before")
    @classmethod
    def _normalise_keys(cls, v: object) -> str:
        """Accept both a plain string and a list (defensive for edge cases)."""
        if isinstance(v, list):
            return ",".join(str(k) for k in v)
        return str(v)

    def get_key_set(self) -> frozenset[str]:
        """
        Return a frozenset of all valid API keys, trimmed of whitespace.
        O(1) membership test; computed fresh each call (config is read-once).
        """
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Singleton settings instance used across the entire application.
settings = Settings()

