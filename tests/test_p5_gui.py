"""The P5 windows: scheduler, site grabber, queue wiring in the main window."""

from __future__ import annotations

import time
from concurrent.futures import Future

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTime  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.core.schedule import PostAction, WEEKDAYS, days_of  # noqa: E402
from app.core.task import TaskState  # noqa: E402
from app.grabber.crawler import CrawlResult, FoundFile  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.storage.settings import Settings  # noqa: E402
from app.ui import i18n  # noqa: E402
from app.ui.controller import Controller  # noqa: E402
from app.ui.grabber_dialog import GrabberDialog, parse_extensions  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.queue_dialog import SchedulerDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def stack(qapp, tmp_path):
    db = Database(tmp_path / "p5.db")
    settings = Settings(db)
    settings.set("download_dir", str(tmp_path / "dl"))
    controller = Controller(db, settings)
    controller.start()
    try:
        yield controller, settings, db
    finally:
        controller.shutdown()
        db.close()


def pump(app, predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


# ------------------------------------------------------------ scheduler window


def test_scheduler_dialog_saves_a_schedule(qapp, stack):
    controller, _settings, db = stack
    queue_id = controller.create_queue("Night")
    dialog = SchedulerDialog(controller, db)
    try:
        assert dialog.queue_list.count() == 1
        assert dialog.current_queue_id() == queue_id

        dialog.enabled.setChecked(True)
        dialog.start_time.setTime(QTime(2, 30))
        dialog.use_stop.setChecked(True)
        dialog.stop_time.setTime(QTime(6, 0))
        for day, box in enumerate(dialog.days):
            box.setChecked(day < 5)  # weekdays only
        dialog.concurrent.setValue(3)
        dialog.on_complete.setCurrentIndex(
            dialog.on_complete.findData(PostAction.SHUTDOWN.value)
        )
        dialog.save_current()

        row = db.get_schedule(queue_id)
        assert row["start_at"] == "02:30" and row["stop_at"] == "06:00"
        assert row["days_mask"] == WEEKDAYS and row["enabled"] == 1
        assert row["on_complete"] == "shutdown"
        assert controller.queue(queue_id).max_concurrent == 3
        assert days_of(row["days_mask"]) == [0, 1, 2, 3, 4]
    finally:
        dialog.deleteLater()


def test_changing_the_start_time_clears_the_last_run(qapp, stack):
    """Otherwise "already ran today" would swallow the new time."""
    controller, _settings, db = stack
    queue_id = controller.create_queue("Night")
    db.save_schedule(queue_id, start_at="02:00", last_run=1_800_000_000.0)

    dialog = SchedulerDialog(controller, db)
    try:
        dialog.start_time.setTime(QTime(4, 0))
        dialog.save_current()
        assert db.get_schedule(queue_id)["last_run"] is None

        # Saving again without touching the time keeps whatever was recorded.
        db.save_schedule(queue_id, start_at="04:00", last_run=123.0)
        dialog.save_current()
        assert db.get_schedule(queue_id)["last_run"] == 123.0
    finally:
        dialog.deleteLater()


def test_scheduler_dialog_starts_and_stops_the_queue(qapp, stack):
    controller, _settings, db = stack
    queue_id = controller.create_queue("Night")
    controller.add("https://example.com/a.zip", queue_id=queue_id)
    dialog = SchedulerDialog(controller, db)
    try:
        dialog.start_queue()
        assert controller.is_queue_running(queue_id)
        assert not dialog.start_button.isEnabled()
        dialog.stop_queue()
        assert not controller.is_queue_running(queue_id)
    finally:
        dialog.deleteLater()


def test_deleting_a_queue_from_the_dialog(qapp, stack, monkeypatch):
    controller, _settings, db = stack
    queue_id = controller.create_queue("Night")
    db.save_schedule(queue_id, start_at="02:00")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    dialog = SchedulerDialog(controller, db)
    try:
        dialog.delete_queue()
        assert controller.queues() == []
        assert db.list_schedules() == []
        assert dialog.queue_list.count() == 0
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------- site grabber


@pytest.mark.parametrize(
    "text, expected",
    [
        ("jpg, png", ("jpg", "png")),
        (".JPG;.PnG", ("jpg", "png")),
        ("", ()),
        ("  ,  ", ()),
    ],
)
def test_parse_extensions(text, expected):
    assert parse_extensions(text) == expected


def test_grabber_builds_options_from_the_form(qapp, stack):
    controller, settings, _db = stack
    dialog = GrabberDialog(controller, settings)
    try:
        dialog.preset.setCurrentIndex(1)  # Images
        assert "jpg" in dialog.extensions.text()
        dialog.depth.setValue(2)
        dialog.max_pages.setValue(7)
        dialog.pattern.setText("/gallery/")
        dialog.exclude.setText("thumb")
        dialog.same_host.setChecked(False)

        options = dialog.options()
        assert options.depth == 2 and options.max_pages == 7
        assert "jpg" in options.extensions and options.same_host is False
        assert options.pattern == "/gallery/" and options.exclude == "thumb"
    finally:
        dialog.deleteLater()


def test_grabber_rejects_a_bad_url(qapp, stack, monkeypatch):
    controller, settings, _db = stack
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[-1]))
    dialog = GrabberDialog(controller, settings)
    try:
        dialog.url.setText("not a url")
        dialog.scan()
        assert warnings and dialog.results.topLevelItemCount() == 0
    finally:
        dialog.deleteLater()


def _result(count: int = 3) -> CrawlResult:
    files = [
        FoundFile(
            url=f"https://example.com/gallery/{i}.jpg",
            name=f"{i}.jpg",
            referer="https://example.com/gallery/",
            extension="jpg",
            depth=0,
        )
        for i in range(count)
    ]
    return CrawlResult(files=files, pages_visited=2)


def test_grabber_lists_results_and_queues_the_checked_ones(qapp, stack, tmp_path):
    controller, settings, db = stack
    queue_id = controller.create_queue("Grabbed")
    dialog = GrabberDialog(controller, settings)
    try:
        future: Future = Future()
        future.set_result(_result())
        dialog._on_finished(future)
        assert dialog.results.topLevelItemCount() == 3
        assert dialog.add_button.isEnabled()
        assert "3" in dialog.status.text() and "2" in dialog.status.text()

        dialog.results.topLevelItem(0).setCheckState(0, dialog.results.topLevelItem(0).checkState(0).Unchecked)
        assert len(dialog.checked_files()) == 2

        dialog.queue_combo.setCurrentIndex(dialog.queue_combo.findData(queue_id))
        dialog.dir_edit.setText(str(tmp_path / "grab"))
        dialog.add_selected()

        items = controller.queue_items(queue_id)
        assert [i.filename for i in items] == ["1.jpg", "2.jpg"]
        assert all(i.state is TaskState.PAUSED for i in items), "queued, not started"
        assert all(i.referer == "https://example.com/gallery/" for i in items)
        assert db.get_download(items[0].db_id)["queue_id"] == queue_id
    finally:
        dialog.deleteLater()


def test_grabber_reports_a_failed_scan(qapp, stack, monkeypatch):
    controller, settings, _db = stack
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[-1]))
    dialog = GrabberDialog(controller, settings)
    try:
        future: Future = Future()
        future.set_exception(RuntimeError("network is down"))
        dialog._on_finished(future)
        assert warnings and "network is down" in warnings[0]
        assert not dialog.progress.isVisible()
    finally:
        dialog.deleteLater()


def test_grabber_scans_a_real_site_through_the_engine(qapp, stack, server):
    """End to end: the dialog drives the crawler on the engine's loop."""
    controller, settings, _db = stack
    server.add_file(
        "grab/index.html",
        b'<html><body><a href="a.zip">a</a><img src="b.png"></body></html>',
    )
    server.add_file("grab/a.zip", b"zip")
    server.add_file("grab/b.png", b"png")

    dialog = GrabberDialog(controller, settings)
    try:
        dialog.url.setText(server.url_for("grab/index.html"))
        dialog.scan()
        assert pump(qapp, lambda: dialog.results.topLevelItemCount() >= 2)
        names = {
            dialog.results.topLevelItem(i).text(0)
            for i in range(dialog.results.topLevelItemCount())
        }
        assert names == {"a.zip", "b.png"}
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------- main window


@pytest.fixture
def window(qapp, stack):
    controller, settings, _db = stack
    win = MainWindow(controller, settings)
    try:
        yield win, controller
    finally:
        win._ticker.stop()
        win.scheduler.stop()
        win.deleteLater()


def test_the_tree_mirrors_the_queues(window):
    win, controller = window
    assert win.queue_root.isHidden()

    queue_id = controller.create_queue("Night")
    controller.add("https://example.com/a.zip", queue_id=queue_id)
    win._rebuild_queue_nodes()
    assert not win.queue_root.isHidden()
    assert win.queue_root.childCount() == 1
    node = win.queue_root.child(0)
    assert "Night" in node.text(0) and "(1)" in node.text(0)

    # Selecting the node filters the table down to that queue.
    win._on_tree_selection(node, None)
    assert win.proxy.rowCount() == 1
    controller.add("https://example.com/loose.zip", start_now=False)
    assert win.proxy.rowCount() == 1


def test_moving_a_download_into_a_queue_from_the_menu(window):
    win, controller = window
    queue_id = controller.create_queue("Night")
    item = controller.add("https://example.com/a.zip", start_now=False)

    win._assign_queue([item], queue_id)
    assert controller.queue_items(queue_id) == [item]
    assert win.queue_root.child(0).text(0).endswith("(1)")

    win._assign_queue([item], None)
    assert controller.queue_items(queue_id) == []


def test_a_scheduled_action_is_carried_out_after_the_countdown(window, monkeypatch):
    win, controller = window
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(win, "_confirm_post_action", lambda action: True)
    monkeypatch.setattr(
        "app.util.power.apply",
        lambda action, delay=0, runner=None, on_exit=None: calls.append(
            (action.value, delay)
        ),
    )
    win._on_post_action(PostAction.SHUTDOWN.value, 1)
    assert calls == [("shutdown", 0)]


def test_a_cancelled_countdown_does_nothing(window, monkeypatch):
    win, _controller = window
    calls: list[str] = []
    monkeypatch.setattr(win, "_confirm_post_action", lambda action: False)
    monkeypatch.setattr(
        "app.util.power.apply",
        lambda action, **kw: calls.append(action.value),
    )
    win._on_post_action(PostAction.SHUTDOWN.value, 1)
    win._on_post_action(PostAction.NONE.value, 1)
    assert calls == []


def test_the_queue_scheduler_is_running_on_the_window(window):
    win, controller = window
    queue_id = controller.create_queue("Night")
    controller.add("https://example.com/a.zip", queue_id=queue_id)
    win.controller.db.save_schedule(queue_id, start_at="00:00")
    win.scheduler.tick()
    assert controller.is_queue_running(queue_id)
    assert i18n.tr("running") in win.queue_root.child(0).text(0)
