"""The floating drop target: a small always-on-top window that takes links.

IDM's drop box is a shortcut for one gesture - drag a link out of a browser
onto something that is always visible, without alt-tabbing to the manager.
That means: frameless, on top, movable by dragging anywhere on it, and it
remembers where the user parked it.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QWidget

from ..storage.settings import Settings
from . import theme
from .i18n import tr

SIZE = (132, 112)
MARGIN = 40


def urls_in(mime) -> list[str]:
    """The http(s) links in a drop, whether it carried URLs or plain text."""
    urls = [u.toString() for u in mime.urls()] if mime.hasUrls() else []
    if not urls and mime.hasText():
        urls = [line.strip() for line in mime.text().splitlines() if line.strip()]
    return [u for u in urls if u.startswith(("http://", "https://"))]


class DropBox(QWidget):
    """Accepts dropped links and hands them to the window."""

    urlsDropped = Signal(list)
    closed = Signal()

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.settings = settings
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAcceptDrops(True)
        self.setFixedSize(*SIZE)
        self.setToolTip(tr("Drop links here"))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self._drag_offset: QPoint | None = None
        self._hover = False
        self.restore_position()

    # ------------------------------------------------------------- position

    def restore_position(self) -> None:
        saved = self.settings.get("dropbox_position")
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            self.move(int(saved[0]), int(saved[1]))
            return
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - SIZE[0] - MARGIN, area.top() + MARGIN)

    def save_position(self) -> None:
        self.settings.set("dropbox_position", [self.x(), self.y()])

    # ---------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:
        palette = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)

        painter.setPen(QPen(palette.color("accent" if self._hover else "border"), 2))
        painter.setBrush(palette.alpha("surface", 235))
        painter.drawRoundedRect(rect, 16, 16)

        arrow = QPainterPath()
        cx, top = rect.center().x(), rect.top() + 24
        arrow.moveTo(cx - 13, top)
        arrow.lineTo(cx + 13, top)
        arrow.lineTo(cx + 13, top + 20)
        arrow.lineTo(cx + 24, top + 20)
        arrow.lineTo(cx, top + 40)
        arrow.lineTo(cx - 24, top + 20)
        arrow.lineTo(cx - 13, top + 20)
        arrow.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color("accent"))
        painter.drawPath(arrow)

        painter.setPen(QPen(palette.color("muted")))
        painter.drawText(
            QRectF(rect.left(), rect.bottom() - 30, rect.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            tr("Drop links here"),
        )

    # ----------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event) -> None:
        data = event.mimeData()
        if data.hasUrls() or data.hasText():
            self._hover = True
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def dropEvent(self, event) -> None:
        urls = urls_in(event.mimeData())
        self._hover = False
        self.update()
        if urls:
            self.urlsDropped.emit(urls)
        event.acceptProposedAction()

    # ------------------------------------------------------------- movement

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self.save_position()

    def mouseDoubleClickEvent(self, event) -> None:
        window = self.parent()
        if window is not None:  # pragma: no cover - needs a real window
            window.showNormal()
            window.raise_()
            window.activateWindow()

    def _menu(self, position) -> None:
        menu = QMenu(self)
        hide = QAction(tr("Hide drop box"), menu)
        hide.triggered.connect(self.hide_box)
        menu.addAction(hide)
        menu.exec(self.mapToGlobal(position))

    def hide_box(self) -> None:
        self.save_position()
        self.settings.set("dropbox_visible", False)
        self.hide()
        self.closed.emit()

    def show_box(self) -> None:
        self.settings.set("dropbox_visible", True)
        self.restore_position()
        self.show()
        self.raise_()
