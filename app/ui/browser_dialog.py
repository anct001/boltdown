"""Setting up the browser extension, without a wiki page open on the side.

Everything here is a step the user would otherwise do by reading the README:
find the unpacked extension, open the browser's extensions page, paste the id
the browser then invents, and register the native-messaging host. The dialog
also says, per browser, whether the host is registered - which is the one fact
that decides whether any of it works.

Loading the extension itself stays the user's own click: browsers deliberately
have no way for another program to install an extension behind their back, and
that is a good rule, not an obstacle to route around.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..ipc import register
from ..util.log import get_logger
from . import icons
from .i18n import tr

log = get_logger(__name__)

#: the page each browser lists its extensions on
EXTENSION_PAGES = {
    "chrome": "chrome://extensions",
    "edge": "edge://extensions",
    "chromium": "chrome://extensions",
    "brave": "brave://extensions",
    "firefox": "about:debugging#/runtime/this-firefox",
}

def browser_label(name: str) -> str:
    return {
        "chrome": "Google Chrome", "edge": "Microsoft Edge",
        "chromium": "Chromium", "brave": "Brave", "firefox": "Firefox",
    }.get(name, name.title())


def open_folder(path: Path) -> None:
    """Show a directory in the file manager."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - a directory we chose ourselves
    else:  # pragma: no cover - Windows is the target
        subprocess.Popen(["xdg-open", str(path)])


class BrowserDialog(QDialog):
    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(tr("Browser integration"))
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.addWidget(self._intro())
        layout.addWidget(self._step_one())
        layout.addWidget(self._step_two())
        layout.addWidget(self._status_box())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_status()

    # ------------------------------------------------------------------ pieces

    def _intro(self) -> QLabel:
        label = QLabel(tr(
            "With the extension installed, every download you start in the "
            "browser is handed to Boltdown instead."
        ))
        label.setWordWrap(True)
        return label

    def _step_one(self) -> QGroupBox:
        box = QGroupBox(tr("1. Load the extension into your browser"))
        layout = QVBoxLayout(box)

        chrome_dir = register.extension_dir("chrome")
        firefox_dir = register.extension_dir("firefox")

        layout.addWidget(self._folder_row(
            tr("Chrome, Edge, Brave: Developer mode -> Load unpacked"), chrome_dir
        ))
        layout.addWidget(self._folder_row(
            tr("Firefox: about:debugging -> Load Temporary Add-on -> manifest.json"),
            firefox_dir,
        ))
        if firefox_dir is None:
            hint = QLabel(tr(
                "The Firefox folder is built by scripts/build_extension.py."
            ))
            hint.setWordWrap(True)
            layout.addWidget(hint)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Extensions page:")))
        for name, page in (("edge", EXTENSION_PAGES["edge"]),
                           ("chrome", EXTENSION_PAGES["chrome"]),
                           ("firefox", EXTENSION_PAGES["firefox"])):
            button = QPushButton(browser_label(name))
            button.setToolTip(tr("Copy the address; paste it into the browser"))
            button.clicked.connect(lambda _=False, p=page: self._copy(p))
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        # A browser cannot be sent to chrome://extensions from outside, so the
        # address goes to the clipboard and this line says so.
        self.copied = QLabel("")
        layout.addWidget(self.copied)
        return box

    def _folder_row(self, text: str, folder: Path | None) -> QFrame:
        frame = QFrame()
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(text)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        button = QPushButton(icons.folder_icon(), tr("Open Folder"))
        button.setEnabled(folder is not None)
        if folder is not None:
            button.setToolTip(str(folder))
            button.clicked.connect(lambda _=False, f=folder: open_folder(f))
        row.addWidget(button)
        return frame

    def _step_two(self) -> QGroupBox:
        box = QGroupBox(tr("2. Allow it to talk to Boltdown"))
        layout = QVBoxLayout(box)
        explain = QLabel(tr(
            "Chromium gives the unpacked extension a new id every time it is "
            "loaded, so paste the id shown under its name. Firefox always uses "
            "the same one and is registered already."
        ))
        explain.setWordWrap(True)
        layout.addWidget(explain)

        row = QHBoxLayout()
        self.extension_id = QLineEdit(self.settings.get("extension_id") or "")
        self.extension_id.setPlaceholderText(
            tr("extension id, or a Firefox add-on id")
        )
        self.extension_id.returnPressed.connect(self.register_host)
        register_button = QPushButton(icons.shield_icon(), tr("Register"))
        register_button.clicked.connect(self.register_host)
        remove_button = QPushButton(tr("Remove"))
        remove_button.clicked.connect(self.unregister_host)
        row.addWidget(self.extension_id, 1)
        row.addWidget(register_button)
        row.addWidget(remove_button)
        layout.addLayout(row)
        return box

    def _status_box(self) -> QGroupBox:
        box = QGroupBox(tr("Registration"))
        layout = QVBoxLayout(box)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr("Browser"), tr("Status")])
        self.tree.setRootIsDecorated(False)
        self.tree.setMaximumHeight(150)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        layout.addWidget(self.tree)
        return box

    # ------------------------------------------------------------------ actions

    def _copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.copied.setText(tr("Copied:") + " " + text)

    def refresh_status(self) -> None:
        self.tree.clear()
        try:
            state = register.status()
        except OSError as exc:  # pragma: no cover - Windows only feature
            log.info("cannot read the registration: %s", exc)
            state = {}
        for browser, manifest in state.items():
            item = QTreeWidgetItem([
                browser_label(browser),
                manifest or tr("Not registered yet"),
            ])
            item.setToolTip(1, manifest or "")
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0)

    def register_host(self) -> None:
        extension_id = self.extension_id.text().strip()
        ids = [extension_id] if extension_id else []
        # Firefox's id is fixed and known, so it is always worth registering -
        # even when the user only pasted a Chromium one.
        if not any(register.is_gecko_id(i) for i in ids):
            ids.append(register.DEFAULT_GECKO_ID)
        if extension_id and not register.valid_extension_id(extension_id):
            QMessageBox.warning(
                self, tr("Browser integration"),
                tr("extension id, or a Firefox add-on id"),
            )
            return
        try:
            results = register.install(ids)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, tr("Browser integration"), str(exc))
            return
        if extension_id:
            self.settings.set("extension_id", extension_id)
        log.info("registered the native host: %s", results)
        self.refresh_status()

    def unregister_host(self) -> None:
        try:
            register.uninstall()
        except OSError as exc:  # pragma: no cover - Windows only feature
            QMessageBox.warning(self, tr("Browser integration"), str(exc))
            return
        self.refresh_status()
