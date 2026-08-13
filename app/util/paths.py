"""Application directories."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Boltdown"
#: what the application was called before the rename; an existing profile is
#: adopted rather than abandoned
LEGACY_NAME = "IDMClone"
LEGACY_ENV = "IDMCLONE_HOME"


def portable_marker() -> Path:
    """`boltdown.portable` next to the executable turns portable mode on."""
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else (
        Path(__file__).resolve().parent.parent.parent
    )
    return root / "boltdown.portable"


def is_portable() -> bool:
    """True when settings and the database live beside the program.

    Made a file rather than a setting on purpose: the setting would have to be
    read from the very directory the flag decides.
    """
    return portable_marker().exists()


def data_dir() -> Path:
    """Per-user application data directory (created on demand)."""
    base = os.environ.get("BOLTDOWN_HOME") or os.environ.get(LEGACY_ENV)
    if base:
        path = Path(base)
    elif is_portable():
        path = portable_marker().parent / "data"
    elif os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = Path(root) / APP_NAME
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = Path(root) / APP_NAME.lower()
    legacy = _legacy_dir(path)
    if legacy is not None:
        # Settings, the database and the queue all live in there; renaming the
        # program is no reason to make the user set everything up again.
        try:
            legacy.rename(path)
        except OSError:
            return legacy
    path.mkdir(parents=True, exist_ok=True)
    return path


def _legacy_dir(new_path: Path) -> Path | None:
    """The pre-rename directory, when it exists and the new one does not."""
    if new_path.exists() or new_path.name not in (APP_NAME, APP_NAME.lower()):
        return None
    candidate = new_path.parent / (
        LEGACY_NAME if new_path.name == APP_NAME else LEGACY_NAME.lower()
    )
    return candidate if candidate.is_dir() else None


def db_path() -> Path:
    return data_dir() / "boltdown.db"


def log_path() -> Path:
    return data_dir() / "boltdown.log"


def default_download_dir() -> Path:
    if os.name == "nt":
        candidate = Path(os.path.expanduser("~")) / "Downloads"
    else:
        candidate = Path(os.path.expanduser("~")) / "Downloads"
    return candidate
