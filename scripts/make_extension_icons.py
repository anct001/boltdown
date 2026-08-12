"""Render the extension's PNG icons from the same QPainter drawing the app uses.

    python scripts/make_extension_icons.py

Keeps the browser button and the desktop app visually identical without
checking hand-edited bitmaps into the repository.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.icons import app_icon  # noqa: E402

SIZES = (16, 32, 48, 128)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    target = PROJECT_ROOT / "extension" / "icons"
    target.mkdir(parents=True, exist_ok=True)

    icon = app_icon()
    for size in SIZES:
        pixmap = icon.pixmap(size, size)
        if pixmap.width() != size:
            pixmap = pixmap.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        path = target / f"icon{size}.png"
        if not pixmap.save(str(path), "PNG"):
            print(f"failed to write {path}", file=sys.stderr)
            return 1
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    _ = app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
