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


def _token(name: str) -> QColor:
    """Icons are painted in the active theme's colours, not fixed ones.

    So "add" is the theme's success green, "stop" its danger red, and a
    Cyberpunk window gets magenta arrows instead of the stock blue.
    """
    from . import theme

    return theme.current().color(name)


def _accent() -> QColor:
    return _token("accent")


def _muted() -> QColor:
    return _token("muted")


def _stroke(p: QPainter, color: QColor, width: float = 2.2) -> None:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)


def add_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _token("success")))
    p.drawRoundedRect(QRectF(13, 5, 6, 22), 3, 3)
    p.drawRoundedRect(QRectF(5, 13, 22, 6), 3, 3)
    return _finish(pixmap, p)


def download_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _token("accent")))
    p.drawRoundedRect(QRectF(13, 4, 6, 12), 2, 2)
    path = QPainterPath()
    path.moveTo(QPointF(6, 14))
    path.lineTo(QPointF(26, 14))
    path.lineTo(QPointF(16, 25))
    path.closeSubpath()
    p.drawPath(path)
    p.drawRoundedRect(QRectF(5, 26, 22, 4), 2, 2)
    return _finish(pixmap, p)


def pause_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _token("warning")))
    p.drawRoundedRect(QRectF(8, 6, 6, 20), 2, 2)
    p.drawRoundedRect(QRectF(18, 6, 6, 20), 2, 2)
    return _finish(pixmap, p)


def stop_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _token("danger")))
    p.drawRoundedRect(QRectF(7, 7, 18, 18), 3, 3)
    return _finish(pixmap, p)


def delete_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _token("danger")))
    p.drawRoundedRect(QRectF(11, 4, 10, 4), 2, 2)
    p.drawRoundedRect(QRectF(6, 9, 20, 4), 2, 2)
    p.drawRoundedRect(QRectF(9, 13, 14, 15), 3, 3)
    p.setBrush(QBrush(_token("surface")))
    for x in (12, 15.5, 19):
        p.drawRect(QRectF(x, 16, 2, 9))
    return _finish(pixmap, p)


def settings_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(color or _token("muted")))
    p.translate(16, 16)
    for i in range(8):
        p.save()
        p.rotate(i * 45)
        p.drawRoundedRect(QRectF(-2.5, -14, 5, 7), 2, 2)
        p.restore()
    p.drawEllipse(QPointF(0, 0), 9, 9)
    p.setBrush(QBrush(_token("surface")))
    p.drawEllipse(QPointF(0, 0), 4, 4)
    return _finish(pixmap, p)


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


def resume_all_icon(color: QColor | None = None) -> QIcon:
    """Two chevrons pointing down - "everything, continue"."""
    pixmap, p = _canvas()
    _stroke(p, color or _accent(), 2.6)
    for offset in (0, 8):
        p.drawPolyline([QPointF(9, 9 + offset), QPointF(16, 16 + offset),
                        QPointF(23, 9 + offset)])
    return _finish(pixmap, p)


def open_file_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    _stroke(p, color or _accent())
    path = QPainterPath()
    path.moveTo(9, 27)
    path.lineTo(9, 5)
    path.lineTo(18, 5)
    path.lineTo(23, 10)
    path.lineTo(23, 27)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(13, 14), QPointF(19, 14))
    p.drawLine(QPointF(13, 19), QPointF(19, 19))
    return _finish(pixmap, p)


def folder_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    _stroke(p, color or _accent())
    path = QPainterPath()
    path.moveTo(5, 25)
    path.lineTo(5, 9)
    path.lineTo(13, 9)
    path.lineTo(16, 12)
    path.lineTo(27, 12)
    path.lineTo(27, 25)
    path.closeSubpath()
    p.drawPath(path)
    return _finish(pixmap, p)


def link_icon(color: QColor | None = None) -> QIcon:
    """Two chain links - copy URL."""
    pixmap, p = _canvas()
    _stroke(p, color or _accent(), 2.4)
    p.drawArc(QRectF(4, 12, 14, 12), 90 * 16, 180 * 16)
    p.drawArc(QRectF(14, 8, 14, 12), 270 * 16, 180 * 16)
    p.drawLine(QPointF(12, 16), QPointF(20, 16))
    return _finish(pixmap, p)


def refresh_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    accent = color or _accent()
    _stroke(p, accent, 2.4)
    p.drawArc(QRectF(6, 6, 20, 20), 40 * 16, 280 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(accent))
    path = QPainterPath()
    path.moveTo(QPointF(26, 4))
    path.lineTo(QPointF(28, 14))
    path.lineTo(QPointF(18, 11))
    path.closeSubpath()
    p.drawPath(path)
    return _finish(pixmap, p)


def info_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    accent = color or _accent()
    _stroke(p, accent, 2.2)
    p.drawEllipse(QPointF(16, 16), 11, 11)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(accent))
    p.drawEllipse(QPointF(16, 10.5), 1.6, 1.6)
    p.drawRoundedRect(QRectF(14.6, 14, 2.8, 9), 1.4, 1.4)
    return _finish(pixmap, p)


def shield_icon(color: QColor | None = None) -> QIcon:
    """Checksum: a shield with a tick."""
    pixmap, p = _canvas()
    accent = color or _accent()
    _stroke(p, accent, 2.2)
    path = QPainterPath()
    path.moveTo(16, 4)
    path.lineTo(27, 8)
    path.lineTo(27, 16)
    path.cubicTo(27, 23, 22, 27, 16, 29)
    path.cubicTo(10, 27, 5, 23, 5, 16)
    path.lineTo(5, 8)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline([QPointF(11, 16), QPointF(15, 20), QPointF(22, 12)])
    return _finish(pixmap, p)


def queue_icon(color: QColor | None = None) -> QIcon:
    """Stacked layers - a download queue."""
    pixmap, p = _canvas()
    accent = color or _accent()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(accent))
    p.drawRoundedRect(QRectF(5, 6, 22, 5), 2, 2)
    c = QColor(accent)
    c.setAlpha(170)
    p.setBrush(QBrush(c))
    p.drawRoundedRect(QRectF(5, 14, 22, 5), 2, 2)
    c.setAlpha(100)
    p.setBrush(QBrush(c))
    p.drawRoundedRect(QRectF(5, 22, 22, 5), 2, 2)
    return _finish(pixmap, p)


def clipboard_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    accent = color or _accent()
    _stroke(p, accent)
    p.drawRoundedRect(QRectF(7, 6, 18, 22), 3, 3)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(accent))
    p.drawRoundedRect(QRectF(12, 3, 8, 6), 2, 2)
    return _finish(pixmap, p)


def dropbox_icon(color: QColor | None = None) -> QIcon:
    """The floating target: a dashed box with an arrow going into it."""
    pixmap, p = _canvas()
    accent = color or _accent()
    pen = QPen(accent)
    pen.setWidthF(2.0)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(5, 12, 22, 16), 4, 4)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(accent))
    p.drawRoundedRect(QRectF(14, 3, 4, 9), 2, 2)
    path = QPainterPath()
    path.moveTo(QPointF(10, 10))
    path.lineTo(QPointF(22, 10))
    path.lineTo(QPointF(16, 18))
    path.closeSubpath()
    p.drawPath(path)
    return _finish(pixmap, p)


def exit_icon(color: QColor | None = None) -> QIcon:
    pixmap, p = _canvas()
    _stroke(p, color or _muted(), 2.4)
    p.drawArc(QRectF(6, 6, 20, 20), 60 * 16, 300 * 16)
    p.drawLine(QPointF(16, 4), QPointF(16, 14))
    return _finish(pixmap, p)


def category_icon(name: str, color: QColor | None = None) -> QIcon:
    """One glyph per category in the left-hand tree."""
    pixmap, p = _canvas()
    accent = color or _accent()
    if name == "Video":
        _stroke(p, accent)
        p.drawRoundedRect(QRectF(4, 8, 17, 16), 3, 3)
        path = QPainterPath()
        path.moveTo(23, 12)
        path.lineTo(28, 9)
        path.lineTo(28, 23)
        path.lineTo(23, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "Music":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(accent))
        p.drawEllipse(QPointF(11, 23), 4.5, 3.6)
        p.drawEllipse(QPointF(23, 20), 4.5, 3.6)
        p.drawRoundedRect(QRectF(14.5, 6, 2.6, 17), 1.3, 1.3)
        p.drawRoundedRect(QRectF(26, 4, 2.6, 16), 1.3, 1.3)
        p.drawRoundedRect(QRectF(14.5, 5, 14, 3), 1.5, 1.5)
    elif name == "Compressed":
        _stroke(p, accent)
        p.drawRoundedRect(QRectF(7, 5, 18, 23), 3, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(accent))
        for y in (8, 12, 16, 20):
            p.drawRect(QRectF(15, y, 3, 3))
    elif name == "Documents":
        _stroke(p, accent)
        path = QPainterPath()
        path.moveTo(8, 27)
        path.lineTo(8, 5)
        path.lineTo(19, 5)
        path.lineTo(24, 10)
        path.lineTo(24, 27)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(12, 15), QPointF(20, 15))
        p.drawLine(QPointF(12, 20), QPointF(20, 20))
    elif name == "Programs":
        _stroke(p, accent)
        p.drawRoundedRect(QRectF(5, 7, 22, 18), 3, 3)
        p.drawLine(QPointF(5, 13), QPointF(27, 13))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(accent))
        for x in (9, 13, 17):
            p.drawEllipse(QPointF(float(x), 10.0), 1.3, 1.3)
    else:  # General / anything new
        _stroke(p, color or _muted())
        p.drawRoundedRect(QRectF(7, 6, 18, 21), 3, 3)
        p.drawLine(QPointF(11, 13), QPointF(21, 13))
        p.drawLine(QPointF(11, 18), QPointF(21, 18))
    return _finish(pixmap, p)


def filter_icon(name: str, color: QColor | None = None) -> QIcon:
    """Glyphs for the All / Unfinished / Finished rows."""
    pixmap, p = _canvas()
    accent = color or _accent()
    if name == "finished":
        _stroke(p, color or QColor(_accent()), 2.6)
        p.drawPolyline([QPointF(7, 17), QPointF(13, 23), QPointF(25, 9)])
    elif name == "unfinished":
        _stroke(p, accent, 2.4)
        p.drawEllipse(QPointF(16, 16), 10, 10)
        p.drawLine(QPointF(16, 16), QPointF(16, 9))
        p.drawLine(QPointF(16, 16), QPointF(21, 18))
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(accent))
        for y in (7, 14, 21):
            p.drawRoundedRect(QRectF(6, y, 20, 4), 2, 2)
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
