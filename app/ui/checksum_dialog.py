"""Hash a finished file and compare it with the value the site published.

Hashing a 4 GB download takes seconds, so it runs on a worker thread and
reports progress; the dialog stays usable and can be cancelled. The comparison
is deliberately forgiving about how people paste checksums - upper case, extra
spaces, or the `<hash>  <filename>` line straight out of a .sha256 file.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..util.fmt import human_size
from . import theme
from .i18n import tr

ALGORITHMS = ("sha256", "md5", "sha1")
BLOCK = 1 << 20


def normalise(value: str) -> str:
    """`ABCD1234  file.zip` -> `abcd1234`."""
    return value.strip().split()[0].lower() if value.strip() else ""


def hash_file(
    path: Path,
    algorithm: str = "sha256",
    *,
    on_progress=None,
    stop: threading.Event | None = None,
) -> str | None:
    """Digest `path`; None if it was cancelled."""
    digest = hashlib.new(algorithm)
    total = path.stat().st_size or 1
    done = 0
    with open(path, "rb") as handle:
        while True:
            if stop is not None and stop.is_set():
                return None
            block = handle.read(BLOCK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if on_progress is not None:
                on_progress(done * 100 // total)
    return digest.hexdigest()


class _Worker(QObject):
    finished = Signal(str)
    failed = Signal(str)
    progressed = Signal(int)


class ChecksumDialog(QDialog):
    def __init__(self, path: Path, parent=None) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(tr("Verify checksum"))
        self.setMinimumWidth(560)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.algorithm = QComboBox()
        for name in ALGORITHMS:
            self.algorithm.addItem(name.upper(), name)
        self.result = QLineEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText(tr("not computed yet"))
        self.expected = QLineEdit()
        self.expected.setPlaceholderText(tr("paste the value from the download page"))
        self.expected.textChanged.connect(self._compare)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.verdict = QLabel("")
        self.verdict.setTextFormat(Qt.TextFormat.PlainText)

        name = QLabel(f"{self.path.name}  ({human_size(self._size())})")
        name.setTextFormat(Qt.TextFormat.PlainText)

        form = QFormLayout()
        form.addRow(tr("File:"), name)
        form.addRow(tr("Algorithm:"), self.algorithm)
        form.addRow(tr("Result:"), self.result)
        form.addRow(tr("Expected:"), self.expected)

        self.compute = QPushButton(tr("Compute"))
        self.compute.clicked.connect(self.start)
        self.copy = QPushButton(tr("Copy"))
        self.copy.setEnabled(False)
        self.copy.clicked.connect(self._copy)
        tools = QHBoxLayout()
        tools.addWidget(self.compute)
        tools.addWidget(self.copy)
        tools.addStretch(1)
        tools.addWidget(self.verdict, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(tools)
        layout.addWidget(self.progress)
        layout.addWidget(buttons)

        self._worker = _Worker(self)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.progressed.connect(self.progress.setValue)

    def _size(self) -> int | None:
        try:
            return self.path.stat().st_size
        except OSError:
            return None

    # --------------------------------------------------------------- hashing

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.path.exists():
            self._on_failed(tr("The file no longer exists."))
            return
        algorithm = self.algorithm.currentData()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.compute.setEnabled(False)
        self.result.clear()
        self._stop.clear()

        def run() -> None:
            try:
                digest = hash_file(
                    self.path, algorithm,
                    on_progress=self._worker.progressed.emit, stop=self._stop,
                )
            except OSError as exc:
                self._worker.failed.emit(str(exc))
                return
            if digest is not None:
                self._worker.finished.emit(digest)

        self._thread = threading.Thread(target=run, name="checksum", daemon=True)
        self._thread.start()

    def _on_finished(self, digest: str) -> None:
        self.result.setText(digest)
        self.progress.setVisible(False)
        self.compute.setEnabled(True)
        self.copy.setEnabled(True)
        self._compare()

    def _on_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.compute.setEnabled(True)
        self.verdict.setText(message)

    def _compare(self) -> None:
        expected = normalise(self.expected.text())
        actual = self.result.text().strip().lower()
        if not expected or not actual:
            self.verdict.setText("")
            return
        palette = theme.current()
        match = expected == actual
        self.verdict.setText(tr("Match") if match else tr("Does NOT match"))
        colour = palette.success if match else palette.danger
        self.verdict.setStyleSheet(f"color: {colour}; font-weight: 600;")

    def _copy(self) -> None:
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(self.result.text())

    def closeEvent(self, event) -> None:
        self._stop.set()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # Cancel and Esc close the dialog without a close event, and hashing a
        # 10 GB file would otherwise keep the disk busy for nothing.
        self._stop.set()
        super().done(result)
