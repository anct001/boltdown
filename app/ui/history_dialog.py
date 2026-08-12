"""What has been downloaded, long after the row left the list."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..storage.db import Database
from ..util.fmt import human_size
from .controller import Controller
from .i18n import tr
from .task_model import state_color

ROW_ROLE = Qt.ItemDataRole.UserRole + 1


class HistoryDialog(QDialog):
    def __init__(self, controller: Controller, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.db = db
        self.setWindowTitle(tr("History"))
        self.setMinimumSize(760, 480)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)

        self.table = QTreeWidget()
        self.table.setHeaderLabels(
            [tr("Name"), tr("Size"), tr("Status"), tr("Added"), "URL"]
        )
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.again = QPushButton(tr("Download again"))
        self.again.clicked.connect(self.download_again)
        self.copy = QPushButton(tr("Copy URL"))
        self.copy.clicked.connect(self.copy_url)
        self.forget = QPushButton(tr("Remove from list"))
        self.forget.clicked.connect(self.forget_selected)
        clear = QPushButton(tr("Clear history"))
        clear.clicked.connect(self.clear_history)

        tools = QHBoxLayout()
        tools.addWidget(self.search, 1)
        for button in (self.again, self.copy, self.forget, clear):
            tools.addWidget(button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(tools)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)

        self.reload()

    # ------------------------------------------------------------------ data

    def reload(self) -> None:
        self.table.clear()
        for row in self.db.list_history(self.search.text().strip()):
            when = datetime.fromtimestamp(row["finished_at"] or 0)
            entry = QTreeWidgetItem([
                row["filename"] or "-",
                human_size(row["size"]),
                tr(row["state"]),
                when.strftime("%d/%m/%Y %H:%M"),
                row["url"],
            ])
            colour = state_color(_state_of(row["state"]))
            if colour is not None:
                entry.setForeground(2, colour)
            entry.setData(0, ROW_ROLE, dict(row))
            self.table.addTopLevelItem(entry)
        self.table.resizeColumnToContents(0)
        self._update_buttons()

    def selected(self) -> list[dict]:
        return [item.data(0, ROW_ROLE) for item in self.table.selectedItems()]

    def _update_buttons(self) -> None:
        has = bool(self.table.selectedItems())
        for button in (self.again, self.copy, self.forget):
            button.setEnabled(has)

    # --------------------------------------------------------------- actions

    def download_again(self) -> None:
        for row in self.selected():
            self.controller.add(row["url"])
        self.accept()

    def copy_url(self) -> None:
        rows = self.selected()
        if rows:
            QGuiApplication.clipboard().setText("\n".join(r["url"] for r in rows))

    def forget_selected(self) -> None:
        for row in self.selected():
            self.db.delete_history(row["id"])
        self.reload()

    def clear_history(self) -> None:
        if QMessageBox.question(
            self, tr("History"), tr("Clear the whole history?")
        ) != QMessageBox.StandardButton.Yes:
            return
        self.db.clear_history()
        self.reload()


def _state_of(value: str):
    from ..core.task import TaskState

    try:
        return TaskState(value)
    except ValueError:
        return TaskState.COMPLETED
