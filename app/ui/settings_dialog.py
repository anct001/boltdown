"""Options dialog."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..media.ffmpeg import find_ffmpeg
from ..media.ytdlp import version as ytdlp_version
from ..storage.settings import Settings
from ..util import autostart
from ..util.fmt import human_size, parse_size
from . import theme
from .add_url_dialog import QUALITIES
from .i18n import LANGUAGES, tr


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(tr("Settings"))
        self.setMinimumWidth(520)

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), tr("General settings"))
        tabs.addTab(self._connection_tab(), tr("Connection"))
        tabs.addTab(self._video_tab(), tr("Video"))
        tabs.addTab(self._clipboard_tab(), tr("Clipboard"))
        tabs.addTab(self._browser_tab(), tr("Browser integration"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ tabs

    def _general_tab(self) -> QWidget:
        page = QWidget()
        self.dir_edit = QLineEdit(str(self.settings.download_dir))
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)

        self.use_categories = QCheckBox(tr("Sort files into category folders"))
        self.use_categories.setChecked(bool(self.settings.get("use_categories")))
        self.minimize_to_tray = QCheckBox(tr("Minimize to tray instead of closing"))
        self.minimize_to_tray.setChecked(bool(self.settings.get("minimize_to_tray")))
        self.ask_before = QCheckBox(tr("Ask before every download"))
        self.ask_before.setChecked(bool(self.settings.get("ask_before_download")))
        self.autostart = QCheckBox(tr("Start with Windows (in the tray)"))
        self.autostart.setChecked(bool(self.settings.get("start_with_windows")))
        self.autostart.setEnabled(sys.platform == "win32")

        self.language = QComboBox()
        for code, label in LANGUAGES.items():
            self.language.addItem(label, code)
        index = self.language.findData(self.settings.language)
        self.language.setCurrentIndex(max(0, index))

        self.theme = QComboBox()
        for label, value in (
            ("Follow Windows", theme.AUTO),
            ("Light", theme.LIGHT_NAME),
            ("Dark", theme.DARK_NAME),
        ):
            self.theme.addItem(tr(label), value)
        self.theme.setCurrentIndex(
            max(0, self.theme.findData(self.settings.get("theme") or theme.AUTO))
        )

        form = QFormLayout(page)
        form.addRow(tr("Downloads folder:"), row)
        form.addRow("", self.use_categories)
        form.addRow("", self.minimize_to_tray)
        form.addRow("", self.ask_before)
        form.addRow("", self.autostart)
        form.addRow(tr("Language:"), self.language)
        form.addRow(tr("Theme:"), self.theme)
        return page

    def _connection_tab(self) -> QWidget:
        page = QWidget()
        self.connections = QSpinBox()
        self.connections.setRange(1, 32)
        self.connections.setValue(self.settings.connections)

        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 20)
        self.concurrent.setValue(self.settings.max_concurrent)

        limit = self.settings.speed_limit
        self.limit = QLineEdit(human_size(limit).replace(" ", "") if limit else "")
        self.limit.setPlaceholderText(tr("unlimited"))

        self.proxy = QLineEdit(self.settings.get("proxy") or "")
        self.proxy.setPlaceholderText("http://127.0.0.1:8080")
        self.user_agent = QLineEdit(self.settings.get("user_agent") or "")
        self.user_agent.setPlaceholderText(tr("auto"))
        self.verify_tls = QCheckBox(tr("Verify TLS certificates"))
        self.verify_tls.setChecked(bool(self.settings.get("verify_tls")))

        form = QFormLayout(page)
        form.addRow(tr("Default connections:"), self.connections)
        form.addRow(tr("Simultaneous downloads:"), self.concurrent)
        form.addRow(tr("Global speed limit:"), self.limit)
        form.addRow(tr("Proxy:"), self.proxy)
        form.addRow(tr("User-Agent:"), self.user_agent)
        form.addRow("", self.verify_tls)
        note = QLabel(tr("Restart required for the language change."))
        note.setEnabled(False)
        form.addRow("", note)
        return page

    def _clipboard_tab(self) -> QWidget:
        page = QWidget()
        self.clipboard_monitor = QCheckBox(tr("Watch the clipboard"))
        self.clipboard_monitor.setChecked(bool(self.settings.get("clipboard_monitor")))
        self.clipboard_ask = QCheckBox(tr("Ask before every download"))
        self.clipboard_ask.setChecked(bool(self.settings.get("clipboard_ask")))
        self.clipboard_extensions = QLineEdit(
            self.settings.get("clipboard_extensions") or ""
        )
        self.clipboard_extensions.setPlaceholderText(
            tr("jpg, png, mp4 (empty = every file)")
        )

        form = QFormLayout(page)
        form.addRow("", self.clipboard_monitor)
        form.addRow("", self.clipboard_ask)
        form.addRow(tr("Extensions:"), self.clipboard_extensions)
        note = QLabel(
            tr("Only text that is a bare link counts, so copying a paragraph "
               "does nothing.")
        )
        note.setWordWrap(True)
        note.setEnabled(False)
        form.addRow("", note)
        return page

    def _browser_tab(self) -> QWidget:
        page = QWidget()
        self.extension_id = QLineEdit(self.settings.get("extension_id") or "")
        self.extension_id.setPlaceholderText(tr("32 letters from chrome://extensions"))
        register_button = QPushButton(tr("Register"))
        register_button.clicked.connect(self._register_host)
        remove_button = QPushButton(tr("Remove"))
        remove_button.clicked.connect(self._unregister_host)

        row = QHBoxLayout()
        row.addWidget(self.extension_id, 1)
        row.addWidget(register_button)
        row.addWidget(remove_button)

        self.host_status = QLabel()
        self.host_status.setWordWrap(True)
        self.host_status.setEnabled(False)
        self._refresh_host_status()

        form = QFormLayout(page)
        form.addRow(tr("Extension ID:"), row)
        form.addRow("", self.host_status)
        note = QLabel(
            tr("Load extension/ as an unpacked extension, then paste its ID here.")
        )
        note.setWordWrap(True)
        note.setEnabled(False)
        form.addRow("", note)
        return page

    def _refresh_host_status(self) -> None:
        from ..ipc import register

        try:
            registered = [b for b, value in register.status().items() if value]
        except OSError:  # pragma: no cover - non-Windows
            registered = []
        self.host_status.setText(
            f"{tr('Registered for')}: {', '.join(registered)}"
            if registered
            else tr("Not registered yet")
        )

    def _register_host(self) -> None:
        from ..ipc import register

        extension_id = self.extension_id.text().strip()
        if not register.valid_extension_id(extension_id):
            QMessageBox.warning(
                self, tr("Browser integration"),
                tr("32 letters from chrome://extensions"),
            )
            return
        try:
            register.install([extension_id])
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, tr("Browser integration"), str(exc))
            return
        self.settings.set("extension_id", extension_id)
        self._refresh_host_status()

    def _unregister_host(self) -> None:
        from ..ipc import register

        try:
            register.uninstall()
        except OSError as exc:  # pragma: no cover - non-Windows
            QMessageBox.warning(self, tr("Browser integration"), str(exc))
            return
        self._refresh_host_status()

    def _video_tab(self) -> QWidget:
        page = QWidget()
        self.quality = QComboBox()
        for label, height in QUALITIES:
            self.quality.addItem(tr(label), height)
        self.quality.setCurrentIndex(
            max(0, self.quality.findData(self.settings.video_quality))
        )

        self.ffmpeg_edit = QLineEdit(self.settings.ffmpeg_path or "")
        self.ffmpeg_edit.setPlaceholderText(tr("auto"))
        pick = QPushButton(tr("Browse..."))
        pick.clicked.connect(self._browse_ffmpeg)
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(pick)

        form = QFormLayout(page)
        form.addRow(tr("Preferred quality:"), self.quality)
        form.addRow(tr("ffmpeg:"), ffmpeg_row)
        self.tool_status = QLabel(_tool_status(self.settings.ffmpeg_path))
        self.tool_status.setWordWrap(True)
        self.tool_status.setEnabled(False)
        form.addRow("", self.tool_status)
        self.ffmpeg_edit.textChanged.connect(
            lambda text: self.tool_status.setText(_tool_status(text.strip() or None))
        )
        return page

    # --------------------------------------------------------------- actions

    def _browse_ffmpeg(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, tr("ffmpeg:"), self.ffmpeg_edit.text(),
            "ffmpeg (ffmpeg.exe ffmpeg);;" + tr("All files") + " (*)",
        )
        if chosen:
            self.ffmpeg_edit.setText(chosen)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Downloads folder:"), self.dir_edit.text()
        )
        if chosen:
            self.dir_edit.setText(chosen)

    def parsed_limit(self) -> int | None:
        text = self.limit.text().strip()
        return parse_size(text) if text else None

    def _save(self) -> None:
        try:
            limit = self.parsed_limit()
        except ValueError:
            QMessageBox.warning(self, tr("Settings"), tr("Global speed limit:"))
            return
        self.settings.update({
            "download_dir": self.dir_edit.text().strip() or None,
            "use_categories": self.use_categories.isChecked(),
            "minimize_to_tray": self.minimize_to_tray.isChecked(),
            "ask_before_download": self.ask_before.isChecked(),
            "language": self.language.currentData(),
            "theme": self.theme.currentData(),
            "connections": self.connections.value(),
            "max_concurrent": self.concurrent.value(),
            "speed_limit": limit,
            "proxy": self.proxy.text().strip() or None,
            "user_agent": self.user_agent.text().strip() or None,
            "verify_tls": self.verify_tls.isChecked(),
            "video_quality": self.quality.currentData(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip() or None,
            "start_with_windows": self.autostart.isChecked(),
            "clipboard_monitor": self.clipboard_monitor.isChecked(),
            "clipboard_ask": self.clipboard_ask.isChecked(),
            "clipboard_extensions": self.clipboard_extensions.text().strip() or None,
        })
        # The registry is the source of truth Windows reads, so keep it in step
        # with the checkbox - and keep the setting honest if the write failed.
        if sys.platform == "win32":
            if not autostart.apply(self.autostart.isChecked()):
                self.settings.set("start_with_windows", autostart.is_enabled())
        # The theme is the one setting that must not wait for a restart.
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, self.theme.currentData())
        self.accept()


def _tool_status(ffmpeg_path: str | None) -> str:
    """Tell the user, in the dialog, whether the optional tools are there."""
    binary = find_ffmpeg(ffmpeg_path)
    parts = [
        f"ffmpeg: {binary}" if binary else tr("ffmpeg not found - videos are saved unmerged"),
        f"yt-dlp: {ytdlp_version()}" if ytdlp_version() else tr("yt-dlp not installed"),
    ]
    return "\n".join(parts)
