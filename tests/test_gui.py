"""GUI tests - run headless via the Qt `offscreen` platform plugin."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.core.task import TaskState  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.storage.settings import Settings  # noqa: E402
from app.ui import i18n  # noqa: E402
from app.ui.add_url_dialog import AddUrlDialog  # noqa: E402
from app.ui.controller import Controller, DownloadItem  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.progress_dialog import ProgressDialog, SegmentBar, SpeedGraph  # noqa: E402
from app.ui.settings_dialog import SettingsDialog  # noqa: E402
from app.ui.task_model import (  # noqa: E402
    COL_NAME,
    COL_SIZE,
    COL_STATUS,
    DownloadTableModel,
)

from .conftest import make_payload, sha256


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def stack(qapp, tmp_path):
    """A controller wired to a throwaway database, plus its settings."""
    db = Database(tmp_path / "gui.db")
    settings = Settings(db)
    settings.set("download_dir", str(tmp_path / "dl"))
    controller = Controller(db, settings)
    controller.start()
    try:
        yield controller, settings, db
    finally:
        controller.shutdown()
        db.close()


def pump(app, predicate, timeout: float = 60.0) -> bool:
    """Spin the Qt event loop until `predicate` holds (or we give up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


# --------------------------------------------------------------- controller


def test_download_lifecycle_through_the_gui(qapp, server, stack):
    controller, settings, db = stack
    data = make_payload(1_200_000, seed=500)
    server.add_file("gui.bin", data)

    model = DownloadTableModel(controller)
    item = controller.add(server.url_for("gui.bin"))

    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    assert model.rowCount() == 1
    assert model.data(model.index(0, COL_NAME)) == "gui.bin"
    assert model.data(model.index(0, COL_STATUS)) == i18n.tr("completed")
    assert model.data(model.index(0, COL_SIZE)) == "1.14 MB"
    assert sha256(item.path) == sha256(data)

    row = db.get_download(item.db_id)
    assert row["state"] == "completed"
    assert row["downloaded"] == len(data)


def test_pause_and_resume_from_the_controller(qapp, server, stack):
    controller, settings, db = stack
    data = make_payload(2 * 1024 * 1024, seed=501)
    server.add_file("gui-pause.bin", data)
    controller.engine.set_speed_limit(900_000)

    item = controller.add(server.url_for("gui-pause.bin"))
    assert pump(qapp, lambda: item.downloaded > 300_000)
    controller.pause_item(item.db_id)
    assert pump(qapp, lambda: item.state is TaskState.PAUSED)
    paused_at = item.downloaded
    assert 0 < paused_at < len(data)

    controller.engine.set_speed_limit(None)
    controller.start_item(item.db_id)
    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    assert sha256(item.path) == sha256(data)


def test_failed_download_surfaces_the_error(qapp, server, stack):
    controller, _settings, _db = stack
    item = controller.add(f"{server.base_url}/no-such-file.bin")
    assert pump(qapp, lambda: item.state is TaskState.ERROR)
    assert "404" in (item.error or "")


def test_items_are_restored_from_the_database(qapp, server, tmp_path):
    db = Database(tmp_path / "restore.db")
    settings = Settings(db)
    first = Controller(db, settings)
    first.start()
    try:
        first.add("http://example.invalid/a.zip", start_now=False)
        first.add("http://example.invalid/b.zip", start_now=False)
    finally:
        first.shutdown()

    second = Controller(db, settings)
    second.start()
    try:
        names = sorted(i.filename for i in second.items())
        assert names == ["a.zip", "b.zip"]
        assert all(i.state is TaskState.PAUSED for i in second.items())
    finally:
        second.shutdown()
        db.close()


def test_model_shows_items_that_existed_before_it(qapp, stack):
    """The window is built after restore(); those rows must still appear."""
    controller, _settings, _db = stack
    controller.add("http://example.invalid/early.zip", start_now=False)
    model = DownloadTableModel(controller)
    assert model.rowCount() == 1
    assert model.data(model.index(0, COL_NAME)) == "early.zip"

    controller.add("http://example.invalid/later.zip", start_now=False)
    assert model.rowCount() == 2


def test_remove_deletes_row_and_file(qapp, server, stack):
    controller, _settings, db = stack
    data = make_payload(80_000, seed=502)
    server.add_file("gui-del.bin", data)
    item = controller.add(server.url_for("gui-del.bin"))
    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    path = item.path
    assert path.exists()

    controller.remove(item.db_id, delete_file=True)
    assert db.get_download(item.db_id) is None
    assert not path.exists()
    assert controller.item(item.db_id) is None


# -------------------------------------------------------------------- model


def _item(**kw) -> DownloadItem:
    defaults = dict(db_id=1, url="http://h/a.mp4", filename="a.mp4", save_path="d")
    defaults.update(kw)
    return DownloadItem(**defaults)


def test_status_text_reflects_state():
    downloading = _item(state=TaskState.DOWNLOADING, size=1000, downloaded=250)
    assert DownloadTableModel.status_text(downloading) == "25.0%"
    failed = _item(state=TaskState.ERROR, error="HTTP 404")
    assert "404" in DownloadTableModel.status_text(failed)
    assert DownloadTableModel.status_text(_item(state=TaskState.QUEUED)) == i18n.tr("queued")


def test_category_filter(qapp, stack):
    controller, _settings, _db = stack
    model = DownloadTableModel(controller)
    from app.ui.task_model import DownloadFilterProxy

    proxy = DownloadFilterProxy()
    proxy.setSourceModel(model)

    controller.add("http://example.invalid/movie.mp4", start_now=False)
    controller.add("http://example.invalid/song.mp3", start_now=False)
    controller.add("http://example.invalid/pack.zip", start_now=False)

    assert proxy.rowCount() == 3
    proxy.set_filter("category", "Video")
    assert proxy.rowCount() == 1
    proxy.set_filter("category", "Music")
    assert proxy.rowCount() == 1
    proxy.set_filter("all")
    proxy.set_search("pack")
    assert proxy.rowCount() == 1
    proxy.set_search("")
    proxy.set_filter("finished")
    assert proxy.rowCount() == 0


# ------------------------------------------------------------------ widgets


def test_main_window_actions_follow_selection(qapp, stack):
    controller, settings, _db = stack
    window = MainWindow(controller, settings)
    try:
        controller.add("http://example.invalid/x.zip", start_now=False)
        qapp.processEvents()
        assert window.model.rowCount() == 1
        assert not window.action_delete.isEnabled()

        window.table.selectRow(0)
        qapp.processEvents()
        assert window.action_delete.isEnabled()
        assert window.action_resume.isEnabled()
        assert not window.action_pause.isEnabled()
    finally:
        window._ticker.stop()
        window.deleteLater()


def test_main_window_accepts_dropped_urls(qapp, stack, monkeypatch):
    controller, settings, _db = stack
    settings.set("ask_before_download", False)
    window = MainWindow(controller, settings)
    added: list[str] = []
    monkeypatch.setattr(
        controller, "add", lambda url, **kw: added.append(url), raising=False
    )
    try:
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QDropEvent

        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/a.iso")])
        event = QDropEvent(
            QPoint(10, 10), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        window.dropEvent(event)
        assert added == ["https://example.com/a.iso"]
    finally:
        window._ticker.stop()
        window.deleteLater()


def test_add_url_dialog_validates_and_returns_options(qapp, stack, monkeypatch):
    _controller, settings, _db = stack
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **kw: warnings.append(args[-1])
    )

    dialog = AddUrlDialog(settings, url="not a url")
    dialog._accept_now()
    assert warnings and dialog.result() != AddUrlDialog.DialogCode.Accepted

    dialog.url_edit.setText("https://example.com/setup.exe")
    dialog.connections.setValue(12)
    dialog.limit.setText("500k")
    dialog._accept_later()
    options = dialog.options()
    assert options["url"] == "https://example.com/setup.exe"
    assert options["connections"] == 12
    assert options["speed_limit"] == 500 * 1024
    assert options["start_now"] is False
    assert dialog.category_label.text() == i18n.tr("Programs")
    dialog.deleteLater()


def test_add_url_dialog_offers_quality_only_for_streams(qapp, stack):
    _controller, settings, _db = stack
    dialog = AddUrlDialog(settings, url="https://example.com/setup.exe")
    assert not dialog.video_box.isEnabled()

    dialog.url_edit.setText("https://cdn.example.com/vod/ep-7/master.m3u8")
    assert dialog.video_box.isEnabled()
    assert "HLS" in dialog.media_hint.text() or dialog.media_hint.text()
    # The category preview uses the name the media pipeline would choose.
    assert dialog.category_label.text() == i18n.tr("Video")

    dialog.quality.setCurrentIndex(dialog.quality.findData(720))
    dialog.audio_only.setChecked(True)
    dialog._accept_now()
    options = dialog.options()
    assert options["max_height"] == 720
    assert options["audio_only"] is True
    dialog.deleteLater()


def test_settings_dialog_writes_through(qapp, stack):
    _controller, settings, _db = stack
    dialog = SettingsDialog(settings)
    dialog.connections.setValue(24)
    dialog.concurrent.setValue(5)
    dialog.limit.setText("2M")
    dialog.quality.setCurrentIndex(dialog.quality.findData(1080))
    dialog.ffmpeg_edit.setText(r"C:\tools\ffmpeg.exe")
    dialog._save()
    assert settings.connections == 24
    assert settings.max_concurrent == 5
    assert settings.speed_limit == 2 * 1024 * 1024
    assert settings.video_quality == 1080
    assert settings.ffmpeg_path == r"C:\tools\ffmpeg.exe"
    dialog.deleteLater()


def test_controller_names_a_stream_before_it_starts(qapp, stack, tmp_path):
    controller, _settings, _db = stack
    item = controller.add(
        "https://cdn.example.com/vod/ep-7/master.m3u8",
        save_dir=tmp_path, start_now=False, max_height=720,
    )
    assert item.filename == "ep-7.mp4"
    assert item.is_media and item.max_height == 720
    request = controller._build_request(item)
    assert request.is_media and request.max_height == 720


def test_progress_dialog_renders_live_state(qapp, stack):
    controller, _settings, _db = stack
    item = _item(
        state=TaskState.DOWNLOADING, size=1_000_000, downloaded=250_000,
        speed=512_000, eta=1.5,
        segments=[(0, 125_000, 499_999), (500_000, 625_000, 999_999)],
    )
    dialog = ProgressDialog(controller, item)
    try:
        assert dialog.bar.value() == 25
        assert "244.14 KB" in dialog.transferred.text()
        assert dialog.toggle_button.text() == i18n.tr("Pause")
        # Widgets must paint without a display attached.
        pixmap = QPixmap(dialog.size())
        dialog.render(pixmap)
    finally:
        dialog.close()
        dialog.deleteLater()


def test_speed_graph_and_segment_bar_paint(qapp):
    graph = SpeedGraph()
    graph.resize(200, 90)
    for value in (0, 1000, 500_000, 250_000):
        graph.add_sample(value)
    graph.render(QPixmap(graph.size()))

    bar = SegmentBar()
    bar.resize(200, 26)
    bar.set_segments([(0, 400, 999), (1000, 1000, 1999)], 2000)
    bar.render(QPixmap(bar.size()))
    graph.deleteLater()
    bar.deleteLater()


# ----------------------------------------------------------- browser integration


def test_ipc_bridge_validates_messages(qapp):
    from app.ui.ipc_bridge import IpcBridge

    bridge = IpcBridge()
    captured: list[dict] = []
    bridge.downloadRequested.connect(captured.append)

    ping = bridge.handle({"type": "ping"})
    assert ping["ok"] is True and ping["app"] == "Boltdown"

    accepted = bridge.handle({"type": "download", "url": "https://h/a.zip"})
    assert accepted["ok"] is True
    qapp.processEvents()
    assert captured and captured[0]["url"] == "https://h/a.zip"

    assert bridge.handle({"type": "download", "url": "file:///etc/passwd"})["ok"] is False
    assert bridge.handle({"type": "download"})["ok"] is False
    assert bridge.handle({"type": "nonsense"})["ok"] is False
    bridge.deleteLater()


def test_captured_link_becomes_a_download(qapp, stack):
    controller, settings, _db = stack
    settings.set("ask_before_download", False)
    window = MainWindow(controller, settings)
    try:
        window.handle_ipc_download({
            "type": "download",
            "url": "http://example.invalid/installer.exe",
            "filename": "installer.exe",
            "referer": "http://example.invalid/page",
            "cookie": "session=abc",
            "user_agent": "TestAgent/1.0",
        })
        qapp.processEvents()
        items = controller.items()
        assert len(items) == 1
        assert items[0].cookie == "session=abc"
        assert items[0].referer == "http://example.invalid/page"
        assert items[0].user_agent == "TestAgent/1.0"
    finally:
        window._ticker.stop()
        window.deleteLater()


def test_streaming_links_go_to_the_media_pipeline(qapp, stack):
    controller, settings, _db = stack
    settings.set("ask_before_download", False)
    settings.set("video_quality", 720)
    window = MainWindow(controller, settings)
    notices: list[tuple[str, str]] = []
    window._notify = lambda title, body: notices.append((title, body))
    added: list = []
    controller.add = lambda **kwargs: added.append(kwargs)
    try:
        window.handle_ipc_download({
            "type": "media", "url": "https://h/live/vod-42/master.m3u8",
            "filename": "master.m3u8", "streaming": True,
        })
        qapp.processEvents()
        assert added and added[0]["max_height"] == 720
        # The playlist file name the browser guessed must not stick.
        assert added[0]["filename"] is None
        assert notices and notices[0][0] == i18n.tr("Downloading video")
    finally:
        window._ticker.stop()
        window.deleteLater()


def test_second_instance_hands_over_its_urls(qapp, stack, tmp_path, monkeypatch):
    """`python -m app <url>` while the app runs must not open a second window."""
    from app.ipc import endpoint
    from app.ui.ipc_bridge import IpcBridge

    monkeypatch.setenv("BOLTDOWN_HOME", str(tmp_path / "ipc"))
    (tmp_path / "ipc").mkdir()
    controller, settings, _db = stack
    settings.set("ask_before_download", False)
    window = MainWindow(controller, settings)
    bridge = IpcBridge()
    bridge.downloadRequested.connect(window.handle_ipc_download)
    bridge.showRequested.connect(window.handle_ipc_show)
    server = endpoint.IpcServer(bridge.handle)
    server.start()
    try:
        reply = endpoint.send(
            {"type": "show", "urls": ["http://example.invalid/from-cli.bin"]}
        )
        assert reply == {"ok": True}
        assert pump(qapp, lambda: len(controller.items()) == 1, timeout=5)
        assert controller.items()[0].filename == "from-cli.bin"
    finally:
        server.stop()
        bridge.deleteLater()
        window._ticker.stop()
        window.deleteLater()


def test_translation_falls_back_to_english():
    i18n.set_language("vi")
    assert i18n.tr("Add URL") == "Thêm URL"
    assert i18n.tr("A string nobody translated") == "A string nobody translated"
    i18n.set_language("en")
    assert i18n.tr("Add URL") == "Add URL"
    i18n.set_language("vi")


def test_a_typed_file_name_reaches_the_disk(qapp, server, stack, tmp_path):
    """The bug: the Add URL dialog's name only changed the list, not the file."""
    controller, _settings, db = stack
    data = make_payload(80_000, seed=940)
    url = server.add_file("named/from-server.bin", data, disposition="from-server.bin")

    item = controller.add(url, save_dir=tmp_path, filename="báo cáo quý 4.bin")
    assert item.name_locked
    assert db.get_download(item.db_id)["name_locked"] == 1
    request = controller._build_request(item)
    assert request.filename == "báo cáo quý 4.bin"

    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    assert sha256(tmp_path / "báo cáo quý 4.bin") == sha256(data)


def test_a_probed_name_is_locked_so_a_resume_keeps_it(qapp, server, stack, tmp_path):
    controller, _settings, db = stack
    data = make_payload(60_000, seed=941)
    url = server.add_file("named/plain.bin", data)

    item = controller.add(url, save_dir=tmp_path)
    assert not item.name_locked, "nothing typed - the probe should decide"
    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    # Once a runner has reported a name, the .part file carries it.
    assert item.name_locked
    assert db.get_download(item.db_id)["name_locked"] == 1
    assert controller._build_request(item).filename == "plain.bin"


def test_restoring_remembers_whether_the_name_was_settled(qapp, stack, tmp_path):
    controller, settings, db = stack
    typed = controller.add("https://example.com/a.bin", save_dir=tmp_path,
                           filename="của tôi.bin", start_now=False)
    guessed = controller.add("https://example.com/b.bin", save_dir=tmp_path,
                             start_now=False)

    fresh = Controller(db, settings)
    fresh.restore()
    restored = {i.db_id: i for i in fresh.items()}
    assert restored[typed.db_id].name_locked
    assert not restored[guessed.db_id].name_locked


# ------------------------------- the CLI talks to the window, not past it


def test_remote_commands_run_on_the_gui_thread(qapp):
    """`--remote-list` reads the download table and writes to SQLite.

    Both belong to the GUI thread. Answering the socket thread there directly
    was a race - and sqlite3 objects are not fond of it either.
    """
    import threading

    from PySide6.QtCore import QThread

    from app.ipc.protocol import TYPE_LIST, TYPE_PAUSE
    from app.ui.ipc_bridge import IpcBridge

    bridge = IpcBridge()
    seen: list[QThread] = []

    def snapshot():
        seen.append(QThread.currentThread())
        return [{"id": 1, "name": "a.bin"}]

    def control(action, db_id):
        seen.append(QThread.currentThread())
        return True

    bridge.snapshot = snapshot
    bridge.control = control
    answers: list[dict] = []

    def from_the_socket_thread():
        answers.append(bridge.handle({"type": TYPE_LIST}))
        answers.append(bridge.handle({"type": TYPE_PAUSE, "id": 1}))

    worker = threading.Thread(target=from_the_socket_thread)
    worker.start()
    deadline = time.monotonic() + 5
    while worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()      # the GUI thread, doing its job
    worker.join(5)

    assert answers == [
        {"ok": True, "downloads": [{"id": 1, "name": "a.bin"}]},
        {"ok": True},
    ]
    assert seen and all(t is qapp.thread() for t in seen), (
        "the controller was touched from the socket thread"
    )


def test_a_wedged_window_does_not_hang_the_command_line(qapp, monkeypatch):
    import threading

    from app.ipc.protocol import TYPE_LIST
    from app.ui import ipc_bridge

    monkeypatch.setattr(ipc_bridge, "GUI_TIMEOUT", 0.2)
    bridge = ipc_bridge.IpcBridge()
    bridge.snapshot = lambda: []

    answer: list[dict] = []
    # Nobody processes events here, so the call can only time out.
    worker = threading.Thread(
        target=lambda: answer.append(bridge.handle({"type": TYPE_LIST}))
    )
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "the CLI would have waited for ever"
    assert answer and answer[0]["ok"] is False


def test_a_remote_command_still_works_from_the_gui_thread_itself(qapp):
    from app.ipc.protocol import TYPE_LIST
    from app.ui.ipc_bridge import IpcBridge

    bridge = IpcBridge()
    bridge.snapshot = lambda: [{"id": 7}]
    assert bridge.handle({"type": TYPE_LIST}) == {"ok": True, "downloads": [{"id": 7}]}
