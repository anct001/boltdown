"""How much has actually been downloaded, drawn from the history table.

No new bookkeeping: every finished download is already archived, so the
numbers here are a query, not a second source of truth that could drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..storage.db import Database
from ..util.fmt import human_size
from . import theme
from .i18n import tr

DAYS = 30


def daily_totals(rows, days: int = DAYS, today: date | None = None) -> list[tuple[date, int]]:
    """Bytes per day for the last `days` days, oldest first, gaps included."""
    today = today or date.today()
    buckets = {today - timedelta(days=offset): 0 for offset in range(days)}
    for row in rows:
        finished = row["finished_at"] or 0
        when = datetime.fromtimestamp(finished).date()
        if when in buckets:
            buckets[when] += int(row["size"] or 0)
    return sorted(buckets.items())


@dataclass(slots=True)
class Summary:
    """The numbers shown at the top - computed apart from the widgets."""

    files: int = 0
    total: int = 0
    average: int = 0
    per_day: int = 0
    first: float | None = None
    largest_name: str = ""
    largest_size: int = 0


def summarise(rows) -> Summary:
    finished = [r for r in rows if r["state"] == "completed"]
    if not finished:
        return Summary()
    total = sum(int(r["size"] or 0) for r in finished)
    stamps = [r["finished_at"] for r in finished if r["finished_at"]]
    days_active = len({datetime.fromtimestamp(t).date() for t in stamps}) or 1
    biggest = max(finished, key=lambda r: r["size"] or 0)
    return Summary(
        files=len(finished),
        total=total,
        average=total // len(finished),
        per_day=total // days_active,
        first=min(stamps) if stamps else None,
        largest_name=biggest["filename"] or "",
        largest_size=int(biggest["size"] or 0),
    )


class DailyChart(QWidget):
    """A plain bar per day - no axes, the numbers are in the form above."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._data: list[tuple[date, int]] = []
        self.setMinimumHeight(140)

    def set_data(self, data: list[tuple[date, int]]) -> None:
        self._data = list(data)
        self.update()

    #: pixel mode: height of one stacked block
    BLOCK = 6

    def _paint_pixel(self, painter, palette) -> None:
        """The same 30 days, stacked out of blocks instead of drawn as bars."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, palette.color("track"))
        painter.setPen(QPen(palette.color("border"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))
        if not self._data:
            return
        peak = max((value for _day, value in self._data), default=0)
        if peak <= 0:
            painter.setPen(QPen(palette.color("muted")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, tr("No downloads yet"))
            return

        inner = rect.adjusted(6, 18, -6, -6)
        step = max(1, inner.width() // max(1, len(self._data)))
        rows = max(1, inner.height() // self.BLOCK)
        painter.setPen(Qt.PenStyle.NoPen)
        for index, (_day, value) in enumerate(self._data):
            blocks = int(round(rows * (value / peak)))
            x = inner.left() + index * step
            for row in range(blocks):
                painter.setBrush(
                    palette.color("accent" if row == blocks - 1 else "success")
                )
                y = inner.bottom() - (row + 1) * self.BLOCK
                painter.drawRect(x, y, max(2, step - 2), self.BLOCK - 1)

        painter.setPen(QPen(palette.color("text")))
        painter.setFont(theme.pixel_font(9))
        painter.drawText(
            rect.adjusted(6, 3, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            human_size(peak),
        )

    def paintEvent(self, event) -> None:
        palette = theme.current()
        painter = QPainter(self)
        if palette.pixel:
            self._paint_pixel(painter, palette)
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(palette.color("border"), 1))
        painter.setBrush(QBrush(palette.color("track")))
        painter.drawRoundedRect(rect, 8, 8)
        if not self._data:
            return

        peak = max((value for _day, value in self._data), default=0)
        if peak <= 0:
            painter.setPen(QPen(palette.color("muted")))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, tr("No downloads yet"))
            return

        inner = rect.adjusted(8, 8, -8, -8)
        step = inner.width() / len(self._data)
        width = max(2.0, step * 0.68)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(palette.color("accent")))
        for index, (_day, value) in enumerate(self._data):
            height = inner.height() * (value / peak)
            x = inner.left() + index * step + (step - width) / 2
            painter.drawRoundedRect(
                QRectF(x, inner.bottom() - height, width, height), 2, 2
            )

        painter.setPen(QPen(palette.color("muted")))
        painter.drawText(
            rect.adjusted(8, 4, -8, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            human_size(peak),
        )


class StatsDialog(QDialog):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(tr("Statistics"))
        self.setMinimumWidth(560)

        rows = db.list_history(limit=100_000)
        self.summary = summarise(rows)
        self.chart = DailyChart()
        self.chart.set_data(
            daily_totals([r for r in rows if r["state"] == "completed"])
        )

        form = QFormLayout()
        form.addRow(tr("Files downloaded:"), QLabel(str(self.summary.files)))
        form.addRow(tr("Total size:"), QLabel(human_size(self.summary.total)))
        form.addRow(tr("Average file:"), QLabel(
            human_size(self.summary.average) if self.summary.files else "-"
        ))
        form.addRow(tr("Per day:"), QLabel(human_size(self.summary.per_day)))
        if self.summary.first:
            form.addRow(tr("Since:"), QLabel(
                datetime.fromtimestamp(self.summary.first).strftime("%d/%m/%Y")
            ))
        if self.summary.largest_name:
            form.addRow(tr("Largest:"), QLabel(
                f"{self.summary.largest_name} ({human_size(self.summary.largest_size)})"
            ))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel(tr("Last 30 days")))
        layout.addWidget(self.chart, 1)
        layout.addWidget(buttons)
