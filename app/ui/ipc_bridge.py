"""Turns IPC messages (browser extension, second instance) into GUI actions.

`handle` runs on the IPC server's thread and must return immediately, so it
only validates and then hands the work to the GUI thread through a queued
signal. Answering "accepted" rather than "finished" also keeps the browser's
native-messaging callback fast.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from .. import __version__
from ..ipc.protocol import (
    TYPE_DOWNLOAD,
    TYPE_LIST,
    TYPE_MEDIA,
    TYPE_PAUSE,
    TYPE_PING,
    TYPE_RESUME,
    TYPE_SHOW,
)
from ..util.log import get_logger

log = get_logger(__name__)


class IpcBridge(QObject):
    downloadRequested = Signal(dict)
    showRequested = Signal(dict)
    #: control messages from `idmclone-cli --remote-*`, answered synchronously
    controlRequested = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        #: set by the window: answers "what is downloading right now?"
        self.snapshot = None
        #: set by the window: (action, db_id or None) -> bool
        self.control = None

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")

        if kind == TYPE_PING:
            return {"ok": True, "app": "IDMClone", "version": __version__}

        if kind in (TYPE_DOWNLOAD, TYPE_MEDIA):
            url = (message.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return {"ok": False, "error": "only http(s) URLs are accepted"}
            log.info("captured %s from the browser: %s", kind, url)
            self.downloadRequested.emit(message)
            return {"ok": True, "accepted": url}

        if kind == TYPE_LIST:
            # Answering needs controller state, so the window installs a
            # callback; without one the CLI gets an honest "not available".
            if self.snapshot is None:
                return {"ok": False, "error": "the application is still starting"}
            return {"ok": True, "downloads": self.snapshot()}

        if kind in (TYPE_PAUSE, TYPE_RESUME):
            if self.control is None:
                return {"ok": False, "error": "the application is still starting"}
            target = message.get("id")
            done = self.control(kind, int(target) if target is not None else None)
            return {"ok": bool(done)}

        if kind == TYPE_SHOW:
            self.showRequested.emit(message)
            return {"ok": True}

        return {"ok": False, "error": f"unknown message type: {kind!r}"}
