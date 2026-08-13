"""Chrome/Edge native messaging host.

Chrome starts this process, talks 4-byte-framed JSON over stdio, and kills it
when the extension's port closes. All it does is relay to the running
application - and start the application if it is not up yet.

Nothing may ever be printed to stdout: that channel belongs to Chrome.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..util.log import get_logger, setup_logging
from . import endpoint
from .protocol import ProtocolError, read_native, write_native

log = get_logger(__name__)

LAUNCH_TIMEOUT = 20.0
LAUNCH_POLL = 0.25


GUI_EXE_NAME = "Boltdown.exe" if sys.platform == "win32" else "Boltdown"


def project_root() -> Path:
    """The directory that contains the `app` package."""
    return Path(__file__).resolve().parent.parent.parent


def app_command() -> tuple[list[str], str]:
    """How to start the application, and from where.

    In a packaged build `sys.executable` is *this* host, not the GUI - starting
    it again would only spawn another host that also finds nothing running.
    The windowed executable sits next to it.
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        gui = here / GUI_EXE_NAME
        return [str(gui if gui.exists() else sys.executable)], str(here)
    return [sys.executable, "-m", "app"], str(project_root())


def launch_app() -> bool:
    """Start the GUI detached from this short-lived host process."""
    command, cwd = app_command()
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen(
            command, cwd=cwd, creationflags=creation_flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
        log.info("launched the application: %s", command)
        return True
    except OSError as exc:
        log.error("could not launch the application: %s", exc)
        return False


def deliver(message: dict[str, Any]) -> dict[str, Any]:
    """Relay one message, starting the app on the first miss."""
    reply = endpoint.send(message)
    if reply is not None:
        return reply

    if not launch_app():
        return {"ok": False, "error": "Boltdown is not installed correctly"}

    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(LAUNCH_POLL)
        reply = endpoint.send(message)
        if reply is not None:
            return reply
    return {"ok": False, "error": "Boltdown did not start in time"}


def main(argv: list[str] | None = None) -> int:
    setup_logging(level=logging.WARNING, console=False)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    log.info("native host started (argv=%s)", argv if argv is not None else sys.argv[1:])

    while True:
        try:
            message = read_native(stdin)
        except ProtocolError as exc:
            log.error("protocol error: %s", exc)
            return 1
        except OSError:
            return 0
        if message is None:
            log.info("browser closed the pipe")
            return 0
        try:
            response = deliver(message)
        except Exception as exc:  # noqa: BLE001 - always answer the browser
            log.exception("failed to deliver message")
            response = {"ok": False, "error": str(exc)}
        try:
            write_native(stdout, response)
        except (OSError, ProtocolError) as exc:
            log.error("could not answer the browser: %s", exc)
            return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
