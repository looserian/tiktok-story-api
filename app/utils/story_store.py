"""
story_store.py - Persistent storage for the last-seen story ID per username.

Storage format (data/last_stories.json):
    {
        "username1": "7666990859480534292",
        "username2": "7667154335628889365"
    }

Public API:
    read_last_stories()  -> dict[str, str]
    write_last_stories(data) -> None
    get_last_story_id(username) -> str | None
    set_last_story_id(username, story_id) -> None
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to the JSON file relative to the project root (where the process runs from).
# Using an absolute path anchored to this file avoids cwd dependency.
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_STORE_FILE = _DATA_DIR / "last_stories.json"


# ── Low-level helpers ─────────────────────────────────────────────────────────

def read_last_stories() -> dict[str, str]:
    """
    Read and return the full contents of last_stories.json.

    - Creates the file (and parent directory) if it does not exist.
    - Recreates the file if the JSON is malformed / corrupted.

    Returns:
        A dict mapping username -> last_seen_story_id.
    """
    # Ensure the data directory exists.
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not _STORE_FILE.exists():
        logger.info("story_store: %s not found — creating empty store", _STORE_FILE)
        write_last_stories({})
        return {}

    try:
        text = _STORE_FILE.read_text(encoding="utf-8")
        data = json.loads(text)

        # Guard: the file must contain a plain dict, not a list or scalar.
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object, got {type(data).__name__}")

        return data  # type: ignore[return-value]

    except (json.JSONDecodeError, ValueError) as exc:
        # File is corrupt — log a warning and reset to an empty store.
        logger.warning(
            "story_store: %s is corrupt (%s) — recreating empty store", _STORE_FILE, exc
        )
        write_last_stories({})
        return {}


def write_last_stories(data: dict[str, str]) -> None:
    """
    Write *data* to last_stories.json, replacing the entire file contents.

    Args:
        data: A dict mapping username -> story_id.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _STORE_FILE.write_text(
        json.dumps(data, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.debug("story_store: wrote %d entries to %s", len(data), _STORE_FILE)


# ── High-level helpers ────────────────────────────────────────────────────────

def get_last_story_id(username: str) -> str | None:
    """
    Return the last-seen story ID for *username*, or ``None`` if unknown.

    Args:
        username: TikTok username (without the leading ``@``).

    Returns:
        The stored story ID string, or ``None`` if the username has never
        been seen before.
    """
    store = read_last_stories()
    return store.get(username)


def set_last_story_id(username: str, story_id: str) -> None:
    """
    Persist *story_id* as the latest seen story for *username*.

    Reads the current store, updates the entry for *username*, then writes
    the whole store back to disk.

    Args:
        username: TikTok username (without the leading ``@``).
        story_id: The story ID string to save.
    """
    store = read_last_stories()
    store[username] = story_id
    write_last_stories(store)
    logger.info(
        "story_store: updated  username=%r  story_id=%r", username, story_id
    )
