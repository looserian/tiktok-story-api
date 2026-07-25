"""
config.py - Application configuration loaded from environment variables.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API key for Bearer token authentication
    api_key: str = "changeme"

    # Application metadata
    app_name: str = "TikTok Story API"
    app_version: str = "1.0"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Singleton settings instance used across the app
settings = Settings()
