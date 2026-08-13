"""Turns IPC messages (browser extension, second instance) into GUI actions.

`handle` runs on the IPC server's thread and must return immediately, so it
only validates and then hands the work to the GUI thread through a queued
signal. Answering "accepted" rather than "finished" also keeps the browser's
native-messaging callback fast.

The `--remote-list` and `--remote-pause` messages are the exception: the CLI
waits for a real answer. Those still run *on the GUI thread* - they read the
download table and write to SQLite, neither of which may be touched from the
socket thread - so `_ask_the_gui` posts the call and waits for the result,
with a timeout so a wedged window cannot hang the caller forever.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

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


#: how long the CLI waits for the window to answer a --remote-* message
GUI_TIMEOUT = 5.0


class _Call:
    """One function to run on the GUI thread, and somewhere to put the answer."""

    __slots__ = ("fn", "args", "result", "error", "done")

    def __init__(self, fn: Callable, args: tuple) -> None:
        self.fn, self.args = fn, args
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = threading.Event()

    def run(self) -> None:
        try:
            self.result = self.fn(*self.args)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self.error = exc
        finally:
            self.done.set()


class IpcBridge(QObject):
    downloadRequested = Signal(dict)
    showRequested = Signal(dict)
    #: control messages from `boltdown-cli --remote-*`, answered synchronously
    controlRequested = Signal(dict)
    #: internal: carries a `_Call` from the IPC thread to the GUI thread
    _invoke = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        #: set by the window: answers "what is downloading right now?"
        self.snapshot = None
        #: set by the window: (action, db_id or None) -> bool
        self.control = None
        # A queued (not blocking-queued) connection: waiting on the event with
        # a timeout cannot deadlock, whereas BlockingQueuedConnection would
        # wait forever if the GUI thread's event loop had already stopped.
        self._invoke.connect(self._run_call)

    @Slot(object)
    def _run_call(self, call: "_Call") -> None:
        call.run()

    def _ask_the_gui(self, fn: Callable, *args: Any) -> Any:
        """Run `fn` on the thread that owns this bridge and return its result."""
        if QThread.currentThread() is self.thread():
            return fn(*args)
        call = _Call(fn, args)
        self._invoke.emit(call)
        if not call.done.wait(GUI_TIMEOUT):
            raise TimeoutError("the window did not answer in time")
        if call.error is not None:
            raise call.error
        return call.result

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = message.get("type")

        if kind == TYPE_PING:
            return {"ok": True, "app": "Boltdown", "version": __version__}

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
            try:
                return {"ok": True, "downloads": self._ask_the_gui(self.snapshot)}
            except Exception as exc:  # noqa: BLE001 - the CLI wants a message
                log.warning("could not read the download list: %s", exc)
                return {"ok": False, "error": str(exc)}

        if kind in (TYPE_PAUSE, TYPE_RESUME):
            if self.control is None:
                return {"ok": False, "error": "the application is still starting"}
            target = message.get("id")
            try:
                done = self._ask_the_gui(
                    self.control, kind, int(target) if target is not None else None
                )
            except Exception as exc:  # noqa: BLE001 - the CLI wants a message
                log.warning("could not %s: %s", kind, exc)
                return {"ok": False, "error": str(exc)}
            return {"ok": bool(done)}

        if kind == TYPE_SHOW:
            self.showRequested.emit(message)
            return {"ok": True}

        return {"ok": False, "error": f"unknown message type: {kind!r}"}
