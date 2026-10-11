"""The list's looks and the conveniences around it."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget  # noqa: E402

from app.core.task import TaskState  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.task_model import friendly_time, matches_filter  # noqa: E402

from .test_gui import qapp, stack  # noqa: E402,F401 - fixtures


@pytest.fixture
def window(qapp, stack):
    from app.ui.main_window import MainWindow

    controller, settings, _db = stack
    win = MainWindow(controller, settings)
    win._notify = lambda *a: None
    yield win
    win._ticker.stop()
    win.remote.stop()
    win.deleteLater()


def settle(check, timeout: float = 3.0) -> bool:
    """The engine applies limits on its own thread; wait until it has."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return check()


def add(controller, name, state=TaskState.PAUSED, size=None):
    item = controller.add(f"https://x.example/{name}", filename=name, start_now=False)
    item.state, item.size = state, size
    return item


# --------------------------------------------------------------- the list


def test_dates_read_like_a_person_would_say_them(qapp):
    now = datetime(2026, 10, 11, 15, 0).timestamp()
    at = lambda **kw: (datetime(2026, 10, 11, 9, 30) - timedelta(**kw)).timestamp()  # noqa: E731
    assert friendly_time(at(), now) == "Hôm nay 09:30" or friendly_time(at(), now).endswith("09:30")
    assert "09:30" in friendly_time(at(days=1), now)
    assert friendly_time(at(days=1), now) != friendly_time(at(), now)
    assert friendly_time(at(days=40), now) == "01/09 09:30"
    assert friendly_time(datetime(2024, 3, 5, 8, 0).timestamp(), now) == "05/03/2024"


def test_the_sidebar_filters_are_one_function(qapp, stack):
    controller, _s, _db = stack
    done = add(controller, "a.zip", TaskState.COMPLETED, 10)
    paused = add(controller, "b.mp4", TaskState.PAUSED, 10)
    assert matches_filter(done, "finished") and not matches_filter(paused, "finished")
    assert matches_filter(paused, "unfinished")
    assert matches_filter(paused, "category", "Video")
    assert matches_filter(done, "all")


def test_the_sidebar_counts_what_each_entry_holds(window):
    controller = window.controller
    add(controller, "one.zip", TaskState.COMPLETED, 10)
    add(controller, "two.mp4")
    add(controller, "three.mp4")
    window._refresh_counts()
    from app.ui.task_model import COUNT_ROLE

    texts = {node.text(0): node.data(0, COUNT_ROLE) or "" for node in window._tree_nodes()}
    from app.ui.i18n import tr

    assert texts[tr("All downloads")] == 3
    assert texts[tr("Unfinished")] == 2
    assert texts[tr("Finished")] == 1
    assert texts[tr("Video")] == 2
    assert texts[tr("Music")] == ""  # nothing to count says nothing


def test_an_empty_list_says_how_to_start_and_a_search_says_it_found_nothing(window):
    title, hint = window._empty_text()
    assert "Ctrl+N" in hint
    add(window.controller, "film.mkv")
    window.search.setText("zzz-nothing")
    title, hint = window._empty_text()
    assert "zzz-nothing" in title


def test_unknown_sizes_are_a_dash_not_a_question(window):
    add(window.controller, "unknown.bin")
    model = window.model
    from app.ui.task_model import COL_SIZE

    row = next(r for r in range(model.rowCount()) if model.item_at(r).filename == "unknown.bin")
    assert model.data(model.index(row, COL_SIZE)) == "—"


@pytest.mark.parametrize("name", sorted(theme.THEMES))
def test_the_list_paints_in_every_theme(qapp, window, name):
    theme.apply(qapp, name)
    window.refresh_icons()
    add(window.controller, "half.iso", TaskState.DOWNLOADING, 1000).downloaded = 500
    add(window.controller, "done.pdf", TaskState.COMPLETED, 1000).downloaded = 1000
    window.resize(1000, 600)
    window.show()
    qapp.processEvents()
    assert not window.table.grab().isNull()


# ------------------------------------------------------------ the themes


def test_the_popular_themes_are_there():
    for name in ("catppuccin-mocha", "catppuccin-latte", "tokyo-night", "gruvbox"):
        assert name in theme.THEMES
    assert not theme.THEMES["catppuccin-latte"].dark


def test_a_solid_theme_after_glass_leaves_no_holes(qapp):
    win = QMainWindow()
    central = QWidget()
    QVBoxLayout(central).addStretch()
    win.setCentralWidget(central)
    win.resize(300, 200)
    win.show()
    try:
        theme.apply(qapp, "glass")
        qapp.processEvents()
        theme.apply(qapp, "nord")
        qapp.processEvents()
        assert not win.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        assert win.grab().toImage().pixelColor(250, 150).alpha() == 255
    finally:
        theme.apply(qapp, "dark")
        win.close()


# --------------------------------------------------------------- details


def test_the_details_panel_follows_the_selection(window):
    item = add(window.controller, "report.pdf", TaskState.ERROR, 2048)
    item.error = "HTTP 403: forbidden"
    window.show()
    row = next(r for r in range(window.proxy.rowCount())
               if window.proxy.index(r, 0).data(Qt.ItemDataRole.DisplayRole) == "report.pdf")
    window.table.setCurrentIndex(window.proxy.index(row, 0))
    panel = window.details
    assert panel.item is item
    assert panel.title.text() == "report.pdf"
    assert "403" in panel.error.text() and panel.error.isVisible()
    assert not panel.open_file.isEnabled()  # not finished: nothing to open


def test_f3_hides_the_panel_and_it_stays_hidden(window):
    window.action_details.setChecked(False)
    assert not window.details.isVisible()
    assert window.settings.get("details_visible") is False


def test_space_pauses_and_resumes_the_selection(window, monkeypatch):
    item = add(window.controller, "big.iso", TaskState.DOWNLOADING, 10)
    calls = []
    monkeypatch.setattr(window, "pause_selected", lambda: calls.append("pause"))
    monkeypatch.setattr(window, "resume_selected", lambda: calls.append("resume"))
    window.table.selectRow(0)
    window.toggle_selected()
    item.state = TaskState.PAUSED
    window.toggle_selected()
    assert calls == ["pause", "resume"]


# ----------------------------------------------------------- slow mode


def test_slow_mode_caps_the_speed_and_lets_go(window):
    controller = window.controller
    window.settings.update({"turtle_limit": 300_000, "speed_limit": None})
    bucket = controller.engine._global_bucket
    window.action_turtle.setChecked(True)
    assert settle(lambda: bucket.rate == 300_000), bucket.rate
    assert window.turtle_button.text()  # shows the cap while it is on

    # A lower saved limit wins; slow mode never raises a limit.
    controller.set_speed_limit(100_000)
    assert settle(lambda: bucket.rate == 100_000), bucket.rate

    controller.set_speed_limit(None)
    window.action_turtle.setChecked(False)
    assert settle(lambda: bucket.rate is None), bucket.rate
    assert window.turtle_button.text() == ""


def test_a_bandwidth_schedule_is_reapplied_when_slow_mode_changes(window):
    scheduler = window.scheduler
    applied = []
    window.controller.apply_speed_limit = lambda base=...: applied.append(base)
    scheduler.bandwidth_schedule = lambda: (type("S", (), {"covers": lambda self, now: True})(), 2_000_000)
    now = datetime.now()
    scheduler.apply_bandwidth(now)
    scheduler.apply_bandwidth(now)  # nothing changed: not applied twice
    window.settings.update({"turtle_mode": True, "turtle_limit": 50_000})
    scheduler.apply_bandwidth(now)
    assert applied == [2_000_000, 2_000_000]


# --------------------------------------------------------------- layout


def test_the_layout_comes_back_after_a_restart(qapp, stack):
    from app.ui.main_window import MainWindow
    from app.ui.task_model import COL_SIZE

    controller, settings, _db = stack
    first = MainWindow(controller, settings)
    try:
        first.resize(700, 500)
        first.table.sortByColumn(COL_SIZE, Qt.SortOrder.DescendingOrder)
        finished = next(n for n in first._tree_nodes() if n.data(0, Qt.ItemDataRole.UserRole + 10) == ("finished", ""))
        first.tree.setCurrentItem(finished)
        first.save_layout()
    finally:
        first._ticker.stop()
        first.remote.stop()
        first.deleteLater()

    second = MainWindow(controller, settings)
    try:
        header = second.table.horizontalHeader()
        assert header.sortIndicatorSection() == COL_SIZE
        assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        assert second.tree.currentItem().data(0, Qt.ItemDataRole.UserRole + 10) == ("finished", "")
        assert second.size().width() == 700
    finally:
        second._ticker.stop()
        second.remote.stop()
        second.deleteLater()


def test_a_damaged_saved_layout_is_ignored(window):
    window.settings.set("window_state", "{not json")
    assert window.restore_layout() is False


def test_a_long_folder_loses_its_middle_not_its_end(window):
    item = add(window.controller, "deep.bin", TaskState.PAUSED, 10)
    item.save_path = "/very/" + "long/" * 60 + "final-folder"
    window.resize(780, 560)
    window.show()
    window.details.show_item(item)
    label = window.details.fields["folder"]
    full = str(Path(item.save_path))  # backslashes on Windows
    shown = label.text()
    # How much survives depends on the font; what matters is that the
    # middle went, the end stayed, and the whole path is one hover away.
    assert "…" in shown and len(shown) < len(full)
    assert shown[-3:] == full[-3:]
    assert label.toolTip() == full
