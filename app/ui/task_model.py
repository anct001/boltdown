"""Table model + progress-bar delegate for the download list."""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
)

from ..core.task import TaskState
from ..util.fmt import human_duration, human_size, human_speed
from .controller import Controller, DownloadItem
from .i18n import tr

COL_NAME, COL_SIZE, COL_STATUS, COL_LEFT, COL_SPEED, COL_ADDED = range(6)
COLUMNS = ("Name", "Size", "Status", "Left", "Speed", "Added")

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
PERCENT_ROLE = Qt.ItemDataRole.UserRole + 2
SORT_ROLE = Qt.ItemDataRole.UserRole + 3

_STATE_COLORS = {
    TaskState.COMPLETED: QColor("#2e7d32"),
    TaskState.ERROR: QColor("#c62828"),
    TaskState.PAUSED: QColor("#ef6c00"),
    TaskState.CANCELLED: QColor("#757575"),
}


class DownloadTableModel(QAbstractTableModel):
    def __init__(self, controller: Controller, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        # Seed from whatever the controller already restored from SQLite - the
        # model is usually built after Controller.start() has run.
        self._items: list[DownloadItem] = controller.items()
        self._row_by_id: dict[int, int] = {}
        self._reindex()

        controller.itemAdded.connect(self.add_item)
        controller.itemChanged.connect(self.update_item)
        controller.itemRemoved.connect(self.remove_item)

    # ------------------------------------------------------- Qt model plumbing

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return tr(COLUMNS[section])

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self._items[index.row()]
        column = index.column()

        if role == ITEM_ROLE:
            return item
        if role == PERCENT_ROLE:
            return item.percent
        if role == SORT_ROLE:
            return self._sort_key(item, column)
        if role == Qt.ItemDataRole.ForegroundRole and column == COL_STATUS:
            return _STATE_COLORS.get(item.state)
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.error or item.url
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (
            COL_SIZE, COL_LEFT, COL_SPEED
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if column == COL_NAME:
            return item.filename
        if column == COL_SIZE:
            return human_size(item.size)
        if column == COL_STATUS:
            return self.status_text(item)
        if column == COL_LEFT:
            if item.state is TaskState.DOWNLOADING and item.eta is not None:
                return human_duration(item.eta)
            if item.size and item.state is not TaskState.COMPLETED:
                return human_size(max(0, item.size - item.downloaded))
            return ""
        if column == COL_SPEED:
            return human_speed(item.speed) if item.speed > 0 else ""
        if column == COL_ADDED:
            return datetime.fromtimestamp(item.added_at).strftime("%d/%m/%Y %H:%M")
        return None

    @staticmethod
    def status_text(item: DownloadItem) -> str:
        if item.state is TaskState.ERROR:
            return f"{tr('error')}: {item.error or ''}".strip(": ")
        if item.state in (TaskState.DOWNLOADING, TaskState.PAUSED) and item.size:
            return f"{item.percent:.1f}%"
        return tr(item.state.value)

    @staticmethod
    def _sort_key(item: DownloadItem, column: int):
        return {
            COL_NAME: item.filename.lower(),
            COL_SIZE: item.size or 0,
            COL_STATUS: item.percent,
            COL_LEFT: item.eta if item.eta is not None else float("inf"),
            COL_SPEED: item.speed,
            COL_ADDED: item.added_at,
        }[column]

    # ------------------------------------------------------------- mutations

    def add_item(self, item: DownloadItem) -> None:
        row = len(self._items)
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.append(item)
        self._row_by_id[item.db_id] = row
        self.endInsertRows()

    def update_item(self, item: DownloadItem) -> None:
        row = self._row_by_id.get(item.db_id)
        if row is None:
            return
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, len(COLUMNS) - 1)
        )

    def remove_item(self, db_id: int) -> None:
        row = self._row_by_id.get(db_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._items[row]
        self._reindex()
        self.endRemoveRows()

    def _reindex(self) -> None:
        self._row_by_id = {item.db_id: i for i, item in enumerate(self._items)}

    def item_at(self, row: int) -> DownloadItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None


class DownloadFilterProxy(QSortFilterProxyModel):
    """Left-hand tree selection: all / unfinished / finished / category / queue."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self._kind = "all"
        self._value = ""
        self._text = ""

    def set_filter(self, kind: str, value: str = "") -> None:
        self._kind = kind
        self._value = value
        self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        item = model.item_at(source_row)
        if item is None:
            return False
        if self._text and self._text not in item.filename.lower() and self._text not in item.url.lower():
            return False
        if self._kind == "all":
            return True
        if self._kind == "unfinished":
            return item.state is not TaskState.COMPLETED
        if self._kind == "finished":
            return item.state is TaskState.COMPLETED
        if self._kind == "category":
            return item.category == self._value
        if self._kind == "queue":
            return str(item.queue_id or "") == str(self._value)
        return True


class ProgressDelegate(QStyledItemDelegate):
    """Draws the Status column as a progress bar, like IDM's list."""

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item: DownloadItem | None = index.data(ITEM_ROLE)
        if item is None or item.state in (TaskState.ERROR, TaskState.CANCELLED) or not item.size:
            super().paint(painter, option, index)
            return

        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(3, 5, -3, -5)
        bar.minimum = 0
        bar.maximum = 100
        bar.progress = int(item.percent)
        bar.text = DownloadTableModel.status_text(item)
        bar.textVisible = True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        style = QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, bar, painter)


def elapsed_since(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    return human_duration(time.time() - timestamp)
