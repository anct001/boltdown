"""Icons drawn at runtime with QPainter.

Shipping no binary assets keeps the repo diffable and the icons scale to any
DPI. Colours come from the active palette so the toolbar looks right in both
light and dark Windows themes.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

SIZE = 32


def _canvas(size: int = SIZE) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pixmap, painter


def _finish(pixmap: QPixmap, painter: QPainter) -> QIcon:
    painter.end()
    return QIcon(pixmap)


def add_icon(color: QColor = QColor("#2e7d32")) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawRoundedRect(QRectF(13, 5, 6, 22), 3, 3)
    p.drawRoundedRect(QRectF(5, 13, 22, 6), 3, 3)
    return _finish(pixmap, p)


def download_icon(color: QColor = QColor("#1565c0")) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawRoundedRect(QRectF(13, 4, 6, 12), 2, 2)
    path = QPainterPath()
    path.moveTo(QPointF(6, 14))
    path.lineTo(QPointF(26, 14))
    path.lineTo(QPointF(16, 25))
    path.closeSubpath()
    p.drawPath(path)
    p.drawRoundedRect(QRectF(5, 26, 22, 4), 2, 2)
    return _finish(pixmap, p)


def pause_icon(color: QColor = QColor("#ef6c00")) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawRoundedRect(QRectF(8, 6, 6, 20), 2, 2)
    p.drawRoundedRect(QRectF(18, 6, 6, 20), 2, 2)
    return _finish(pixmap, p)


def stop_icon(color: QColor = QColor("#c62828")) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawRoundedRect(QRectF(7, 7, 18, 18), 3, 3)
    return _finish(pixmap, p)


def delete_icon(color: QColor = QColor("#b71c1c")) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.drawRoundedRect(QRectF(11, 4, 10, 4), 2, 2)
    p.drawRoundedRect(QRectF(6, 9, 20, 4), 2, 2)
    p.drawRoundedRect(QRectF(9, 13, 14, 15), 3, 3)
    p.setBrush(QBrush(QColor(255, 255, 255, 200)))
    for x in (12, 15.5, 19):
        p.drawRect(QRectF(x, 16, 2, 9))
    return _finish(pixmap, p)


def settings_icon(color: QColor = QColor("#455a64")) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color))
    p.translate(16, 16)
    for i in range(8):
        p.save()
        p.rotate(i * 45)
        p.drawRoundedRect(QRectF(-2.5, -14, 5, 7), 2, 2)
        p.restore()
    p.drawEllipse(QPointF(0, 0), 9, 9)
    p.setBrush(QBrush(QColor("#eceff1")))
    p.drawEllipse(QPointF(0, 0), 4, 4)
    return _finish(pixmap, p)


def _accent() -> QColor:
    """Toolbar glyphs follow the theme; the coloured icons above do not."""
    from . import theme

    return theme.current().color("accent")


def batch_icon(color: QColor | None = None) -> QIcon:
    """Three stacked rows plus a small plus - "add many"."""
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _accent()))
    for y in (6, 13, 20):
        p.drawRoundedRect(QRectF(4, y, 16, 4), 2, 2)
    p.drawRoundedRect(QRectF(23, 13, 4, 14), 2, 2)
    p.drawRoundedRect(QRectF(18, 18, 14, 4), 2, 2)
    return _finish(pixmap, p)


def clock_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    pen = QPen(color or _accent())
    pen.setWidthF(2.4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(16, 16), 11, 11)
    p.drawLine(QPointF(16, 16), QPointF(16, 9))
    p.drawLine(QPointF(16, 16), QPointF(21, 18))
    return _finish(pixmap, p)


def history_icon(color: QColor | None = None) -> QIcon:
    """A clock whose dial is open on the left, with an arrow back into it."""
    pixmap, p = _canvas()
    pen = QPen(color or _accent())
    pen.setWidthF(2.4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(5, 5, 22, 22), 60 * 16, 300 * 16)
    p.drawLine(QPointF(16, 16), QPointF(16, 10))
    p.drawLine(QPointF(16, 16), QPointF(20, 19))
    path = QPainterPath()
    path.moveTo(QPointF(6, 2))
    path.lineTo(QPointF(13, 8))
    path.lineTo(QPointF(5, 10))
    path.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _accent()))
    p.drawPath(path)
    return _finish(pixmap, p)


def globe_icon(color: QColor | None = None) -> QIcon:
    """Site grabber: a globe with one meridian and one parallel."""
    pixmap, p = _canvas()
    pen = QPen(color or _accent())
    pen.setWidthF(2.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(16, 16), 11, 11)
    p.drawLine(QPointF(5, 16), QPointF(27, 16))
    p.drawEllipse(QPointF(16, 16), 5, 11)
    return _finish(pixmap, p)


def app_icon() -> QIcon:
    pixmap, p = _canvas(64)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#1565c0")))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 12, 12)
    p.setBrush(QBrush(QColor("#ffffff")))
    p.drawRoundedRect(QRectF(27, 12, 10, 22), 3, 3)
    path = QPainterPath()
    path.moveTo(QPointF(16, 30))
    path.lineTo(QPointF(48, 30))
    path.lineTo(QPointF(32, 48))
    path.closeSubpath()
    p.drawPath(path)
    return _finish(pixmap, p)
