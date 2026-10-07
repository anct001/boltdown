"""The search box over the download list."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from app.ui.controller import DownloadItem  # noqa: E402
from app.ui.task_model import fold, matches_search, search_words  # noqa: E402

from .test_gui import qapp, stack  # noqa: E402,F401 - fixtures


def _item(**kw) -> DownloadItem:
    base = dict(db_id=1, url="https://cdn.example/files/q4.pdf", filename="Báo cáo quý 4.pdf",
                save_path="/tmp")
    base.update(kw)
    return DownloadItem(**base)


def test_accents_are_optional():
    assert fold("Báo cáo QUÝ 4 Đà Nẵng") == "bao cao quy 4 da nang"
    item = _item()
    for query in ("bao cao", "báo cáo", "BAO CAO QUY", "quý"):
        assert matches_search(item, search_words(query)), query


def test_every_word_has_to_match_somewhere():
    item = _item(referer="https://intranet.example/reports")
    assert matches_search(item, search_words("bao cao intranet"))
    assert matches_search(item, search_words("cdn pdf"))
    assert not matches_search(item, search_words("bao cao excel"))


def test_the_error_is_searchable():
    item = _item(error="HTTP 403: the server refused")
    assert matches_search(item, search_words("403"))


def test_an_empty_search_matches_everything():
    assert matches_search(_item(), search_words("   "))


@pytest.fixture
def window(qapp, stack):
    from app.ui.main_window import MainWindow

    controller, settings, _db = stack
    win = MainWindow(controller, settings)
    yield win
    win._ticker.stop()
    win.deleteLater()


def test_the_window_filters_counts_and_clears_on_escape(qapp, window):
    controller = window.controller
    controller.add("https://cdn.example/Báo cáo.pdf", filename="Báo cáo.pdf", start_now=False)
    controller.add("https://cdn.example/setup.exe", filename="setup.exe", start_now=False)
    controller.add("https://other.example/photo.jpg", filename="photo.jpg", start_now=False)
    qapp.processEvents()

    window.search.setText("bao cao")
    qapp.processEvents()
    assert window.proxy.rowCount() == 1
    assert window.search_count.text() == "1 / 3"

    window.search.setText("cdn")
    qapp.processEvents()
    assert window.proxy.rowCount() == 2

    QTest.keyClick(window.search, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert window.search.text() == ""
    assert window.proxy.rowCount() == 3
    assert window.search_count.text() == ""
