"""Catch download links the moment they are copied.

The feature people actually install IDM for: copy a link, the manager offers
to take it. Two things keep it from being annoying, and both live in
`link_in`, which is pure and therefore testable:

* only text that is *just* a URL counts - copying a paragraph that happens to
  contain a link is not a download request;
* the extension has to be on the watch list, so copying an article URL does
  nothing while copying a .zip does.

`ignore` exists because the app copies URLs itself (the Copy URL action);
without it, using that action would immediately offer to download the thing
you just copied.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication

from ..storage.settings import Settings
from ..util.log import get_logger

log = get_logger(__name__)


def parse_extensions(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    parts = [p.strip().lstrip(".").lower() for p in text.replace(";", ",").split(",")]
    return tuple(p for p in parts if p)


def extension_of(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rstrip("/").rpartition("/")[2]
    return name.rpartition(".")[2].lower() if "." in name else ""


def link_in(text: str | None, extensions: tuple[str, ...] = ()) -> str | None:
    """The URL worth downloading in `text`, or None."""
    if not text:
        return None
    candidate = text.strip()
    if len(candidate.split()) != 1 or "\n" in candidate:
        return None
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    if extensions and extension_of(candidate) not in extensions:
        return None
    return candidate


class ClipboardWatcher(QObject):
    """Watches the clipboard while enabled; emits one signal per new link."""

    linkCaptured = Signal(str)

    def __init__(self, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._last: str | None = None
        self._ignored: set[str] = set()
        self._connected = False

    # ---------------------------------------------------------------- control

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("clipboard_monitor"))

    def extensions(self) -> tuple[str, ...]:
        return parse_extensions(self.settings.get("clipboard_extensions"))

    def start(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None or self._connected:  # pragma: no cover - headless
            return
        clipboard.dataChanged.connect(self._on_changed)
        self._connected = True
        # Whatever is on the clipboard right now was not copied *at us*.
        self._last = clipboard.text()

    def stop(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and self._connected:
            clipboard.dataChanged.disconnect(self._on_changed)
        self._connected = False

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set("clipboard_monitor", bool(enabled))
        if enabled:
            self.start()

    def ignore(self, text: str) -> None:
        """Do not react to this text - the application put it there."""
        if text:
            self._ignored.add(text.strip())

    # ----------------------------------------------------------------- events

    def _on_changed(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:  # pragma: no cover - headless
            return
        self.handle(clipboard.text())

    def handle(self, text: str | None) -> str | None:
        """Process one clipboard value; returns the link that was emitted."""
        if not self.enabled:
            return None
        candidate = (text or "").strip()
        if candidate and candidate in self._ignored:
            self._ignored.discard(candidate)
            self._last = candidate
            return None
        if candidate == self._last:
            return None
        self._last = candidate
        link = link_in(candidate, self.extensions())
        if link is None:
            return None
        log.info("clipboard: captured %s", link)
        self.linkCaptured.emit(link)
        return link
