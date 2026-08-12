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
from ..ipc.protocol import TYPE_DOWNLOAD, TYPE_MEDIA, TYPE_PING, TYPE_SHOW
from ..util.log import get_logger

log = get_logger(__name__)


class IpcBridge(QObject):
    downloadRequested = Signal(dict)
    showRequested = Signal(dict)

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

        if kind == TYPE_SHOW:
            self.showRequested.emit(message)
            return {"ok": True}

        return {"ok": False, "error": f"unknown message type: {kind!r}"}
