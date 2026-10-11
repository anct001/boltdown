"""Options dialog."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
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
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import categories, torrent
from ..media.ffmpeg import find_ffmpeg
from ..media.ytdlp import version as ytdlp_version
from ..storage.settings import Settings
from ..util import autostart, phone, proxy
from ..util.credentials import protect, unprotect
from ..util.fmt import human_size, parse_size
from . import theme
from .add_url_dialog import QUALITIES
from .i18n import LANGUAGES, tr


class _Live:
    """The saved settings, but with the volume the slider is showing now."""

    def __init__(self, settings, volume: int) -> None:
        self._settings = settings
        self._volume = volume

    def get(self, key, default=None):
        if key == "sound_volume":
            return self._volume
        if key == "sound_effects":
            return True
        return self._settings.get(key, default)


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
        tabs.addTab(self._categories_tab(), tr("Categories"))
        tabs.addTab(self._clipboard_tab(), tr("Clipboard"))
        tabs.addTab(self._browser_tab(), tr("Browser integration"))
        tabs.addTab(self._phone_tab(), tr("Phone"))

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
        self.resume_on_start = QCheckBox(tr("Continue unfinished downloads at start"))
        self.resume_on_start.setChecked(bool(self.settings.get("resume_on_start")))
        self.notify_on_finish = QCheckBox(tr("Notify when a download finishes"))
        self.notify_on_finish.setChecked(bool(self.settings.get("notify_on_finish")))

        self.language = QComboBox()
        for code, label in LANGUAGES.items():
            self.language.addItem(label, code)
        index = self.language.findData(self.settings.language)
        self.language.setCurrentIndex(max(0, index))

        self.theme = QComboBox()
        self.theme.addItem(tr("Follow Windows"), theme.AUTO)
        for palette in theme.THEMES.values():
            self.theme.addItem(tr(palette.label), palette.name)
        self.theme.setCurrentIndex(
            max(0, self.theme.findData(self.settings.get("theme") or theme.AUTO))
        )

        form = QFormLayout(page)
        form.addRow(tr("Downloads folder:"), row)
        form.addRow("", self.use_categories)
        form.addRow("", self.minimize_to_tray)
        form.addRow("", self.ask_before)
        form.addRow("", self.autostart)
        form.addRow("", self.resume_on_start)
        form.addRow("", self.notify_on_finish)
        form.addRow(tr("Language:"), self.language)
        form.addRow(tr("Theme:"), self.theme)

        # Sound sits with the theme on purpose: the blips are part of the
        # look, not a separate feature to hunt for.
        self.sound_effects = QCheckBox(tr("Sound effects"))
        self.sound_effects.setChecked(bool(self.settings.get("sound_effects")))
        self.sound_volume = QSlider(Qt.Orientation.Horizontal)
        self.sound_volume.setRange(0, 100)
        self.sound_volume.setValue(int(self.settings.get("sound_volume") or 0))
        self.sound_volume.setToolTip(tr("Volume"))
        preview = QPushButton(tr("Listen"))
        preview.clicked.connect(self._preview_sound)
        row = QHBoxLayout()
        row.addWidget(self.sound_volume, 1)
        row.addWidget(preview)
        form.addRow("", self.sound_effects)
        form.addRow(tr("Volume:"), row)
        return page

    def _preview_sound(self) -> None:
        """Play at the volume on the slider, whatever the checkbox says.

        The point of the button is to hear it before deciding, so it ignores
        the toggle and reads the slider rather than the saved setting.
        """
        from . import sounds

        board = sounds.SoundBoard(_Live(self.settings, self.sound_volume.value()))
        board.preview("completed")

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
        turtle = int(self.settings.get("turtle_limit") or 0)
        self.turtle_limit = QLineEdit(human_size(turtle).replace(" ", "") if turtle else "")
        self.turtle_limit.setPlaceholderText("512KB")
        self.turtle_limit.setToolTip(tr("Ctrl+T or the turtle in the status bar switches it"))

        self.proxy = QLineEdit(self.settings.get("proxy") or "")
        self.proxy.setPlaceholderText(
            "http://127.0.0.1:8080  /  socks5://127.0.0.1:1080"
        )
        self.use_system_proxy = QCheckBox(tr("Use the Windows proxy settings"))
        self.use_system_proxy.setChecked(bool(self.settings.get("use_system_proxy")))
        self.proxy_status = QLabel(_proxy_status())
        self.proxy_status.setWordWrap(True)
        self.proxy_status.setEnabled(False)
        self.user_agent = QLineEdit(self.settings.get("user_agent") or "")
        self.user_agent.setPlaceholderText(tr("auto"))
        self.verify_tls = QCheckBox(tr("Verify TLS certificates"))
        self.verify_tls.setChecked(bool(self.settings.get("verify_tls")))
        self.adaptive = QCheckBox(tr("Slow down while other programs need the network"))
        self.adaptive.setToolTip(tr(
            "Watches the round trip to the internet; when downloads make it "
            "climb, they are slowed until calls, games and pages are quick again."
        ))
        self.adaptive.setChecked(bool(self.settings.get("adaptive_throttle")))
        self.adaptive_target = QSpinBox()
        self.adaptive_target.setRange(20, 500)
        self.adaptive_target.setSuffix(" ms")
        self.adaptive_target.setValue(int(self.settings.get("adaptive_target_ms") or 75))
        self.adaptive_target.setToolTip(tr("Extra delay allowed before downloads give way"))
        self.adaptive_target.setEnabled(self.adaptive.isChecked())
        self.adaptive.toggled.connect(self.adaptive_target.setEnabled)
        self.open_torrents = QCheckBox(tr("Download .torrent links as torrents"))
        self.open_torrents.setChecked(bool(self.settings.get("open_torrents")))
        self.torrent_status = QLabel(
            tr("Torrents: ready (libtorrent installed)") if torrent.available()
            else tr("Torrents need libtorrent: pip install libtorrent")
        )
        self.torrent_status.setWordWrap(True)
        self.torrent_status.setEnabled(False)

        form = QFormLayout(page)
        form.addRow(tr("Default connections:"), self.connections)
        form.addRow(tr("Simultaneous downloads:"), self.concurrent)
        form.addRow(tr("Global speed limit:"), self.limit)
        form.addRow(tr("Slow mode limit:"), self.turtle_limit)
        form.addRow("", self.adaptive)
        form.addRow(tr("Allowed extra delay:"), self.adaptive_target)
        form.addRow("", self.open_torrents)
        form.addRow("", self.torrent_status)
        form.addRow(tr("Proxy:"), self.proxy)
        form.addRow("", self.use_system_proxy)
        form.addRow("", self.proxy_status)
        form.addRow(tr("User-Agent:"), self.user_agent)
        form.addRow("", self.verify_tls)
        note = QLabel(tr("Restart required for the language change."))
        note.setEnabled(False)
        form.addRow("", note)
        return page

    def _categories_tab(self) -> QWidget:
        page = QWidget()
        self.categories = QPlainTextEdit(
            self.settings.get("categories") or categories.format_categories()
        )
        self.categories.setPlaceholderText("Video = mp4, mkv, avi")
        restore = QPushButton(tr("Restore defaults"))
        restore.clicked.connect(
            lambda: self.categories.setPlainText(
                categories.format_categories(categories.CATEGORIES)
            )
        )
        self.auto_extract = QCheckBox(tr("Unpack archives when they finish"))
        self.auto_extract.setChecked(bool(self.settings.get("auto_extract")))
        self.auto_checksum = QCheckBox(
            tr("Check installers and disk images against the site's published checksums")
        )
        self.auto_checksum.setToolTip(tr(
            "After the download, look for SHA256SUMS or a .sha256 file next to "
            "it and compare. A mismatch is reported and the file is not unpacked."
        ))
        self.auto_checksum.setChecked(bool(self.settings.get("auto_checksum")))
        self.scan_defender = QCheckBox(tr("Scan finished files with Defender"))
        self.scan_defender.setChecked(bool(self.settings.get("scan_with_defender")))
        self.scan_defender.setEnabled(sys.platform == "win32")

        layout = QVBoxLayout(page)
        note = QLabel(tr("One line per folder: Name = ext, ext, ext"))
        note.setEnabled(False)
        layout.addWidget(note)
        layout.addWidget(self.categories, 1)
        layout.addWidget(restore)
        layout.addWidget(self.auto_extract)
        layout.addWidget(self.auto_checksum)
        layout.addWidget(self.scan_defender)
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
        self.extension_id.setPlaceholderText(tr("extension id, or a Firefox add-on id"))
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
                tr("extension id, or a Firefox add-on id"),
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

        self.subtitle_langs = QLineEdit(str(self.settings.get("subtitle_langs") or ""))
        self.subtitle_langs.setPlaceholderText(tr("e.g. vi, en - or all; empty for none"))
        self.embed_thumbnail = QCheckBox(tr("Put the video's thumbnail in as cover art"))
        self.embed_thumbnail.setChecked(bool(self.settings.get("embed_thumbnail")))

        form = QFormLayout(page)
        form.addRow(tr("Preferred quality:"), self.quality)
        form.addRow(tr("Subtitles:"), self.subtitle_langs)
        form.addRow("", self.embed_thumbnail)
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

    def _phone_tab(self) -> QWidget:
        page = QWidget()
        self.phone_service = QComboBox()
        for label, value in ((tr("Off"), phone.OFF), ("ntfy", phone.NTFY),
                             ("Telegram", phone.TELEGRAM)):
            self.phone_service.addItem(label, value)
        self.phone_service.setCurrentIndex(
            max(0, self.phone_service.findData(self.settings.get("phone_service") or phone.OFF))
        )
        self.ntfy_server = QLineEdit(str(self.settings.get("ntfy_server") or phone.DEFAULT_NTFY))
        self.ntfy_topic = QLineEdit(str(self.settings.get("ntfy_topic") or ""))
        self.ntfy_topic.setPlaceholderText(tr("a long name nobody would guess"))
        self.telegram_token = QLineEdit(unprotect(self.settings.get("telegram_token")))
        self.telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_token.setPlaceholderText("123456:ABC...")
        self.telegram_chat = QLineEdit(str(self.settings.get("telegram_chat") or ""))
        self.phone_events = {}
        for event, label in (("finished", tr("When a download finishes")),
                             ("failed", tr("When a download fails")),
                             ("queue_done", tr("When a queue has finished"))):
            box = QCheckBox(label)
            box.setChecked(bool(self.settings.get(f"phone_on_{event}")))
            self.phone_events[event] = box
        test = QPushButton(tr("Send a test"))
        test.clicked.connect(self._test_phone)
        self.phone_status = QLabel("")
        self.phone_status.setWordWrap(True)

        form = QFormLayout(page)
        note = QLabel(tr(
            "ntfy needs no account: install the ntfy app and subscribe to the "
            "topic below. Telegram needs a bot from @BotFather and your chat id."
        ))
        note.setWordWrap(True)
        form.addRow(note)
        form.addRow(tr("Send with:"), self.phone_service)
        form.addRow(tr("ntfy server:"), self.ntfy_server)
        form.addRow(tr("ntfy topic:"), self.ntfy_topic)
        form.addRow(tr("Telegram bot token:"), self.telegram_token)
        form.addRow(tr("Telegram chat id:"), self.telegram_chat)
        for box in self.phone_events.values():
            form.addRow("", box)
        form.addRow(test, self.phone_status)

        # Remote control: the other direction, the phone telling the app.
        heading = QLabel(f"<b>{tr('Remote control')}</b>")
        form.addRow(heading)
        self.remote_enabled = QCheckBox(tr("Control downloads from a phone on this network"))
        self.remote_enabled.setChecked(bool(self.settings.get("remote_enabled")))
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1024, 65535)
        self.remote_port.setValue(int(self.settings.get("remote_port") or 9614))
        self._new_remote_token = False
        new_token = QPushButton(tr("New link"))
        new_token.setToolTip(tr("Phones with the old link lose access"))
        new_token.clicked.connect(self._renew_remote_token)
        self.remote_links = QLabel()
        self.remote_links.setWordWrap(True)
        self.remote_links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.remote_links.setOpenExternalLinks(True)
        self._show_remote_links()
        form.addRow("", self.remote_enabled)
        form.addRow(tr("Port:"), self.remote_port)
        form.addRow(new_token, self.remote_links)
        return page

    def _remote(self):
        return getattr(self.parent(), "remote", None)

    def _show_remote_links(self) -> None:
        remote = self._remote()
        links = remote.links() if remote is not None and not self._new_remote_token else []
        if links:
            shown = "<br>".join(f'<a href="{link}">{link}</a>' for link in links)
            self.remote_links.setText(
                tr("Open on the phone (same Wi-Fi):") + "<br>" + shown
            )
        elif remote is not None and remote.error:
            self.remote_links.setText(f"{tr('Failed')}: {remote.error}")
        else:
            self.remote_links.setText(tr("The link appears here once it is switched on."))

    def _renew_remote_token(self) -> None:
        self._new_remote_token = True
        self._show_remote_links()

    def _phone_target(self) -> "phone.Target":
        return phone.Target(
            service=self.phone_service.currentData(),
            ntfy_server=self.ntfy_server.text().strip() or phone.DEFAULT_NTFY,
            ntfy_topic=self.ntfy_topic.text().strip(),
            telegram_token=self.telegram_token.text().strip(),
            telegram_chat=self.telegram_chat.text().strip(),
            proxy=self.proxy.text().strip() or None,
        )

    def _test_phone(self) -> None:
        target = self._phone_target()
        if not target.ready:
            self.phone_status.setText(tr("Choose a service and fill in its fields first."))
            return
        problem = phone.send(target, "Boltdown", tr("Test message - notifications work."))
        self.phone_status.setText(tr("Sent.") if problem is None else f"{tr('Failed')}: {problem}")

    def _save(self) -> None:
        try:
            limit = self.parsed_limit()
        except ValueError:
            QMessageBox.warning(self, tr("Settings"), tr("Global speed limit:"))
            return
        try:
            text = self.turtle_limit.text().strip()
            turtle_limit = parse_size(text) if text else 512 * 1024
        except ValueError:
            QMessageBox.warning(self, tr("Settings"), tr("Slow mode limit:"))
            return
        self.settings.update({
            "download_dir": self.dir_edit.text().strip() or None,
            "use_categories": self.use_categories.isChecked(),
            "minimize_to_tray": self.minimize_to_tray.isChecked(),
            "ask_before_download": self.ask_before.isChecked(),
            "language": self.language.currentData(),
            "theme": self.theme.currentData(),
            "sound_effects": self.sound_effects.isChecked(),
            "sound_volume": self.sound_volume.value(),
            "connections": self.connections.value(),
            "max_concurrent": self.concurrent.value(),
            "speed_limit": limit,
            "turtle_limit": turtle_limit,
            "adaptive_throttle": self.adaptive.isChecked(),
            "open_torrents": self.open_torrents.isChecked(),
            "remote_enabled": self.remote_enabled.isChecked(),
            "remote_port": self.remote_port.value(),
            **({"remote_token": ""} if self._new_remote_token else {}),
            "adaptive_target_ms": self.adaptive_target.value(),
            "proxy": self.proxy.text().strip() or None,
            "use_system_proxy": self.use_system_proxy.isChecked(),
            "user_agent": self.user_agent.text().strip() or None,
            "verify_tls": self.verify_tls.isChecked(),
            "video_quality": self.quality.currentData(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip() or None,
            "start_with_windows": self.autostart.isChecked(),
            "clipboard_monitor": self.clipboard_monitor.isChecked(),
            "clipboard_ask": self.clipboard_ask.isChecked(),
            "clipboard_extensions": self.clipboard_extensions.text().strip() or None,
            "resume_on_start": self.resume_on_start.isChecked(),
            "notify_on_finish": self.notify_on_finish.isChecked(),
            "auto_extract": self.auto_extract.isChecked(),
            "auto_checksum": self.auto_checksum.isChecked(),
            "subtitle_langs": self.subtitle_langs.text().strip(),
            "phone_service": self.phone_service.currentData(),
            "ntfy_server": self.ntfy_server.text().strip() or phone.DEFAULT_NTFY,
            "ntfy_topic": self.ntfy_topic.text().strip(),
            "telegram_token": protect(self.telegram_token.text().strip()),
            "telegram_chat": self.telegram_chat.text().strip(),
            **{f"phone_on_{event}": box.isChecked() for event, box in self.phone_events.items()},
            "embed_thumbnail": self.embed_thumbnail.isChecked(),
            "scan_with_defender": self.scan_defender.isChecked(),
            "categories": self.categories.toPlainText().strip() or None,
        })
        # The table has to take effect for the next download, not the next launch.
        categories.set_categories(
            categories.parse_categories(self.categories.toPlainText())
        )
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


def _proxy_status() -> str:
    """Tell the user what Windows is set to, and whether SOCKS will work."""
    settings = proxy.system_proxy()
    parts = []
    if settings.pac_url:
        parts.append(f"{tr('System PAC file')}: {settings.pac_url}")
    elif settings.enabled and settings.server:
        parts.append(f"{tr('Windows proxy')}: {settings.server}")
    else:
        parts.append(tr("Windows is set to connect directly"))
    if not proxy.socks_available():
        parts.append(tr("socks5:// needs the socksio package"))
    return "\n".join(parts)


def _tool_status(ffmpeg_path: str | None) -> str:
    """Tell the user, in the dialog, whether the optional tools are there."""
    binary = find_ffmpeg(ffmpeg_path)
    parts = [
        f"ffmpeg: {binary}" if binary else tr("ffmpeg not found - videos are saved unmerged"),
        f"yt-dlp: {ytdlp_version()}" if ytdlp_version() else tr("yt-dlp not installed"),
    ]
    return "\n".join(parts)
