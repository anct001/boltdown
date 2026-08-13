"""The 'Add a download' dialog."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..core.categories import category_for
from ..media.detect import classify, suggested_name
from ..storage.settings import Settings
from ..util import filenames
from ..util.fmt import parse_size
from .i18n import tr

#: what happens to each kind of media URL, shown under the quality selector
_HINTS = {
    "hls": "HLS playlist: segments download in parallel, then ffmpeg joins them.",
    "dash": "DASH manifest: yt-dlp picks the tracks, ffmpeg merges them.",
    "site": "Video page: yt-dlp finds the streams, the download stays multi-segment.",
}

#: label -> maximum height; None means "whatever the site offers"
QUALITIES: list[tuple[str, int | None]] = [
    ("Best available", None),
    ("2160p", 2160),
    ("1440p", 1440),
    ("1080p", 1080),
    ("720p", 720),
    ("480p", 480),
    ("360p", 360),
]


class AddUrlDialog(QDialog):
    def __init__(self, settings: Settings, url: str = "", parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.start_now = True
        self.setWindowTitle(tr("Add a download"))
        self.setMinimumWidth(560)

        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://...")
        self.dir_edit = QLineEdit(str(settings.download_dir))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("auto"))
        self.category_label = QLabel("-")
        self.connections = QSpinBox()
        self.connections.setRange(1, 32)
        self.connections.setValue(settings.connections)

        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)

        form = QFormLayout()
        form.addRow(tr("Address:"), self.url_edit)
        form.addRow(tr("Save to:"), dir_row)
        form.addRow(tr("File name:"), self.name_edit)
        form.addRow(tr("Category:"), self.category_label)
        form.addRow(tr("Connections:"), self.connections)

        # --- advanced ---------------------------------------------------
        self.referer = QLineEdit()
        self.cookie = QLineEdit()
        self.cookie_import = QPushButton(tr("From browser"))
        self.cookie_import.setToolTip(tr("Read the cookies this site set in Chrome/Edge"))
        self.cookie_import.clicked.connect(self._import_cookies)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(self.cookie, 1)
        cookie_row.addWidget(self.cookie_import)
        self.user_agent = QLineEdit()
        self.user_agent.setPlaceholderText(tr("auto"))
        self.proxy = QLineEdit(settings.get("proxy") or "")
        self.proxy.setPlaceholderText("http://127.0.0.1:8080")
        self.limit = QLineEdit()
        self.limit.setPlaceholderText(tr("unlimited"))

        advanced_form = QFormLayout()
        advanced_form.addRow(tr("Referer:"), self.referer)
        advanced_form.addRow(tr("Cookie:"), cookie_row)
        advanced_form.addRow(tr("User-Agent:"), self.user_agent)
        advanced_form.addRow(tr("Proxy:"), self.proxy)
        advanced_form.addRow(tr("Speed limit:"), self.limit)

        self.advanced = QGroupBox(tr("Advanced"))
        self.advanced.setCheckable(True)
        self.advanced.setChecked(False)
        self.advanced.setLayout(advanced_form)  # checkable box disables its children

        # --- video / streaming -------------------------------------------
        self.quality = QComboBox()
        for label, height in QUALITIES:
            self.quality.addItem(tr(label), height)
        self.quality.setCurrentIndex(
            max(0, self.quality.findData(settings.video_quality))
        )
        self.audio_only = QCheckBox(tr("Audio only"))
        self.media_hint = QLabel()
        self.media_hint.setWordWrap(True)
        self.media_hint.setEnabled(False)

        video_form = QFormLayout()
        video_form.addRow(tr("Quality:"), self.quality)
        video_form.addRow("", self.audio_only)
        video_form.addRow("", self.media_hint)
        self.video_box = QGroupBox(tr("Video / stream"))
        self.video_box.setLayout(video_form)

        buttons = QDialogButtonBox()
        self.now_button = buttons.addButton(
            tr("Download now"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.later_button = buttons.addButton(
            tr("Download later"), QDialogButtonBox.ButtonRole.ActionRole
        )
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept_now)
        buttons.rejected.connect(self.reject)
        self.later_button.clicked.connect(self._accept_later)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.video_box)
        layout.addWidget(self.advanced)
        layout.addWidget(buttons)

        self.url_edit.textChanged.connect(self._refresh_preview)
        self.name_edit.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

    # ------------------------------------------------------------- behaviour

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, tr("Save to:"), self.dir_edit.text()
        )
        if chosen:
            self.dir_edit.setText(chosen)

    def _refresh_preview(self) -> None:
        url = self.url_edit.text().strip()
        kind = classify(url) if url else None
        media = bool(kind and kind.is_media)
        # Quality only means something when there are several renditions to
        # choose from, so the box is disabled for an ordinary file.
        self.video_box.setEnabled(media)
        self.media_hint.setText(tr(_HINTS.get(kind.value, "")) if media else "")

        name = self.name_edit.text().strip()
        if not name:
            name = (suggested_name(url) if media else filenames.from_url(url)) or ""
        self.category_label.setText(tr(category_for(name)) if name else "-")

    def _import_cookies(self) -> None:
        """Fill the Cookie field from the browser's own store."""
        from ..util import browser_cookies

        url = self.url_edit.text().strip()
        host = urlsplit(url).hostname or ""
        if not host:
            QMessageBox.warning(self, tr("Add a download"), tr("Enter a URL"))
            return
        browsers = browser_cookies.installed_browsers()
        if not browsers:
            QMessageBox.information(
                self, tr("Cookie:"), tr("No Chromium browser profile was found.")
            )
            return
        for browser in browsers:
            header = browser_cookies.read_cookies(browser, host)
            if header:
                self.cookie.setText(header)
                self.advanced.setChecked(True)
                return
        QMessageBox.information(
            self, tr("Cookie:"),
            tr("That site has no cookies stored in your browser."),
        )

    def _accept_now(self) -> None:
        self.start_now = True
        if self._validate():
            self.accept()

    def _accept_later(self) -> None:
        self.start_now = False
        if self._validate():
            self.accept()

    def _validate(self) -> bool:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, tr("Add a download"), tr("Enter a URL"))
            return False
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            QMessageBox.warning(
                self, tr("Add a download"),
                tr("That does not look like an http(s) URL."),
            )
            return False
        try:
            self.speed_limit()
        except ValueError:
            QMessageBox.warning(self, tr("Add a download"), tr("Speed limit:"))
            return False
        return True

    # ---------------------------------------------------------------- result

    def speed_limit(self) -> int | None:
        text = self.limit.text().strip()
        return parse_size(text) if text else None

    def options(self) -> dict:
        return {
            "url": self.url_edit.text().strip(),
            "save_dir": Path(self.dir_edit.text().strip()),
            "filename": self.name_edit.text().strip() or None,
            "connections": self.connections.value(),
            "speed_limit": self.speed_limit(),
            "referer": self.referer.text().strip() or None,
            "cookie": self.cookie.text().strip() or None,
            "user_agent": self.user_agent.text().strip() or None,
            "proxy": self.proxy.text().strip() or None,
            "start_now": self.start_now,
            "max_height": self.quality.currentData(),
            "audio_only": self.audio_only.isChecked(),
        }


class ConfirmExitDialog(QDialog):
    """Asked when downloads are still running at exit."""

    def __init__(self, active: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Exit"))
        self.remember = QCheckBox(tr("Minimize to tray instead of closing"))
        label = QLabel(f"{tr('Downloading')}: {active}")
        label.setTextFormat(Qt.TextFormat.PlainText)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(self.remember)
        layout.addWidget(buttons)
