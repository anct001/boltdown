"""Site Grabber window: scan a page, pick files, send them to a queue.

The crawl itself runs on the engine's event loop (`Engine.run_coroutine`), so
the window never blocks and no second loop is needed. Results come back on the
loop thread and are handed to the GUI thread through a queued signal, the same
rule the rest of the application follows.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core.http_client import RequestSpec, build_client
from ..grabber.crawler import CrawlOptions, CrawlResult, FoundFile, crawl
from ..storage.settings import Settings
from ..util.log import get_logger
from .controller import Controller
from .i18n import tr

log = get_logger(__name__)

FILE_ROLE = Qt.ItemDataRole.UserRole + 1

PRESETS: list[tuple[str, str]] = [
    ("Everything", ""),
    ("Images", "jpg, jpeg, png, gif, webp, bmp, svg"),
    ("Video", "mp4, mkv, webm, avi, mov, m4v, flv"),
    ("Audio", "mp3, m4a, flac, wav, ogg, opus"),
    ("Archives", "zip, rar, 7z, tar, gz, xz"),
    ("Documents", "pdf, doc, docx, xls, xlsx, ppt, pptx, txt, epub"),
]


def parse_extensions(text: str) -> tuple[str, ...]:
    parts = [p.strip().lstrip(".").lower() for p in text.replace(";", ",").split(",")]
    return tuple(p for p in parts if p)


class GrabberDialog(QDialog):
    """Scan a site and queue up whatever it found."""

    #: internal - carries the crawl result from the engine thread
    _finished = Signal(object)

    def __init__(self, controller: Controller, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.setWindowTitle(tr("Site Grabber"))
        self.setMinimumSize(720, 520)
        self._future = None

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com/gallery/")
        self.depth = QSpinBox()
        self.depth.setRange(0, 5)
        self.depth.setValue(1)
        self.depth.setToolTip(tr("0 = only this page"))
        self.preset = QComboBox()
        for label, extensions in PRESETS:
            self.preset.addItem(tr(label), extensions)
        self.preset.currentIndexChanged.connect(self._apply_preset)
        self.extensions = QLineEdit()
        self.extensions.setPlaceholderText(tr("jpg, png, mp4 (empty = every file)"))
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText(tr("regular expression, optional"))
        self.exclude = QLineEdit()
        self.exclude.setPlaceholderText(tr("regular expression, optional"))
        self.max_pages = QSpinBox()
        self.max_pages.setRange(1, 2000)
        self.max_pages.setValue(50)
        self.same_host = QCheckBox(tr("Stay on the same host"))
        self.same_host.setChecked(True)

        form = QFormLayout()
        form.addRow(tr("Address:"), self.url)
        form.addRow(tr("Depth:"), self.depth)
        form.addRow(tr("Files:"), self.preset)
        form.addRow(tr("Extensions:"), self.extensions)
        form.addRow(tr("URL must match:"), self.pattern)
        form.addRow(tr("URL must not match:"), self.exclude)
        form.addRow(tr("Page limit:"), self.max_pages)
        form.addRow("", self.same_host)

        self.results = QTreeWidget()
        self.results.setHeaderLabels([tr("Name"), tr("Type"), "URL"])
        self.results.setRootIsDecorated(False)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.results.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.results.itemChanged.connect(lambda *_: self._refresh_counts())

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.status = QLabel("")

        self.scan_button = QPushButton(tr("Scan"))
        self.scan_button.clicked.connect(self.scan)
        check_all = QPushButton(tr("Select all"))
        check_all.clicked.connect(lambda: self._set_all(True))
        check_none = QPushButton(tr("Select none"))
        check_none.clicked.connect(lambda: self._set_all(False))

        self.queue_combo = QComboBox()
        self.dir_edit = QLineEdit(str(settings.download_dir))
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)

        target = QHBoxLayout()
        target.addWidget(QLabel(tr("Queue:")))
        target.addWidget(self.queue_combo, 1)
        target.addWidget(QLabel(tr("Save to:")))
        target.addWidget(self.dir_edit, 2)
        target.addWidget(browse)

        tools = QHBoxLayout()
        tools.addWidget(self.scan_button)
        tools.addWidget(check_all)
        tools.addWidget(check_none)
        tools.addStretch(1)
        tools.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.add_button = buttons.addButton(
            tr("Add selected"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_selected)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(tools)
        layout.addWidget(self.progress)
        layout.addWidget(self.results, 1)
        layout.addLayout(target)
        layout.addWidget(buttons)

        self._finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self.reload_queues()
        controller.queuesChanged.connect(self.reload_queues)

    # ------------------------------------------------------------------ setup

    def reload_queues(self) -> None:
        current = self.queue_combo.currentData()
        self.queue_combo.clear()
        self.queue_combo.addItem(tr("No queue"), None)
        for info in self.controller.queues():
            self.queue_combo.addItem(info.name, info.id)
        index = self.queue_combo.findData(current)
        self.queue_combo.setCurrentIndex(max(0, index))

    def _apply_preset(self, index: int) -> None:
        self.extensions.setText(self.preset.itemData(index) or "")

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Save to:"), self.dir_edit.text()
        )
        if chosen:
            self.dir_edit.setText(chosen)

    def options(self) -> CrawlOptions:
        return CrawlOptions(
            depth=self.depth.value(),
            max_pages=self.max_pages.value(),
            same_host=self.same_host.isChecked(),
            extensions=parse_extensions(self.extensions.text()),
            pattern=self.pattern.text().strip() or None,
            exclude=self.exclude.text().strip() or None,
        )

    # ------------------------------------------------------------------- scan

    def scan(self) -> None:
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self, tr("Site Grabber"), tr("That does not look like an http(s) URL.")
            )
            return
        try:
            options = self.options()
        except Exception as exc:  # noqa: BLE001 - a bad regex is user input
            QMessageBox.warning(self, tr("Site Grabber"), str(exc))
            return

        self.results.clear()
        self.add_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.progress.setVisible(True)
        self.status.setText(tr("Scanning..."))
        self._future = self.controller.engine.run_coroutine(self._crawl(url, options))
        self._future.add_done_callback(lambda fut: self._finished.emit(fut))

    async def _crawl(self, url: str, options: CrawlOptions) -> CrawlResult:
        spec = RequestSpec(
            url=url,
            proxy=self.settings.get("proxy"),
            user_agent=self.settings.get("user_agent"),
            verify_tls=bool(self.settings.get("verify_tls")),
        )
        async with build_client(spec) as client:
            return await crawl(client, url, options)

    def _on_finished(self, future) -> None:
        self.progress.setVisible(False)
        self.scan_button.setEnabled(True)
        try:
            result: CrawlResult = future.result()
        except Exception as exc:  # noqa: BLE001 - show whatever went wrong
            log.info("site grabber failed: %s", exc)
            self.status.setText("")
            QMessageBox.warning(self, tr("Site Grabber"), str(exc))
            return

        for found in result.files:
            entry = QTreeWidgetItem(
                [found.name, found.extension.upper() or "-", found.url]
            )
            entry.setData(0, FILE_ROLE, found)
            entry.setCheckState(0, Qt.CheckState.Checked)
            self.results.addTopLevelItem(entry)
        self.results.resizeColumnToContents(0)
        note = tr("Found {n} files on {p} pages")
        self.status.setText(
            note.replace("{n}", str(len(result.files))).replace(
                "{p}", str(result.pages_visited)
            )
            + (" - " + tr("limit reached") if result.stopped_early else "")
        )
        self._refresh_counts()

    # --------------------------------------------------------------- results

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.results.topLevelItemCount()):
            self.results.topLevelItem(row).setCheckState(0, state)

    def checked_files(self) -> list[FoundFile]:
        files = []
        for row in range(self.results.topLevelItemCount()):
            entry = self.results.topLevelItem(row)
            if entry.checkState(0) == Qt.CheckState.Checked:
                files.append(entry.data(0, FILE_ROLE))
        return files

    def _refresh_counts(self) -> None:
        self.add_button.setEnabled(bool(self.checked_files()))

    def add_selected(self) -> None:
        files = self.checked_files()
        if not files:
            return
        queue_id = self.queue_combo.currentData()
        save_dir = Path(self.dir_edit.text().strip() or self.settings.download_dir)
        for found in files:
            self.controller.add(
                found.url,
                save_dir=save_dir,
                filename=found.name or None,
                referer=found.referer,
                queue_id=queue_id,
                start_now=queue_id is None,
            )
        self.accept()
