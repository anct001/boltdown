"""Application directories."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "IDMClone"


def data_dir() -> Path:
    """Per-user application data directory (created on demand)."""
    base = os.environ.get("IDMCLONE_HOME")
    if base:
        path = Path(base)
    elif os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = Path(root) / APP_NAME
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = Path(root) / APP_NAME.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "idmclone.db"


def log_path() -> Path:
    return data_dir() / "idmclone.log"


def default_download_dir() -> Path:
    if os.name == "nt":
        candidate = Path(os.path.expanduser("~")) / "Downloads"
    else:
        candidate = Path(os.path.expanduser("~")) / "Downloads"
    return candidate
