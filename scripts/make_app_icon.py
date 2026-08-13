"""Render `packaging/boltdown.ico` from the same QPainter drawing the app uses.

    python scripts/make_app_icon.py

Qt has no ICO *writer*, so the container is assembled here: a Vista-era ICO is
just a directory of PNG blobs, which keeps the alpha channel intact at every
size and avoids checking a binary asset into the repository by hand.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.icons import app_icon  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(size: int) -> bytes:
    pixmap = app_icon().pixmap(size, size)
    if pixmap.width() != size:
        pixmap = pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    # The QByteArray has to outlive the buffer: handing QBuffer a temporary
    # crashes as soon as Python frees it.
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not pixmap.save(buffer, "PNG"):
        raise RuntimeError(f"could not render the {size}px icon")
    buffer.close()
    return bytes(storage)


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR + one ICONDIRENTRY per image, then the PNG payloads."""
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    offset = len(header) + 16 * len(images)
    entries, payloads = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256
            0 if size >= 256 else size,
            0,          # palette colours (0 = truecolour)
            0,          # reserved
            1,          # colour planes
            32,         # bits per pixel
            len(data),
            offset,
        )
        payloads += data
        offset += len(data)
    return header + entries + payloads


def main() -> int:
    app = QApplication.instance() or QApplication([])
    target = PROJECT_ROOT / "packaging" / "boltdown.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_ico([(size, png_bytes(size)) for size in SIZES]))
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(SIZES)} sizes)")
    _ = app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
