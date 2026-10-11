"""The panel under the list: everything about the selected download.

What used to take a double-click and a dialog - where the file goes, the
address, how the segments are doing, what went wrong - sits under the list
for whatever row is selected, the way qBittorrent and Motrix show it.
F3 shows and hides it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.task import TaskState
from ..util.fmt import human_duration, human_size, human_speed
from .controller import DownloadItem
from .i18n import tr
from .progress_dialog import SegmentBar
from .task_model import friendly_time


class DetailsPanel(QFrame):
    """Shows one `DownloadItem`; `show_item(None)` for nothing selected."""

    openFileRequested = Signal(object)
    openFolderRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("detailsPanel")
        self.item: DownloadItem | None = None

        self.title = QLabel()
        self.title.setObjectName("detailsTitle")
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.1 if font.pointSizeF() > 0 else 11)
        self.title.setFont(font)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.fields: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        rows = (
            (("status", tr("Status")), ("size", tr("Size"))),
            (("done", tr("Transferred:").rstrip(":")), ("speed", tr("Speed"))),
            (("left", tr("Time left:").rstrip(":")), ("connections", tr("Connections"))),
            (("folder", tr("Folder")), ("added", tr("Added"))),
            (("url", tr("Address")), None),
        )
        for row, pairs in enumerate(rows):
            for col, pair in enumerate(pairs):
                if pair is None:
                    continue
                key, label = pair
                caption = QLabel(label)
                caption.setObjectName("detailsCaption")
                caption.setEnabled(False)
                value = QLabel("")
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value.setMinimumWidth(40)
                # Ignored width: the layout, not the text, sets the size, so
                # a long path elides instead of widening the window.
                value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                self.fields[key] = value
                grid.addWidget(caption, row, col * 2)
                span = 3 if key == "url" else 1
                grid.addWidget(value, row, col * 2 + 1, 1, span)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.error = QLabel()
        self.error.setObjectName("detailsError")
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.segments = SegmentBar()
        self.segments.setMinimumHeight(18)
        self.segments.setMaximumHeight(18)

        self.open_file = QPushButton(tr("Open"))
        self.open_file.clicked.connect(lambda: self.item and self.openFileRequested.emit(self.item))
        self.open_folder = QPushButton(tr("Open folder"))
        self.open_folder.clicked.connect(
            lambda: self.item and self.openFolderRequested.emit(self.item)
        )
        self.copy_link = QPushButton(tr("Copy URL"))
        self.copy_link.clicked.connect(self._copy_link)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for button in (self.open_file, self.open_folder, self.copy_link):
            buttons.addWidget(button)

        self.placeholder = QLabel(tr("Select a download to see its details."))
        self.placeholder.setEnabled(False)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        body.addWidget(self.title)
        body.addLayout(grid)
        body.addWidget(self.error)
        body.addWidget(self.segments)
        body.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(self.placeholder)
        layout.addWidget(self.body)
        self.show_item(None)

    # ------------------------------------------------------------------ data

    def show_item(self, item: DownloadItem | None) -> None:
        self.item = item
        self.placeholder.setVisible(item is None)
        self.body.setVisible(item is not None)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        if item is None or not self.isVisible():
            return
        self.title.setText(item.filename)
        state = item.state
        status = tr(state.value)
        if state in (TaskState.DOWNLOADING, TaskState.PAUSED) and item.size:
            status = f"{status} · {item.percent:.1f}%"
        f = self.fields
        f["status"].setText(status)
        f["size"].setText(human_size(item.size) if item.size else "—")
        f["done"].setText(human_size(item.downloaded) if item.downloaded else "—")
        downloading = state is TaskState.DOWNLOADING
        f["speed"].setText(human_speed(item.speed) if downloading and item.speed > 0 else "—")
        f["left"].setText(
            human_duration(item.eta) if downloading and item.eta is not None else "—"
        )
        live = [s for s in item.segments if s[2] is None or s[1] <= s[2]]
        f["connections"].setText(str(len(live)) if downloading and live else "—")
        _elide(f["folder"], str(Path(item.save_path)))
        f["added"].setText(friendly_time(item.added_at))
        _elide(f["url"], item.url)
        self.error.setVisible(bool(item.error) and state is TaskState.ERROR)
        self.error.setText(item.error or "")
        self.segments.setVisible(bool(item.segments) and bool(item.size))
        self.segments.set_segments(item.segments, item.size)
        self.open_file.setEnabled(state is TaskState.COMPLETED)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refresh()  # long paths and addresses re-elide to the new width

    def _copy_link(self) -> None:
        if self.item is not None:
            QGuiApplication.clipboard().setText(self.item.url)


def _elide(label: QLabel, text: str) -> None:
    """Long paths lose their middle, not their end: the file name is what
    tells two of them apart. The whole text stays in the tooltip."""
    width = max(120, label.width() - 4)
    label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, width))
    label.setToolTip(text)
