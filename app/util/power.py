"""Turning the machine off when the downloads are done.

IDM's "shut down when finished" is the reason people leave it running
overnight, so it has to be both reliable and *cancellable*: the shutdown is
scheduled with a delay and `cancel()` calls it back off, which is what the
countdown dialog does when the user clicks "Stop".

Every command is built by `command_for` and executed by an injectable runner,
so the tests can assert on the exact argv without powering anything down.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Sequence

from ..core.schedule import PostAction
from .log import get_logger

log = get_logger(__name__)

DEFAULT_DELAY = 60  # seconds of grace before the machine goes down

Runner = Callable[[Sequence[str]], object]


def _windows_command(action: PostAction, delay: int) -> list[str] | None:
    if action is PostAction.SHUTDOWN:
        return ["shutdown", "/s", "/f", "/t", str(max(0, delay))]
    if action is PostAction.HIBERNATE:
        # /h takes no timeout; the caller waits out the delay itself.
        return ["shutdown", "/h"]
    if action is PostAction.SLEEP:
        # Without hibernation disabled this suspends to RAM; it is the only
        # sleep entry point reachable without a Win32 API call.
        return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
    return None


def _posix_command(action: PostAction, delay: int) -> list[str] | None:
    minutes = max(1, round(delay / 60))
    if action is PostAction.SHUTDOWN:
        return ["shutdown", "-h", f"+{minutes}"]
    if action is PostAction.HIBERNATE:
        return ["systemctl", "hibernate"]
    if action is PostAction.SLEEP:
        return ["systemctl", "suspend"]
    return None


def command_for(action: PostAction, delay: int = DEFAULT_DELAY) -> list[str] | None:
    """The argv for `action`, or None when the app itself has to act."""
    if action in (PostAction.NONE, PostAction.EXIT):
        return None
    if os.name == "nt":
        return _windows_command(action, delay)
    return _posix_command(action, delay)


def cancel_command() -> list[str] | None:
    """Undo a pending shutdown, if the platform can."""
    return ["shutdown", "/a"] if os.name == "nt" else ["shutdown", "-c"]


def _default_runner(argv: Sequence[str]) -> object:
    return subprocess.Popen(
        list(argv),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def apply(
    action: PostAction,
    *,
    delay: int = DEFAULT_DELAY,
    runner: Runner | None = None,
    on_exit: Callable[[], None] | None = None,
) -> bool:
    """Carry out `action`. Returns False when there was nothing to do."""
    if action is PostAction.NONE:
        return False
    if action is PostAction.EXIT:
        if on_exit is not None:
            on_exit()
        return True
    argv = command_for(action, delay)
    if argv is None:  # pragma: no cover - unknown platform
        log.warning("no %s command for this platform", action.value)
        return False
    try:
        (runner or _default_runner)(argv)
    except OSError as exc:
        log.error("could not run %s: %s", " ".join(argv), exc)
        return False
    log.info("post-download action: %s", " ".join(argv))
    return True


def cancel(runner: Runner | None = None) -> bool:
    argv = cancel_command()
    if argv is None:  # pragma: no cover - unknown platform
        return False
    try:
        (runner or _default_runner)(argv)
    except OSError as exc:
        log.error("could not cancel the shutdown: %s", exc)
        return False
    return True
