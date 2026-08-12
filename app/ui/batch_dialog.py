"""Add many downloads at once, from a pasted list or a numbered pattern."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..storage.settings import Settings
from ..util.patterns import MAX_URLS, PatternError, parse
from .controller import Controller
from .i18n import tr

PLACEHOLDER = (
    "https://example.com/file1.zip\n"
    "https://example.com/file2.zip\n"
    "https://example.com/photos/img[001-120].jpg"
)
PREVIEW_LIMIT = 200


class BatchDialog(QDialog):
    def __init__(self, controller: Controller, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.setWindowTitle(tr("Add many URLs"))
        self.setMinimumSize(640, 520)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(PLACEHOLDER)
        self.text.textChanged.connect(self._refresh)

        self.preview = QListWidget()
        self.summary = QLabel("")

        self.dir_edit = QLineEdit(str(settings.download_dir))
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        self.connections = QSpinBox()
        self.connections.setRange(1, 32)
        self.connections.setValue(settings.connections)
        self.queue_combo = QComboBox()

        target = QHBoxLayout()
        target.addWidget(QLabel(tr("Save to:")))
        target.addWidget(self.dir_edit, 2)
        target.addWidget(browse)
        target.addWidget(QLabel(tr("Connections:")))
        target.addWidget(self.connections)
        target.addWidget(QLabel(tr("Queue:")))
        target.addWidget(self.queue_combo, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.add_button = buttons.addButton(
            tr("Add all"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_all)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("One URL per line. [001-120] and [a-z] expand.")))
        layout.addWidget(self.text, 2)
        layout.addWidget(self.summary)
        layout.addWidget(self.preview, 3)
        layout.addLayout(target)
        layout.addWidget(buttons)

        self.reload_queues()
        controller.queuesChanged.connect(self.reload_queues)

    # ------------------------------------------------------------------ state

    def reload_queues(self) -> None:
        current = self.queue_combo.currentData()
        self.queue_combo.clear()
        self.queue_combo.addItem(tr("No queue"), None)
        for info in self.controller.queues():
            self.queue_combo.addItem(info.name, info.id)
        self.queue_combo.setCurrentIndex(max(0, self.queue_combo.findData(current)))

    def urls(self) -> list[str]:
        return parse(self.text.toPlainText())

    def _refresh(self) -> None:
        self.preview.clear()
        try:
            urls = self.urls()
        except PatternError as exc:
            self.summary.setText(str(exc))
            self.add_button.setEnabled(False)
            return
        # Showing ten thousand rows would be slower than the download itself.
        self.preview.addItems(urls[:PREVIEW_LIMIT])
        if len(urls) > PREVIEW_LIMIT:
            self.preview.addItem(
                tr("... and {n} more").replace("{n}", str(len(urls) - PREVIEW_LIMIT))
            )
        self.summary.setText(
            tr("{n} URLs ready").replace("{n}", str(len(urls))) if urls else ""
        )
        self.add_button.setEnabled(bool(urls))

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Save to:"), self.dir_edit.text()
        )
        if chosen:
            self.dir_edit.setText(chosen)

    # --------------------------------------------------------------- action

    def add_all(self) -> None:
        try:
            urls = self.urls()
        except PatternError as exc:
            QMessageBox.warning(self, tr("Add many URLs"), str(exc))
            return
        if not urls:
            return
        if len(urls) > MAX_URLS:  # pragma: no cover - parse already refuses
            return
        queue_id = self.queue_combo.currentData()
        save_dir = Path(self.dir_edit.text().strip() or self.settings.download_dir)
        for url in urls:
            self.controller.add(
                url,
                save_dir=save_dir,
                connections=self.connections.value(),
                queue_id=queue_id,
                start_now=queue_id is None,
            )
        self.accept()
