"""Typed access to the `settings` table, with defaults in one place."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..util.paths import default_download_dir
from .db import Database

DEFAULTS: dict[str, Any] = {
    "download_dir": None,          # None -> the user's Downloads folder
    "connections": 8,
    "max_concurrent": 3,
    "speed_limit": None,           # bytes/second, None = unlimited
    "use_categories": True,
    "proxy": None,
    "user_agent": None,
    "language": "vi",
    "minimize_to_tray": True,
    "confirm_exit": True,
    "ask_before_download": True,   # show the Add URL dialog for dropped links
    "verify_tls": True,
    "window_geometry": None,
    "video_quality": None,         # max height for video; None = best available
    "ffmpeg_path": None,           # None -> look next to the app, then on PATH
    "start_with_windows": False,   # mirrored into the HKCU Run key
    "extension_id": None,          # last id registered for native messaging
}


class Settings:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        value = self._db.get_setting(key, DEFAULTS.get(key, default))
        self._cache[key] = value
        return value

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        self._db.set_setting(key, value)

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    # Convenience accessors used all over the UI ---------------------------

    @property
    def download_dir(self) -> Path:
        value = self.get("download_dir")
        return Path(value) if value else default_download_dir()

    @property
    def connections(self) -> int:
        return max(1, min(32, int(self.get("connections"))))

    @property
    def max_concurrent(self) -> int:
        return max(1, int(self.get("max_concurrent")))

    @property
    def speed_limit(self) -> int | None:
        value = self.get("speed_limit")
        return int(value) if value else None

    @property
    def language(self) -> str:
        return str(self.get("language") or "vi")

    @property
    def video_quality(self) -> int | None:
        value = self.get("video_quality")
        return int(value) if value else None

    @property
    def ffmpeg_path(self) -> str | None:
        value = self.get("ffmpeg_path")
        return str(value) if value else None
