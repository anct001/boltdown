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
from ..util.links import is_download_link
from . import endpoint
from .protocol import (
    MAX_BATCH,
    TYPE_BATCH,
    TYPE_DOWNLOAD,
    TYPE_MEDIA,
    TYPE_PING,
    ProtocolError,
    read_native,
    write_native,
)

log = get_logger(__name__)

LAUNCH_TIMEOUT = 20.0
LAUNCH_POLL = 0.25


#: what a browser may ask for. The loopback endpoint also answers `list`,
#: `pause`, `resume` and `show` for `boltdown-cli --remote-*`; none of those
#: is the extension's business, and relaying them would let any script that
#: gets hold of the extension's messaging read the download list or stop it.
BROWSER_TYPES = frozenset({TYPE_PING, TYPE_DOWNLOAD, TYPE_MEDIA, TYPE_BATCH})
#: fields the application reads from a browser message; anything else - a
#: `token` above all - is dropped rather than forwarded
BROWSER_FIELDS = frozenset(
    {"type", "url", "filename", "referer", "cookie", "user_agent", "size",
     "mime", "streaming", "page", "items"}
)
#: the envelope field a long-lived port uses to pair a reply with its request
SEQ = "seq"


def _is_http(value: Any) -> bool:
    """Any address the app downloads: web, FTP or a magnet link."""
    return is_download_link(value)

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


def screen(message: Any) -> tuple[dict[str, Any] | None, str | None]:
    """The part of a browser message worth relaying, or why there is none."""
    if not isinstance(message, dict):
        return None, "a message must be a JSON object"
    kind = message.get("type")
    if kind not in BROWSER_TYPES:
        return None, f"message type not allowed from the browser: {kind!r}"
    clean = {key: value for key, value in message.items() if key in BROWSER_FIELDS}
    if kind == TYPE_BATCH:
        items = clean.get("items")
        if not isinstance(items, list) or not items:
            return None, "a batch needs a list of items"
        if len(items) > MAX_BATCH:
            return None, f"a batch carries at most {MAX_BATCH} links"
        kept = []
        for item in items:
            if not isinstance(item, dict) or not _is_http(item.get("url")):
                return None, "only http(s) URLs are accepted"
            cookie = item.get("cookie")
            kept.append({"url": item["url"].strip(),
                         **({"cookie": cookie} if isinstance(cookie, str) else {})})
        clean["items"] = kept
        clean.pop("url", None)
    elif kind != TYPE_PING:
        if not _is_http(clean.get("url")):
            return None, "only http(s) URLs are accepted"
        clean.pop("items", None)
    else:
        clean.pop("items", None)
    return clean, None


def deliver(message: dict[str, Any]) -> dict[str, Any]:
    """Relay one message, starting the app on the first miss."""
    message, problem = screen(message)
    if message is None:
        log.warning("refused a browser message: %s", problem)
        return {"ok": False, "error": problem}

    reply = endpoint.send(message)
    if reply is not None:
        return reply

    # The popup pings every time it opens, just to show whether the app is
    # up. Starting the whole application for that - and holding the popup
    # for twenty seconds while it boots - answers a question nobody asked.
    if message["type"] == TYPE_PING:
        return {"ok": False, "error": "Boltdown is not running", "running": False}

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
        # A long-lived port (connectNative) numbers its requests; echo the
        # number so the extension can pair replies even after a timeout.
        seq = message.pop(SEQ, None) if isinstance(message, dict) else None
        try:
            response = deliver(message)
        except Exception as exc:  # noqa: BLE001 - always answer the browser
            log.exception("failed to deliver message")
            response = {"ok": False, "error": str(exc)}
        if seq is not None:
            response = {**response, SEQ: seq}
        try:
            write_native(stdout, response)
        except (OSError, ProtocolError) as exc:
            log.error("could not answer the browser: %s", exc)
            return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
