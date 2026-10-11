"""The Qt binding itself: a release that leaks None would crash the app.

PySide6 6.12.0 dropped a reference to None on nearly every call that
returns nothing. Python 3.12+ never frees None (it is immortal), but on 3.11
every paint of the list took a little off its count until the process died
"deallocating None" - after all the tests had passed, or after hours in a
window. pyproject.toml holds 6.12 back on 3.11; this checks the binding that
is actually installed, so a new release with the same fault is caught here
rather than by users.
"""

from __future__ import annotations

import sys

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QPainter, QPixmap  # noqa: E402

from .test_gui import qapp  # noqa: E402,F401 - fixture


@pytest.mark.skipif(sys.version_info >= (3, 12), reason="None is immortal from 3.12")
def test_calls_that_return_nothing_do_not_eat_references_to_none(qapp):
    pixmap = QPixmap(40, 20)
    painter = QPainter(pixmap)
    try:
        before = sys.getrefcount(None)
        for _ in range(200):
            painter.save()
            painter.setPen(Qt.GlobalColor.red)
            painter.drawRoundedRect(QRectF(0, 0, 10, 10), 3, 3)
            painter.restore()
        lost = before - sys.getrefcount(None)
    finally:
        painter.end()
    # A few references may come and go with the interpreter's own work;
    # the broken binding loses 800 here.
    assert lost < 50, (
        f"PySide6 {PySide6.__version__} lost {lost} references to None in 800 "
        "calls - a long-running window would crash; see pyproject.toml"
    )
