"""Per-download progress window: speed graph and live segment map."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import deque

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.task import TaskState
from ..util.fmt import human_duration, human_size, human_speed
from . import theme
from .controller import Controller, DownloadItem
from .i18n import tr

def _accent() -> QColor:
    return theme.current().color("accent")


def _accent_soft() -> QColor:
    return theme.current().alpha("accent", 70)


def _track() -> QColor:
    return theme.current().color("border")


class SpeedGraph(QWidget):
    """Rolling chart of the last ~2 minutes of transfer rate."""

    def __init__(self, parent=None, capacity: int = 120) -> None:
        super().__init__(parent)
        self._samples: deque[float] = deque(maxlen=capacity)
        self.setMinimumHeight(90)

    def add_sample(self, speed: float) -> None:
        self._samples.append(max(0.0, speed))
        self.update()

    def clear(self) -> None:
        self._samples.clear()
        self.update()

    def paintEvent(self, event) -> None:
        if theme.current().iso:
            self._paint_iso()
            return
        if theme.current().pixel:
            self._paint_pixel()
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillRect(rect, self.palette().base())
        painter.setPen(QPen(_track(), 1))
        painter.drawRect(rect)

        for i in range(1, 4):
            y = rect.top() + rect.height() * i / 4
            painter.drawLine(rect.left(), y, rect.right(), y)

        if len(self._samples) < 2:
            return
        peak = max(self._samples) or 1.0
        step = rect.width() / (self._samples.maxlen - 1)
        # Anchor the newest sample to the right edge so the chart scrolls in
        # from the right instead of clinging to the left while it fills up.
        first_x = rect.right() - (len(self._samples) - 1) * step
        path = QPainterPath()
        base_y = rect.bottom()
        for i, value in enumerate(self._samples):
            x = first_x + i * step
            y = base_y - (value / (peak * 1.15)) * rect.height()
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        fill = QPainterPath(path)
        fill.lineTo(rect.right(), base_y)
        fill.lineTo(first_x, base_y)
        fill.closeSubpath()
        painter.fillPath(fill, QBrush(_accent_soft()))
        painter.setPen(QPen(_accent(), 1.6))
        painter.drawPath(path)

        painter.setPen(self.palette().text().color())
        painter.drawText(
            rect.adjusted(6, 3, -6, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            human_speed(peak),
        )


    def _paint_iso(self) -> None:
        """The rolling rate as a skyline of towers, newest on the right."""
        from . import voxel

        palette = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, palette.color("track"))
        painter.setPen(QPen(palette.color("border"), 2))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        if not self._samples:
            return

        peak = max(self._samples) or 1.0
        step = 10
        columns = max(1, (rect.width() - 24) // step)
        recent = list(self._samples)[-columns:]
        # Same reasoning as the table delegate: the towers stand in a straight
        # line across the widget, so the camera moves rather than the grid.
        levels_max = max(1, (rect.height() - 40) // 6)
        for index, value in enumerate(recent):
            levels = max(1, int(round(value / (peak * 1.05) * levels_max)))
            offset = columns - len(recent) + index
            camera = voxel.Camera(
                origin_x=rect.left() + 16 + offset * step,
                origin_y=rect.bottom() - 16,
                tile_w=step // 2, tile_h=max(2, step // 4), voxel_h=6,
            )
            voxel.draw_stack(
                painter, camera, 0, 0, levels,
                color=palette.color("success" if levels < levels_max * 0.75 else "accent"),
            )

        painter.setPen(palette.color("text"))
        painter.setFont(theme.font_for(human_speed(peak), 9))
        painter.drawText(
            rect.adjusted(6, 4, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            human_speed(peak),
        )

    #: pixel mode: one bar every CELL px, quantised to STEPS heights
    CELL = 6
    STEPS = 10

    def _paint_pixel(self) -> None:
        """The same data as a spectrum-analyser bar chart.

        Quantising the height is the point: a smooth curve is exactly what a
        machine with a 4-colour tile grid could not draw, and the steps are
        what make it read as a game.
        """
        palette = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, palette.color("track"))
        painter.setPen(QPen(palette.color("border"), 2))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        if not self._samples:
            return
        peak = max(self._samples) or 1.0
        inner = rect.adjusted(3, 3, -3, -3)
        columns = max(1, inner.width() // self.CELL)
        recent = list(self._samples)[-columns:]
        row_height = max(1, inner.height() // self.STEPS)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, value in enumerate(recent):
            filled = int(round(value / (peak * 1.05) * self.STEPS))
            x = inner.left() + (columns - len(recent) + index) * self.CELL
            for row in range(filled):
                # Green at the bottom, amber at the top: a level meter.
                token = "success" if row < self.STEPS * 0.7 else "accent"
                painter.setBrush(palette.color(token))
                y = inner.bottom() - (row + 1) * row_height + 1
                painter.drawRect(x, y, self.CELL - 1, row_height - 1)

        painter.setPen(palette.color("text"))
        painter.setFont(theme.font_for(human_speed(peak), 9))
        painter.drawText(
            rect.adjusted(6, 4, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            human_speed(peak),
        )


class SegmentBar(QWidget):
    """One stripe per segment - makes dynamic splitting visible."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._segments: list[tuple[int, int, int | None]] = []
        self._size = 0
        self.setMinimumHeight(26)

    def set_segments(self, segments, size: int | None) -> None:
        self._segments = list(segments or [])
        self._size = size or 0
        self.update()

    RADIUS = 6

    #: pixel mode: width of one cell in the segment bar
    CELL = 7

    def _paint_pixel(self) -> None:
        """A row of discrete cells, lit as the bytes arrive."""
        palette = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, palette.color("track"))

        inner = rect.adjusted(3, 3, -3, -3)
        cells = max(1, inner.width() // self.CELL)
        done = [0.0] * cells
        if self._size and self._segments:
            for start, current, _end in self._segments:
                if current <= start:
                    continue
                first = int(start / self._size * cells)
                last = min(cells, int(current / self._size * cells) + 1)
                for index in range(max(0, first), last):
                    done[index] = 1.0
        painter.setPen(Qt.PenStyle.NoPen)
        for index, value in enumerate(done):
            painter.setBrush(
                palette.color("accent") if value else palette.color("surface_alt")
            )
            painter.drawRect(
                inner.left() + index * self.CELL, inner.top(),
                self.CELL - 1, inner.height(),
            )
        # Where each segment starts, so a dynamic split still shows.
        if self._size and self._segments:
            painter.setPen(QPen(palette.color("danger"), 1))
            for start, _current, _end in self._segments:
                if start == 0:
                    continue
                x = inner.left() + int(start / self._size * cells) * self.CELL
                painter.drawLine(x, inner.top(), x, inner.bottom())

        painter.setPen(QPen(palette.color("border"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

    def paintEvent(self, event) -> None:
        if theme.current().pixel:
            self._paint_pixel()
            return
        palette = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        clip = QPainterPath()
        clip.addRoundedRect(rect, self.RADIUS, self.RADIUS)
        painter.setClipPath(clip)
        painter.fillRect(rect, palette.color("track"))

        if self._size and self._segments:
            scale = rect.width() / self._size
            painter.setPen(Qt.PenStyle.NoPen)
            for start, current, _end in self._segments:
                if current <= start:
                    continue
                x = rect.left() + start * scale
                width = max(1.0, (current - start) * scale)
                painter.fillRect(
                    QRectF(x, rect.top(), width, rect.height()), palette.color("accent")
                )
            # A hairline where each segment begins: that is what makes a
            # dynamic split visible the moment it happens.
            painter.setPen(QPen(palette.alpha("window", 200), 1))
            for start, _current, _end in self._segments:
                if start == 0:
                    continue
                x = rect.left() + start * scale
                painter.drawLine(x, rect.top(), x, rect.bottom())

        painter.setClipping(False)
        painter.setPen(QPen(palette.color("border"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)


class ProgressDialog(QDialog):
    def __init__(self, controller: Controller, item: DownloadItem, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.item = item
        self.setWindowTitle(tr("Download progress"))
        self.setMinimumWidth(560)

        self.name_label = QLabel(item.filename)
        font = self.name_label.font()
        font.setBold(True)
        self.name_label.setFont(font)
        self.url_label = QLabel(item.url)
        self.url_label.setWordWrap(True)
        self.url_label.setEnabled(False)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)

        self.transferred = QLabel("-")
        self.left = QLabel("-")
        self.speed = QLabel("-")
        self.status = QLabel("-")
        info = QFormLayout()
        info.addRow(tr("Status") + ":", self.status)
        info.addRow(tr("Transferred:"), self.transferred)
        info.addRow(tr("Time left:"), self.left)
        info.addRow(tr("Speed") + ":", self.speed)

        self.graph = SpeedGraph()
        graph_box = QGroupBox(tr("Transfer rate"))
        graph_layout = QVBoxLayout(graph_box)
        graph_layout.addWidget(self.graph)

        self.segments = SegmentBar()
        seg_box = QGroupBox(tr("Segments"))
        seg_layout = QVBoxLayout(seg_box)
        seg_layout.addWidget(self.segments)

        self.open_when_done = QCheckBox(tr("Open file when done"))
        self.toggle_button = QPushButton(tr("Pause"))
        self.toggle_button.clicked.connect(self._toggle)
        close_button = QPushButton(tr("Close"))
        close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_when_done)
        buttons.addStretch(1)
        buttons.addWidget(self.toggle_button)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.name_label)
        layout.addWidget(self.url_label)
        layout.addWidget(self.bar)
        layout.addLayout(info)
        layout.addWidget(graph_box, 1)  # absorbs any extra height
        layout.addWidget(seg_box)
        layout.addLayout(buttons)

        controller.itemChanged.connect(self._on_item_changed)
        self.refresh()

    # ------------------------------------------------------------- behaviour

    def _on_item_changed(self, item: DownloadItem) -> None:
        if item.db_id != self.item.db_id:
            return
        self.item = item
        self.refresh()
        if item.state is TaskState.COMPLETED and self.open_when_done.isChecked():
            self.open_when_done.setChecked(False)
            _open_path(str(item.path))

    def refresh(self) -> None:
        item = self.item
        self.name_label.setText(item.filename)
        self.bar.setValue(int(item.percent))
        total = human_size(item.size) if item.size else "?"
        self.transferred.setText(f"{human_size(item.downloaded)} / {total}")
        self.left.setText(
            human_duration(item.eta) if item.state is TaskState.DOWNLOADING else "-"
        )
        self.speed.setText(human_speed(item.speed) if item.speed else "-")
        self.status.setText(item.error or tr(item.state.value))
        self.segments.set_segments(item.segments, item.size)
        if item.state is TaskState.DOWNLOADING:
            self.graph.add_sample(item.speed)
        self.toggle_button.setText(
            tr("Pause") if item.is_live else tr("Resume")
        )
        self.toggle_button.setEnabled(item.state is not TaskState.COMPLETED)

    def _toggle(self) -> None:
        if self.item.is_live:
            self.controller.pause_item(self.item.db_id)
        else:
            self.controller.start_item(self.item.db_id)

    def closeEvent(self, event) -> None:
        try:
            self.controller.itemChanged.disconnect(self._on_item_changed)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)


def _open_path(path: str) -> None:
    """Open a file with the OS default handler."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - intended behaviour
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass
