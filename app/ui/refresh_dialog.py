"""Refresh download address: give an unfinished download a new link.

Two ways to get one, as in IDM: paste it, or let the browser hand it over -
the source page opens, the user clicks the download link again, and the
extension's capture is attached to this download instead of starting a new
one (see `MainWindow.handle_ipc_download`).
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..util.fmt import human_size
from .controller import DownloadItem
from .i18n import tr

#: how long a "get it from the browser" request waits for the click
BROWSER_WAIT = 300.0


def _is_http(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://"))


class RefreshDialog(QDialog):
    """Asks for the new address. `from_browser` is set when the user chose
    to fetch it from the browser rather than paste one."""

    def __init__(self, item: DownloadItem, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self.from_browser = False
        self.setWindowTitle(tr("Refresh download address"))
        self.setMinimumWidth(560)

        explain = QLabel(tr(
            "The link of this download no longer works - it expired, or the "
            "site wants a new one. Give it a new address for the same file; "
            "what is already downloaded is kept."
        ))
        explain.setWordWrap(True)

        progress = f"{human_size(item.downloaded)}"
        if item.size:
            progress += f" / {human_size(item.size)}"
        self.current = QLineEdit(item.url)
        self.current.setReadOnly(True)
        self.new_url = QLineEdit()
        self.new_url.setPlaceholderText("https://...")
        pasted = QGuiApplication.clipboard().text().strip()
        if _is_http(pasted) and pasted != item.url:
            self.new_url.setText(pasted)

        form = QFormLayout()
        form.addRow(tr("File name:"), QLabel(item.filename))
        form.addRow(tr("Downloaded:"), QLabel(progress))
        form.addRow(tr("Current address:"), self.current)
        form.addRow(tr("New address:"), self.new_url)

        self.browser_button = QPushButton(tr("Get it from the browser"))
        self.browser_button.setToolTip(tr(
            "Opens the page the file came from; click its download link "
            "again and the new address is used for this download."
        ))
        self.browser_button.setEnabled(_is_http(item.referer or ""))
        self.browser_button.clicked.connect(self._use_browser)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.addButton(self.browser_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self._accept_url)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.new_url.textChanged.connect(self._update)
        self._update()

        layout = QVBoxLayout(self)
        layout.addWidget(explain)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.new_url.setFocus()

    def address(self) -> str:
        return self.new_url.text().strip()

    def _update(self) -> None:
        text = self.address()
        self.ok_button.setEnabled(_is_http(text) and text != self.item.url)

    def _accept_url(self) -> None:
        if self.ok_button.isEnabled():
            self.accept()

    def _use_browser(self) -> None:
        self.from_browser = True
        self.accept()
