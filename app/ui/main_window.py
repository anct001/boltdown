"""Main window: toolbar, category tree, download table."""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QTableView,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.categories import CATEGORIES, GENERAL
from ..core.schedule import PostAction
from ..core.task import TaskState
from ..media.detect import classify
from ..storage.settings import Settings
from ..util import postprocess, power
from ..util.fmt import human_speed
from . import icons, theme
from .add_url_dialog import AddUrlDialog
from .batch_dialog import BatchDialog
from . import sounds as sound_effects
from .browser_dialog import BrowserDialog
from .checksum_dialog import ChecksumDialog
from .clipboard_watch import ClipboardWatcher
from .controller import Controller, DownloadItem
from .dropbox import DropBox
from .grabber_dialog import GrabberDialog
from .history_dialog import HistoryDialog
from .i18n import tr
from .playlist_dialog import PlaylistDialog
from .profiles_dialog import SiteProfilesDialog
from .progress_dialog import ProgressDialog, _open_path
from .queue_dialog import SchedulerDialog
from .scheduler import QueueScheduler
from .settings_dialog import SettingsDialog
from .stats_dialog import StatsDialog
from .task_model import (
    COL_ADDED,
    COL_STATUS,
    DownloadFilterProxy,
    DownloadTableModel,
    ITEM_ROLE,
    ProgressDelegate,
)

FILTER_ROLE = Qt.ItemDataRole.UserRole + 10
#: seconds the user gets to call off a scheduled shutdown
POST_ACTION_DELAY = 30

_ACTION_LABELS = {
    "exit": "Downloads finished - closing Boltdown",
    "shutdown": "Downloads finished - shutting the computer down",
    "hibernate": "Downloads finished - hibernating",
    "sleep": "Downloads finished - going to sleep",
}


class MainWindow(QMainWindow):
    #: post-processing runs on a worker thread and reports back through this
    _postDone = Signal(str, str)

    def __init__(self, controller: Controller, settings: Settings) -> None:
        super().__init__()
        self.controller = controller
        self.settings = settings
        self.tray = None
        self._force_quit = False
        self._progress_dialogs: dict[int, ProgressDialog] = {}
        #: ids already handled, so one finished download is announced once
        self._finished: set[int] = set()
        #: same, for the failure sound - a retrying download must not buzz
        #: once per attempt
        self._failed: set[int] = set()
        self.sounds = sound_effects.SoundBoard(settings)

        self.setWindowTitle("Boltdown")
        self.setWindowIcon(icons.app_icon())
        self.resize(1040, 620)
        self.setAcceptDrops(True)

        self.model = DownloadTableModel(controller, self)
        self.proxy = DownloadFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self._build_actions()
        self._build_toolbar()
        self._build_menu()
        self._build_body()
        self._build_statusbar()

        self._ticker = QTimer(self)
        self._ticker.timeout.connect(self._refresh_status)
        self._ticker.start(500)

        self.scheduler = QueueScheduler(controller, controller.db, self)
        self.scheduler.actionRequested.connect(self._on_post_action)
        self.scheduler.start()

        self.clipboard = ClipboardWatcher(settings, self)
        self.clipboard.linkCaptured.connect(self._on_clipboard_link)
        if settings.get("clipboard_monitor"):
            self.clipboard.start()
        self.dropbox: DropBox | None = None
        if settings.get("dropbox_visible"):
            self.toggle_dropbox(True)

        self._postDone.connect(self._notify, Qt.QueuedConnection)
        controller.itemChanged.connect(self._on_item_changed)
        controller.itemAdded.connect(lambda _item: self.sounds.play("added"))
        controller.queueFinished.connect(self._on_queue_finished)
        controller.queuesChanged.connect(self._rebuild_queue_nodes)
        self._update_action_states()

    # ------------------------------------------------------------------ build

    def _build_actions(self) -> None:
        self.action_add = QAction(icons.add_icon(), tr("Add URL"), self)
        self.action_add.setShortcut(QKeySequence("Ctrl+N"))
        self.action_add.triggered.connect(self.add_url)

        self.action_resume = QAction(icons.download_icon(), tr("Resume"), self)
        self.action_resume.triggered.connect(self.resume_selected)

        self.action_pause = QAction(icons.pause_icon(), tr("Pause"), self)
        self.action_pause.triggered.connect(self.pause_selected)

        self.action_pause_all = QAction(icons.stop_icon(), tr("Pause All"), self)
        self.action_pause_all.triggered.connect(self.controller.pause_all)

        self.action_resume_all = QAction(icons.resume_all_icon(), tr("Resume All"), self)
        self.action_resume_all.triggered.connect(self.controller.resume_all)

        self.action_delete = QAction(icons.delete_icon(), tr("Delete"), self)
        self.action_delete.setShortcut(QKeySequence.StandardKey.Delete)
        self.action_delete.triggered.connect(self.delete_selected)

        self.action_options = QAction(icons.settings_icon(), tr("Options"), self)
        self.action_options.triggered.connect(self.open_settings)

        self.action_scheduler = QAction(icons.clock_icon(), tr("Scheduler"), self)
        self.action_scheduler.triggered.connect(self.open_scheduler)

        self.action_grabber = QAction(icons.globe_icon(), tr("Site Grabber"), self)
        self.action_grabber.triggered.connect(self.open_grabber)

        self.action_batch = QAction(icons.batch_icon(), tr("Add many URLs"), self)
        self.action_batch.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.action_batch.triggered.connect(self.open_batch)

        self.action_playlist = QAction(icons.batch_icon(), tr("Playlist"), self)
        self.action_playlist.triggered.connect(self.open_playlist)

        self.action_history = QAction(icons.history_icon(), tr("History"), self)
        self.action_history.triggered.connect(self.open_history)

        self.action_stats = QAction(icons.clock_icon(), tr("Statistics"), self)
        self.action_stats.triggered.connect(self.open_stats)

        self.action_profiles = QAction(icons.globe_icon(), tr("Site rules"), self)
        self.action_profiles.triggered.connect(self.open_profiles)

        self.action_browser = QAction(
            icons.link_icon(), tr("Browser integration"), self
        )
        self.action_browser.triggered.connect(self.open_browser_setup)

        self.action_dropbox = QAction(icons.dropbox_icon(), tr("Drop box"), self)
        self.action_dropbox.setCheckable(True)
        self.action_dropbox.setChecked(bool(self.settings.get("dropbox_visible")))
        self.action_dropbox.toggled.connect(self.toggle_dropbox)

        self.action_clipboard = QAction(icons.clipboard_icon(), tr("Watch the clipboard"), self)
        self.action_clipboard.setCheckable(True)
        self.action_clipboard.setChecked(bool(self.settings.get("clipboard_monitor")))
        self.action_clipboard.toggled.connect(self.toggle_clipboard)

        self.action_paste = QAction(icons.link_icon(), tr("Paste URL from clipboard"), self)
        self.action_paste.setShortcut(QKeySequence.StandardKey.Paste)
        self.action_paste.triggered.connect(self.paste_url)
        self.addAction(self.action_paste)

        self.action_exit = QAction(icons.exit_icon(), tr("Exit"), self)
        self.action_exit.triggered.connect(self.quit_application)

    def _build_toolbar(self) -> None:
        bar = QToolBar("main", self)
        bar.setMovable(False)
        bar.setIconSize(bar.iconSize() * 1.1)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        for action in (
            self.action_add, self.action_batch, self.action_resume, self.action_pause,
            self.action_pause_all, self.action_delete,
        ):
            bar.addAction(action)
        bar.addSeparator()
        bar.addAction(self.action_scheduler)
        bar.addAction(self.action_grabber)
        bar.addAction(self.action_options)
        self.addToolBar(bar)

    def _build_menu(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu(tr("File"))
        file_menu.addAction(self.action_add)
        file_menu.addAction(self.action_batch)
        file_menu.addAction(self.action_playlist)
        file_menu.addAction(self.action_paste)
        file_menu.addSeparator()
        file_menu.addAction(self.action_history)
        file_menu.addAction(self.action_stats)
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)

        downloads = menu.addMenu(tr("Downloads"))
        for action in (
            self.action_resume, self.action_pause, self.action_resume_all,
            self.action_pause_all, self.action_delete,
        ):
            downloads.addAction(action)
        downloads.addSeparator()
        downloads.addAction(self.action_scheduler)
        downloads.addAction(self.action_grabber)

        options = menu.addMenu(tr("Options"))
        options.addAction(self.action_browser)
        options.addAction(self.action_profiles)
        options.addSeparator()
        options.addAction(self.action_clipboard)
        options.addAction(self.action_dropbox)
        options.addSeparator()
        options.addAction(self.action_options)

        help_menu = menu.addMenu(tr("Help"))
        about = QAction(tr("About"), self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _build_body(self) -> None:
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(170)
        self.tree.setMaximumWidth(280)
        for label, kind, value in (
            ("All downloads", "all", ""),
            ("Unfinished", "unfinished", ""),
            ("Finished", "finished", ""),
        ):
            node = QTreeWidgetItem([tr(label)])
            node.setData(0, FILTER_ROLE, (kind, value))
            node.setIcon(0, icons.filter_icon(kind))
            self.tree.addTopLevelItem(node)
        categories = QTreeWidgetItem([tr("Downloads")])
        categories.setData(0, FILTER_ROLE, ("all", ""))
        categories.setIcon(0, icons.folder_icon())
        for name in (*CATEGORIES.keys(), GENERAL):
            child = QTreeWidgetItem([tr(name)])
            child.setData(0, FILTER_ROLE, ("category", name))
            child.setIcon(0, icons.category_icon(name))
            categories.addChild(child)
        self.tree.addTopLevelItem(categories)
        categories.setExpanded(True)

        self.queue_root = QTreeWidgetItem([tr("Queues")])
        self.queue_root.setData(0, FILTER_ROLE, ("all", ""))
        self.queue_root.setIcon(0, icons.queue_icon())
        self.tree.addTopLevelItem(self.queue_root)
        self._rebuild_queue_nodes()

        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.tree.currentItemChanged.connect(self._on_tree_selection)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.proxy.set_search)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setItemDelegateForColumn(COL_STATUS, ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._update_action_states)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, self.model.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        # The progress bar needs room the header text alone would not give it.
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_STATUS, 150)
        self.table.sortByColumn(COL_ADDED, Qt.SortOrder.AscendingOrder)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.search)
        right_layout.addWidget(self.table, 1)

        splitter = QSplitter()
        splitter.addWidget(self.tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        self.status_speed = QLabel()
        self.status_count = QLabel()
        self.statusBar().addPermanentWidget(self.status_count)
        self.statusBar().addPermanentWidget(self.status_speed)
        self._refresh_status()

    # ---------------------------------------------------------------- actions

    def add_url(self, url: str = "") -> None:
        dialog = AddUrlDialog(self.settings, url=url or "", parent=self)
        if dialog.exec() != AddUrlDialog.DialogCode.Accepted:
            return
        options = dialog.options()
        if not self._confirm_duplicate(options["url"]):
            return
        self.controller.add(**options)

    def _confirm_duplicate(self, url: str) -> bool:
        """Warn when this URL is already in the list or the history."""
        existing = next((i for i in self.controller.items() if i.url == url), None)
        if existing is not None:
            answer = QMessageBox.question(
                self, tr("Add a download"),
                "\n\n".join([
                    tr("This URL is already in the list."),
                    existing.filename,
                    tr("Add it again?"),
                ]),
            )
            return answer == QMessageBox.StandardButton.Yes
        past = self.controller.db.list_history(url, limit=5)
        match = next((row for row in past if row["url"] == url), None)
        if match is None:
            return True
        when = datetime.fromtimestamp(match["finished_at"] or 0).strftime("%d/%m/%Y")
        answer = QMessageBox.question(
            self, tr("Add a download"),
            "\n\n".join([
                tr("You downloaded this before."),
                f"{match['filename']} - {when}",
                tr("Add it again?"),
            ]),
        )
        return answer == QMessageBox.StandardButton.Yes

    def paste_url(self) -> None:
        text = (QGuiApplication.clipboard().text() or "").strip()
        if text.startswith(("http://", "https://")):
            self.add_url(text)

    def resume_selected(self) -> None:
        for item in self._selected_items():
            self.controller.start_item(item.db_id)

    def pause_selected(self) -> None:
        for item in self._selected_items():
            self.controller.pause_item(item.db_id)

    def delete_selected(self) -> None:
        items = self._selected_items()
        if not items:
            return
        box = QMessageBox(self)
        box.setWindowTitle(tr("Delete"))
        box.setText(tr("Delete the selected downloads?"))
        keep = box.addButton(tr("Remove from list"), QMessageBox.ButtonRole.AcceptRole)
        wipe = box.addButton(tr("Delete file too"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (keep, wipe):
            return
        for item in items:
            self.controller.remove(item.db_id, delete_file=clicked is wipe)

    def open_scheduler(self) -> None:
        SchedulerDialog(self.controller, self.controller.db, self).exec()

    def open_grabber(self) -> None:
        GrabberDialog(self.controller, self.settings, self).exec()

    def open_batch(self) -> None:
        BatchDialog(self.controller, self.settings, self).exec()

    def open_playlist(self, url: str = "") -> None:
        PlaylistDialog(self.controller, self.settings, url or "", self).exec()

    def open_history(self) -> None:
        HistoryDialog(self.controller, self.controller.db, self).exec()

    def open_stats(self) -> None:
        StatsDialog(self.controller.db, self).exec()

    def open_profiles(self) -> None:
        SiteProfilesDialog(self.controller, self.controller.db, self).exec()

    def verify_checksum(self, item: DownloadItem) -> None:
        if not item.path.exists():
            QMessageBox.information(self, tr("Open"), tr("The file no longer exists."))
            return
        ChecksumDialog(item.path, self).exec()

    # ------------------------------------------------------- clipboard & box

    def toggle_clipboard(self, enabled: bool) -> None:
        self.clipboard.set_enabled(enabled)
        if enabled:
            self._notify(tr("Watch the clipboard"), tr("Copy a link to download it"))

    def _on_clipboard_link(self, url: str) -> None:
        options = {"url": url}
        if self.settings.get("clipboard_ask"):
            self._prefilled_dialog(options)
        else:
            self.controller.add(**options)
            self._notify(tr("Add URL"), url)

    def toggle_dropbox(self, visible: bool) -> None:
        if visible:
            if self.dropbox is None:
                self.dropbox = DropBox(self.settings, self)
                self.dropbox.urlsDropped.connect(self._on_dropped_urls)
                self.dropbox.closed.connect(
                    lambda: self.action_dropbox.setChecked(False)
                )
            self.dropbox.show_box()
        elif self.dropbox is not None:
            self.dropbox.hide_box()

    def _on_dropped_urls(self, urls: list) -> None:
        for url in urls:
            if self.settings.get("ask_before_download"):
                self.add_url(url)
            else:
                self.controller.add(url)
                self._notify(tr("Add URL"), url)

    def open_browser_setup(self) -> None:
        BrowserDialog(self.settings, self).exec()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        self.controller.engine.set_speed_limit(self.settings.speed_limit)
        self.controller.engine.max_concurrent = self.settings.max_concurrent
        self.refresh_icons()
        self.action_clipboard.setChecked(bool(self.settings.get("clipboard_monitor")))

    def refresh_icons(self) -> None:
        """Redraw every glyph in the new theme's accent colour.

        Icons are painted, not loaded, so a theme change means repainting them
        - otherwise a Cyberpunk window would keep the blue arrows of Dark.
        """
        for action, icon in (
            (self.action_add, icons.add_icon()),
            (self.action_batch, icons.batch_icon()),
            (self.action_resume, icons.download_icon()),
            (self.action_pause, icons.pause_icon()),
            (self.action_pause_all, icons.stop_icon()),
            (self.action_resume_all, icons.resume_all_icon()),
            (self.action_delete, icons.delete_icon()),
            (self.action_options, icons.settings_icon()),
            (self.action_scheduler, icons.clock_icon()),
            (self.action_grabber, icons.globe_icon()),
            (self.action_browser, icons.link_icon()),
            (self.action_history, icons.history_icon()),
            (self.action_dropbox, icons.dropbox_icon()),
            (self.action_clipboard, icons.clipboard_icon()),
            (self.action_paste, icons.link_icon()),
            (self.action_exit, icons.exit_icon()),
        ):
            action.setIcon(icon)

        for row, kind in enumerate(("all", "unfinished", "finished")):
            self.tree.topLevelItem(row).setIcon(0, icons.filter_icon(kind))
        categories = self.tree.topLevelItem(3)
        categories.setIcon(0, icons.folder_icon())
        for index in range(categories.childCount()):
            child = categories.child(index)
            payload = child.data(0, FILTER_ROLE) or ("", "")
            child.setIcon(0, icons.category_icon(payload[1]))
        self.queue_root.setIcon(0, icons.queue_icon())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The blurred backdrop can only be asked for once the window exists.
        theme.apply_backdrop(self)

    def show_properties(self, item: DownloadItem) -> None:
        dialog = self._progress_dialogs.get(item.db_id)
        if dialog is None:
            dialog = ProgressDialog(self.controller, item, self)
            dialog.finished.connect(
                lambda _result, db_id=item.db_id: self._progress_dialogs.pop(db_id, None)
            )
            self._progress_dialogs[item.db_id] = dialog
        dialog.show()
        dialog.raise_()

    def quit_application(self) -> None:
        self._force_quit = True
        self.close()

    # ------------------------------------------------- browser / second instance

    def handle_ipc_download(self, message: dict) -> None:
        """A link captured by the extension, or handed over by another instance."""
        url = (message.get("url") or "").strip()
        if not url:
            return
        options = {
            "url": url,
            "filename": message.get("filename") or None,
            "referer": message.get("referer") or None,
            "cookie": message.get("cookie") or None,
            "user_agent": message.get("user_agent") or None,
        }
        media = classify(url).is_media
        if media:
            # A stream has no Content-Disposition name to inherit, and the
            # browser's guess is the playlist file name.
            options["filename"] = None
            options["max_height"] = self.settings.video_quality
        if self.settings.get("ask_before_download"):
            self._prefilled_dialog(options)
        else:
            self.controller.add(**options)
            self._notify(tr("Downloading video") if media else tr("Add URL"), url)

    def remote_snapshot(self) -> list[dict]:
        """What `boltdown-cli --remote-list` prints."""
        return [
            {
                "id": item.db_id,
                "name": item.filename,
                "state": item.state.value,
                "size": item.size,
                "downloaded": item.downloaded,
                "speed": item.speed,
                "url": item.url,
            }
            for item in self.controller.items()
        ]

    def remote_control(self, action: str, db_id: int | None) -> bool:
        """Pause or resume one download, or everything when `db_id` is None."""
        if action == "pause":
            if db_id is None:
                self.controller.pause_all()
            else:
                self.controller.pause_item(db_id)
            return True
        if action == "resume":
            if db_id is None:
                self.controller.resume_all()
            else:
                self.controller.start_item(db_id)
            return True
        return False

    def handle_ipc_show(self, message: dict) -> None:
        for url in message.get("urls", []) or []:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                self.handle_ipc_download({"url": url})
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _prefilled_dialog(self, options: dict) -> None:
        dialog = AddUrlDialog(self.settings, url=options["url"], parent=self)
        if options.get("filename"):
            dialog.name_edit.setText(options["filename"])
        if any(options.get(k) for k in ("referer", "cookie", "user_agent")):
            dialog.advanced.setChecked(True)
            dialog.referer.setText(options.get("referer") or "")
            dialog.cookie.setText(options.get("cookie") or "")
            dialog.user_agent.setText(options.get("user_agent") or "")
        if options.get("max_height") is not None:
            index = dialog.quality.findData(options["max_height"])
            if index >= 0:
                dialog.quality.setCurrentIndex(index)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if dialog.exec() == AddUrlDialog.DialogCode.Accepted:
            self.controller.add(**dialog.options())

    def _notify(self, title: str, body: str) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, body, icons.app_icon())
        else:
            self.statusBar().showMessage(f"{title}: {body}", 5000)

    # --------------------------------------------------------------- helpers

    def _selected_items(self) -> list[DownloadItem]:
        rows = self.table.selectionModel().selectedRows()
        items = []
        for index in rows:
            item = index.data(ITEM_ROLE)
            if item is not None:
                items.append(item)
        return items

    def _rebuild_queue_nodes(self) -> None:
        """Mirror the queue list into the tree, keeping the current filter."""
        self.queue_root.takeChildren()
        queues = self.controller.queues()
        for info in queues:
            label = f"{info.name} ({len(self.controller.queue_items(info.id))})"
            if info.running:
                label += " " + tr("running")
            node = QTreeWidgetItem([label])
            node.setData(0, FILTER_ROLE, ("queue", str(info.id)))
            self.queue_root.addChild(node)
        self.queue_root.setHidden(not queues)
        self.queue_root.setExpanded(True)

    def _on_tree_selection(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        payload = current.data(0, FILTER_ROLE)
        if payload:
            self.proxy.set_filter(*payload)

    def _on_double_click(self, index) -> None:
        item = index.data(ITEM_ROLE)
        if item is None:
            return
        if item.state is TaskState.COMPLETED:
            self._open_file(item)
        else:
            self.show_properties(item)

    def _on_item_changed(self, item: DownloadItem) -> None:
        self._update_action_states()
        if item.state is TaskState.COMPLETED and item.db_id not in self._finished:
            self._finished.add(item.db_id)
            self.sounds.play("completed")
            self._on_download_finished(item)
        elif item.state is TaskState.ERROR and item.db_id not in self._failed:
            # Once per download, not once per retry.
            self._failed.add(item.db_id)
            self.sounds.play("error")

    def _on_queue_finished(self, queue_id: int) -> None:
        self.sounds.play("queue_done")

    def _on_download_finished(self, item: DownloadItem) -> None:
        """Notify, then run whatever post-processing is switched on."""
        if self.settings.get("notify_on_finish"):
            self._notify(tr("Download finished"), item.filename)
        wants_extract = (
            self.settings.get("auto_extract") and postprocess.is_archive(item.path)
        )
        wants_scan = self.settings.get("scan_with_defender")
        if not (wants_extract or wants_scan):
            return

        path = item.path
        name = item.filename

        def work() -> None:
            if wants_scan:
                result = postprocess.scan(path)
                if not result.ok:
                    self._postDone.emit(tr("Defender"), f"{name}: {result.detail}")
                    return
            if wants_extract:
                result = postprocess.extract(path)
                message = result.detail if result.ok else f"{name}: {result.detail}"
                self._postDone.emit(
                    tr("Unpacked") if result.ok else tr("Could not unpack"), message
                )

        threading.Thread(target=work, name=f"post-{item.db_id}", daemon=True).start()

    def _update_action_states(self, *_args) -> None:
        items = self._selected_items()
        live = any(i.is_live for i in items)
        stopped = any(
            i.state in (TaskState.PAUSED, TaskState.ERROR, TaskState.QUEUED)
            for i in items
        )
        self.action_pause.setEnabled(live)
        self.action_resume.setEnabled(stopped and not live)
        self.action_delete.setEnabled(bool(items))

    def _refresh_status(self) -> None:
        speed = self.controller.total_speed()
        active = self.controller.active_count()
        self.status_speed.setText(human_speed(speed) if active else "")
        self.status_count.setText(f"{tr('Downloading')}: {active}")
        if self.tray is not None:
            self.tray.update_tooltip(speed, active)

    def _open_file(self, item: DownloadItem) -> None:
        path = item.path
        if not path.exists():
            QMessageBox.information(
                self, tr("Open"), tr("The file no longer exists.")
            )
            return
        _open_path(str(path))

    def _open_folder(self, item: DownloadItem) -> None:
        path = item.path
        if path.exists() and sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            _open_path(str(Path(item.save_path)))

    def _show_context_menu(self, position) -> None:
        items = self._selected_items()
        if not items:
            return
        item = items[0]
        menu = QMenu(self)
        if item.state is TaskState.COMPLETED:
            menu.addAction(icons.open_file_icon(), tr("Open"), lambda: self._open_file(item))
        menu.addAction(icons.folder_icon(), tr("Open Folder"), lambda: self._open_folder(item))
        menu.addSeparator()
        if item.is_live:
            menu.addAction(icons.pause_icon(), tr("Pause"), self.pause_selected)
        else:
            menu.addAction(icons.download_icon(), tr("Resume"), self.resume_selected)
        menu.addAction(icons.refresh_icon(), tr("Redownload"),
                       lambda: self.controller.redownload(item.db_id))
        self._add_queue_menu(menu, items)
        menu.addSeparator()
        menu.addAction(icons.link_icon(), tr("Copy URL"), lambda: self._copy_url(item))
        if item.state is TaskState.COMPLETED:
            menu.addAction(
                icons.shield_icon(), tr("Verify checksum"),
                lambda: self.verify_checksum(item),
            )
        menu.addAction(icons.info_icon(), tr("Properties"), lambda: self.show_properties(item))
        menu.addSeparator()
        menu.addAction(icons.delete_icon(), tr("Delete"), self.delete_selected)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _copy_url(self, item: DownloadItem) -> None:
        # Tell the watcher first: copying a link from the list must not make
        # the app offer to download what it already has.
        self.clipboard.ignore(item.url)
        QGuiApplication.clipboard().setText(item.url)

    def _add_queue_menu(self, menu: QMenu, items: list[DownloadItem]) -> None:
        queues = self.controller.queues()
        if not queues:
            return
        submenu = menu.addMenu(icons.queue_icon(), tr("Move to queue"))
        for info in queues:
            action = submenu.addAction(info.name)
            action.setCheckable(True)
            action.setChecked(all(i.queue_id == info.id for i in items))
            action.triggered.connect(
                lambda _checked=False, qid=info.id: self._assign_queue(items, qid)
            )
        submenu.addSeparator()
        submenu.addAction(
            tr("No queue"), lambda: self._assign_queue(items, None)
        )

    def _assign_queue(self, items: list[DownloadItem], queue_id: int | None) -> None:
        for item in items:
            self.controller.assign_queue(item.db_id, queue_id)
        self._rebuild_queue_nodes()

    # ------------------------------------------------------ scheduled actions

    def _on_post_action(self, action_value: str, _queue_id: int) -> None:
        try:
            action = PostAction(action_value)
        except ValueError:  # pragma: no cover - the DB only holds known values
            return
        if action is PostAction.NONE:
            return
        if not self._confirm_post_action(action):
            return
        self._run_post_action(action)

    def _confirm_post_action(self, action: PostAction) -> bool:
        """Countdown the user can call off - the whole point of a scheduler."""
        label = tr(_ACTION_LABELS.get(action.value, action.value))
        box = QMessageBox(self)
        box.setWindowTitle(tr("Scheduler"))
        box.setIcon(QMessageBox.Icon.Warning)
        proceed = box.addButton(tr("Do it now"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(proceed)

        remaining = POST_ACTION_DELAY
        countdown = QTimer(box)
        countdown.setInterval(1000)

        def render() -> None:
            box.setText(f"{label} ({remaining}s)")

        def tick() -> None:
            nonlocal remaining
            remaining -= 1
            render()
            if remaining <= 0:
                countdown.stop()
                box.done(QMessageBox.ButtonRole.AcceptRole.value)

        countdown.timeout.connect(tick)
        render()
        countdown.start()
        box.exec()
        countdown.stop()
        return box.clickedButton() is proceed or remaining <= 0

    def _run_post_action(self, action: PostAction) -> bool:
        # The countdown already happened in the dialog, so go immediately.
        return power.apply(action, delay=0, on_exit=self.quit_application)

    def _about(self) -> None:
        QMessageBox.about(
            self, tr("About"),
            "Boltdown 0.1\n\n"
            f"{tr('Downloads')}: multi-segment HTTP engine with resume.\n"
            "Python + PySide6.",
        )

    # ----------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event) -> None:
        data = event.mimeData()
        if data.hasUrls() or data.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        data = event.mimeData()
        urls = [u.toString() for u in data.urls()] if data.hasUrls() else []
        if not urls and data.hasText():
            urls = [line.strip() for line in data.text().splitlines() if line.strip()]
        for url in urls:
            if url.startswith(("http://", "https://")):
                if self.settings.get("ask_before_download"):
                    self.add_url(url)
                else:
                    self.controller.add(url)
        event.acceptProposedAction()

    # ------------------------------------------------------------- lifecycle

    def closeEvent(self, event) -> None:
        if (
            not self._force_quit
            and self.tray is not None
            and self.tray.isVisible()
            and self.settings.get("minimize_to_tray")
        ):
            event.ignore()
            self.hide()
            return
        self._ticker.stop()
        self.scheduler.stop()
        self.clipboard.stop()
        if self.dropbox is not None:
            self.dropbox.save_position()
            self.dropbox.close()
        self.controller.shutdown()
        event.accept()
        QApplication.instance().quit()
