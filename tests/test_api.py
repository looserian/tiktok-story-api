"""
test_api.py - Unit and integration tests for TikTok Story API.
"""

from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.utils.parser import _extract_url, parse_story_response
from app.utils.story_store import get_last_story_id, set_last_story_id, read_last_stories

client = TestClient(app)

VALID_KEY = next(iter(settings.get_key_set())) if settings.get_key_set() else "changeme"


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == settings.app_name
    assert data["version"] == settings.app_version
    assert data["status"] == "running"


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == settings.app_version


def test_auth_missing_key():
    response = client.get("/stories?username=testuser")
    assert response.status_code == 401
    data = response.json()
    assert data["success"] is False
    assert "error" in data


def test_auth_invalid_key():
    response = client.get(
        "/stories?username=testuser",
        headers={"X-API-Key": "invalid-secret-key-12345"},
    )
    assert response.status_code == 401
    data = response.json()
    assert data["success"] is False
    assert data["error"] == "Invalid API key."


def test_auth_valid_x_api_key(monkeypatch):
    mock_fetch = AsyncMock(return_value={
        "success": True,
        "story_json": {
            "itemList": [
                {
                    "id": "7380000000000000001",
                    "createTime": 1714000000,
                    "author": {"uniqueId": "testuser", "nickname": "Test User"},
                    "video": {"playAddr": "https://v19.tiktok.com/play.mp4"},
                }
            ]
        }
    })
    monkeypatch.setattr("app.routes.fetch_page_info", mock_fetch)

    response = client.get(
        "/stories?username=testuser",
        headers={"X-API-Key": VALID_KEY},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["username"] == "testuser"
    assert data["story_count"] == 1


def test_auth_valid_bearer_token(monkeypatch):
    mock_fetch = AsyncMock(return_value={
        "success": True,
        "story_json": {
            "itemList": [
                {
                    "id": "7380000000000000001",
                    "createTime": 1714000000,
                    "author": {"uniqueId": "testuser", "nickname": "Test User"},
                    "video": {"playAddr": "https://v19.tiktok.com/play.mp4"},
                }
            ]
        }
    })
    monkeypatch.setattr("app.routes.fetch_page_info", mock_fetch)

    response = client.get(
        "/stories?username=testuser",
        headers={"Authorization": f"Bearer {VALID_KEY}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True


def test_latest_story_duplicate_detection(monkeypatch, tmp_path):
    mock_fetch = AsyncMock(return_value={
        "success": True,
        "story_json": {
            "itemList": [
                {
                    "id": "9990000000000000001",
                    "createTime": 1714000100,
                    "author": {"uniqueId": "repeatuser"},
                    "video": {"playAddr": "https://v19.tiktok.com/play.mp4"},
                }
            ]
        }
    })
    monkeypatch.setattr("app.routes.fetch_page_info", mock_fetch)

    test_file = tmp_path / "last_stories.json"
    monkeypatch.setattr("app.utils.story_store._STORE_FILE", test_file)
    monkeypatch.setattr("app.utils.story_store._DATA_DIR", tmp_path)

    # First call -> new_story: True
    res1 = client.get(
        "/stories/latest?username=repeatuser",
        headers={"X-API-Key": VALID_KEY},
    )
    assert res1.status_code == 200
    assert res1.json()["new_story"] is True

    # Second call -> new_story: False
    res2 = client.get(
        "/stories/latest?username=repeatuser",
        headers={"X-API-Key": VALID_KEY},
    )
    assert res2.status_code == 200
    assert res2.json()["new_story"] is False


def test_extract_url_helper():
    assert _extract_url("https://example.com/a.mp4") == "https://example.com/a.mp4"
    assert _extract_url({"urlList": ["https://example.com/b.mp4"]}) == "https://example.com/b.mp4"
    assert _extract_url(None) is None
    assert _extract_url({}) is None


def test_parse_story_response_defensive():
    raw_payload = {
        "itemList": [
            {
                "id": 123456789,
                "createTime": 1700000000,
                "author": {
                    "uniqueId": "user123",
                    "avatarLarger": {"urlList": ["https://cdn.com/avatar.webp"]},
                },
                "video": {
                    "playAddr": {"urlList": ["https://cdn.com/play.mp4"]},
                    "downloadAddr": {"url_list": ["https://cdn.com/dl.mp4"]},
                    "cover": {"urlList": ["https://cdn.com/cover.webp"]},
                },
            }
        ]
    }
    parsed = parse_story_response(raw_payload)
    assert parsed["success"] is True
    assert parsed["username"] == "user123"
    assert parsed["avatar"] == "https://cdn.com/avatar.webp"
    assert parsed["stories"][0]["id"] == "123456789"
    assert parsed["stories"][0]["video_url"] == "https://cdn.com/play.mp4"
    assert parsed["stories"][0]["download_url"] == "https://cdn.com/dl.mp4"
    assert parsed["stories"][0]["cover"] == "https://cdn.com/cover.webp"
