"""Editing the per-host rules.

Deliberately a plain table with a form under it rather than an in-place
editor: the fields are heterogeneous (numbers, a speed with a unit, free
text) and half of them are usually blank.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.profiles import SiteProfile
from ..storage.db import Database
from ..util.fmt import human_size, parse_size
from .i18n import tr

ROW_ROLE = Qt.ItemDataRole.UserRole + 1
#: 0 in the connections box means "leave it to the global setting"
INHERIT = 0


class SiteProfilesDialog(QDialog):
    def __init__(self, controller, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.db = db
        self.setWindowTitle(tr("Site rules"))
        self.setMinimumSize(760, 520)

        self.table = QTreeWidget()
        self.table.setHeaderLabels(
            [tr("Host"), tr("Connections:"), tr("Speed limit:"), tr("Note")]
        )
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.currentItemChanged.connect(self._load_selected)

        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("example.com  /  *.example.com  /  *")
        self.enabled = QCheckBox(tr("Enabled"))
        self.enabled.setChecked(True)
        self.connections = QSpinBox()
        self.connections.setRange(INHERIT, 32)
        self.connections.setSpecialValueText(tr("default"))
        self.limit = QLineEdit()
        self.limit.setPlaceholderText(tr("unlimited"))
        self.user_agent = QLineEdit()
        self.user_agent.setPlaceholderText(tr("auto"))
        self.referer = QLineEdit()
        self.cookie = QLineEdit()
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("http://127.0.0.1:8080  /  socks5://127.0.0.1:1080")
        self.note = QLineEdit()

        form = QFormLayout()
        form.addRow(tr("Host:"), self.pattern)
        form.addRow("", self.enabled)
        form.addRow(tr("Connections:"), self.connections)
        form.addRow(tr("Speed limit:"), self.limit)
        form.addRow(tr("User-Agent:"), self.user_agent)
        form.addRow(tr("Referer:"), self.referer)
        form.addRow(tr("Cookie:"), self.cookie)
        form.addRow(tr("Proxy:"), self.proxy)
        form.addRow(tr("Note"), self.note)

        new = QPushButton(tr("New"))
        new.clicked.connect(self.clear_form)
        save = QPushButton(tr("Save"))
        save.clicked.connect(self.save_current)
        self.remove = QPushButton(tr("Delete"))
        self.remove.clicked.connect(self.delete_current)
        tools = QHBoxLayout()
        tools.addWidget(new)
        tools.addWidget(save)
        tools.addWidget(self.remove)
        tools.addStretch(1)

        hint = QLabel(
            tr("The narrowest match wins: cdn.example.com beats *.example.com "
               "beats *. Empty fields are left to the download itself.")
        )
        hint.setWordWrap(True)
        hint.setEnabled(False)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(form)
        layout.addLayout(tools)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        self.reload()

    # ------------------------------------------------------------------ data

    def reload(self) -> None:
        self.table.clear()
        for profile in self.controller.profiles():
            entry = QTreeWidgetItem([
                profile.pattern,
                str(profile.connections) if profile.connections else tr("default"),
                human_size(profile.speed_limit) if profile.speed_limit else tr("unlimited"),
                profile.note,
            ])
            if not profile.enabled:
                entry.setText(0, f"{profile.pattern}  ({tr('disabled')})")
            entry.setData(0, ROW_ROLE, profile)
            self.table.addTopLevelItem(entry)
        self.table.resizeColumnToContents(0)
        self._update_buttons()

    def selected(self) -> SiteProfile | None:
        entry = self.table.currentItem()
        return entry.data(0, ROW_ROLE) if entry is not None else None

    def _update_buttons(self) -> None:
        self.remove.setEnabled(self.selected() is not None)

    def _load_selected(self, *_args) -> None:
        profile = self.selected()
        self._update_buttons()
        if profile is None:
            return
        self.pattern.setText(profile.pattern)
        self.enabled.setChecked(profile.enabled)
        self.connections.setValue(profile.connections or INHERIT)
        self.limit.setText(
            human_size(profile.speed_limit).replace(" ", "") if profile.speed_limit else ""
        )
        self.user_agent.setText(profile.user_agent or "")
        self.referer.setText(profile.referer or "")
        self.cookie.setText(profile.cookie or "")
        self.proxy.setText(profile.proxy or "")
        self.note.setText(profile.note or "")

    def clear_form(self) -> None:
        self.table.setCurrentItem(None)
        for field in (self.pattern, self.limit, self.user_agent, self.referer,
                      self.cookie, self.proxy, self.note):
            field.clear()
        self.enabled.setChecked(True)
        self.connections.setValue(INHERIT)
        self.pattern.setFocus()

    # --------------------------------------------------------------- actions

    def save_current(self) -> None:
        pattern = self.pattern.text().strip()
        if not pattern:
            QMessageBox.warning(self, tr("Site rules"), tr("Enter a host"))
            return
        try:
            limit = parse_size(self.limit.text().strip()) if self.limit.text().strip() else None
        except ValueError:
            QMessageBox.warning(self, tr("Site rules"), tr("Speed limit:"))
            return
        self.db.save_profile(
            pattern,
            enabled=self.enabled.isChecked(),
            connections=self.connections.value() or None,
            speed_limit=limit,
            user_agent=self.user_agent.text().strip() or None,
            referer=self.referer.text().strip() or None,
            cookie=self.cookie.text().strip() or None,
            proxy=self.proxy.text().strip() or None,
            note=self.note.text().strip() or None,
        )
        self.reload()

    def delete_current(self) -> None:
        profile = self.selected()
        if profile is None or profile.id is None:
            return
        self.db.delete_profile(profile.id)
        self.clear_form()
        self.reload()
