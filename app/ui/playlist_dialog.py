"""Pick which videos of a playlist to download.

Listing runs `extract_flat`, so a two hundred video channel comes back in one
request instead of two hundred. Each chosen entry then becomes an ordinary
media download - the same runner, the same queue, the same resume.
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..media import ytdlp
from ..storage.settings import Settings
from ..util.log import get_logger
from .add_url_dialog import QUALITIES
from .controller import Controller
from .i18n import tr

log = get_logger(__name__)
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1


class PlaylistDialog(QDialog):
    """List a playlist, then queue the ticked videos."""

    _listed = Signal(object)

    def __init__(self, controller: Controller, settings: Settings,
                 url: str = "", parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.setWindowTitle(tr("Playlist"))
        self.setMinimumSize(720, 520)
        self._future: Future | None = None

        self.url = QLineEdit(url)
        self.url.setPlaceholderText("https://www.youtube.com/playlist?list=...")
        self.list_button = QPushButton(tr("List videos"))
        self.list_button.clicked.connect(self.list_videos)
        top = QHBoxLayout()
        top.addWidget(self.url, 1)
        top.addWidget(self.list_button)

        self.entries = QTreeWidget()
        self.entries.setHeaderLabels([tr("Name"), tr("Uploader")])
        self.entries.setRootIsDecorated(False)
        self.entries.setAlternatingRowColors(True)
        self.entries.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.entries.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.entries.itemChanged.connect(lambda *_: self._refresh_counts())

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.status = QLabel("")

        check_all = QPushButton(tr("Select all"))
        check_all.clicked.connect(lambda: self._set_all(True))
        check_none = QPushButton(tr("Select none"))
        check_none.clicked.connect(lambda: self._set_all(False))
        tools = QHBoxLayout()
        tools.addWidget(check_all)
        tools.addWidget(check_none)
        tools.addStretch(1)
        tools.addWidget(self.status)

        self.quality = QComboBox()
        for label, height in QUALITIES:
            self.quality.addItem(tr(label), height)
        self.quality.setCurrentIndex(max(0, self.quality.findData(settings.video_quality)))
        self.queue_combo = QComboBox()
        self.dir_edit = QLineEdit(str(settings.download_dir))

        target = QHBoxLayout()
        target.addWidget(QLabel(tr("Quality:")))
        target.addWidget(self.quality)
        target.addWidget(QLabel(tr("Queue:")))
        target.addWidget(self.queue_combo, 1)
        target.addWidget(QLabel(tr("Save to:")))
        target.addWidget(self.dir_edit, 2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.add_button = buttons.addButton(
            tr("Add selected"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add_selected)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(tools)
        layout.addWidget(self.progress)
        layout.addWidget(self.entries, 1)
        layout.addLayout(target)
        layout.addWidget(buttons)

        self._listed.connect(self._on_listed, Qt.ConnectionType.QueuedConnection)
        self.reload_queues()
        controller.queuesChanged.connect(self.reload_queues)

    # ------------------------------------------------------------------ setup

    def reload_queues(self) -> None:
        current = self.queue_combo.currentData()
        self.queue_combo.clear()
        self.queue_combo.addItem(tr("No queue"), None)
        for info in self.controller.queues():
            self.queue_combo.addItem(info.name, info.id)
        self.queue_combo.setCurrentIndex(max(0, self.queue_combo.findData(current)))

    # ----------------------------------------------------------------- listing

    def list_videos(self) -> None:
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self, tr("Playlist"), tr("That does not look like an http(s) URL.")
            )
            return
        self.entries.clear()
        self.add_button.setEnabled(False)
        self.list_button.setEnabled(False)
        self.progress.setVisible(True)
        self.status.setText(tr("Listing..."))

        options = ytdlp.build_options(
            proxy=self.settings.get("proxy"),
            user_agent=self.settings.get("user_agent"),
            verify_tls=bool(self.settings.get("verify_tls")),
        )
        self._future = self.controller.engine.run_coroutine(
            ytdlp.extract_playlist(url, options)
        )
        self._future.add_done_callback(lambda fut: self._listed.emit(fut))

    def _on_listed(self, future) -> None:
        self.progress.setVisible(False)
        self.list_button.setEnabled(True)
        try:
            playlist = future.result()
        except Exception as exc:  # noqa: BLE001 - show whatever went wrong
            log.info("playlist listing failed: %s", exc)
            self.status.setText("")
            QMessageBox.warning(self, tr("Playlist"), str(exc))
            return
        if playlist is None or not playlist.entries:
            self.status.setText(tr("That URL is a single video, not a playlist."))
            return

        for entry in playlist.entries:
            row = QTreeWidgetItem([f"{entry.index}. {entry.label}", entry.uploader])
            row.setData(0, ENTRY_ROLE, entry)
            row.setCheckState(0, Qt.CheckState.Checked)
            self.entries.addTopLevelItem(row)
        self.setWindowTitle(f"{tr('Playlist')} - {playlist.title}")
        self.status.setText(
            tr("{n} videos").replace("{n}", str(len(playlist.entries)))
        )
        self._refresh_counts()

    # ---------------------------------------------------------------- picking

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.entries.topLevelItemCount()):
            self.entries.topLevelItem(row).setCheckState(0, state)

    def checked_entries(self) -> list[ytdlp.PlaylistEntry]:
        chosen = []
        for row in range(self.entries.topLevelItemCount()):
            item = self.entries.topLevelItem(row)
            if item.checkState(0) == Qt.CheckState.Checked:
                chosen.append(item.data(0, ENTRY_ROLE))
        return chosen

    def _refresh_counts(self) -> None:
        self.add_button.setEnabled(bool(self.checked_entries()))

    def add_selected(self) -> None:
        entries = self.checked_entries()
        if not entries:
            return
        queue_id = self.queue_combo.currentData()
        save_dir = Path(self.dir_edit.text().strip() or self.settings.download_dir)
        for entry in entries:
            self.controller.add(
                entry.url,
                save_dir=save_dir,
                max_height=self.quality.currentData(),
                queue_id=queue_id,
                start_now=queue_id is None,
            )
        self.accept()
