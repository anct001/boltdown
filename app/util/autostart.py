"""Starting with Windows.

The Run key is the right mechanism here: it needs no elevation, the user can
see and remove it, and it runs after the desktop is up - a download manager
does not need a service. The stored command always starts the app minimised to
the tray, because nobody wants a window in their face at every login.

Every function takes the registry path so tests can write somewhere harmless
instead of the real Run key.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Boltdown"
TRAY_FLAG = "--tray"


def _winreg():
    if sys.platform != "win32":  # pragma: no cover - Windows only feature
        raise OSError("autostart registration requires Windows")
    import winreg

    return winreg


def launcher() -> Path:
    """The executable Windows should start at login.

    A frozen build is a single exe. From a checkout, `pip install -e .` leaves
    `boltdown-gui.exe` in the venv's Scripts directory - a windowless launcher
    that already knows where the package lives, which a bare `-m app` would not
    (the Run key starts processes in system32, so the package would not import).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    scripts = Path(sys.executable).parent
    for name in ("boltdown-gui.exe", "boltdown-gui"):
        candidate = scripts / name
        if candidate.exists():
            return candidate
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return windowless if windowless.exists() else Path(sys.executable)


def launch_command(executable: str | os.PathLike[str] | None = None) -> str:
    """The command line Windows should run at login."""
    target = Path(executable) if executable is not None else launcher()
    return f'"{target}" {TRAY_FLAG}'


def current(key_path: str = RUN_KEY) -> str | None:
    """The command currently registered, or None."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None


def is_enabled(key_path: str = RUN_KEY) -> bool:
    return current(key_path) is not None


def enable(
    executable: str | os.PathLike[str] | None = None, key_path: str = RUN_KEY
) -> str:
    winreg = _winreg()
    command = launch_command(executable)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    log.info("autostart enabled: %s", command)
    return command


def disable(key_path: str = RUN_KEY) -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:
        return False
    log.info("autostart disabled")
    return True


def apply(
    enabled: bool,
    executable: str | os.PathLike[str] | None = None,
    key_path: str = RUN_KEY,
) -> bool:
    """Make the registry match `enabled`; never raises on a missing key."""
    try:
        if enabled:
            enable(executable, key_path)
        else:
            disable(key_path)
        return True
    except OSError as exc:
        log.warning("could not change the autostart entry: %s", exc)
        return False
