"""Refresh download address: same file, new link, the bytes on disk kept."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from app.core.task import TaskState  # noqa: E402

from .conftest import make_payload, sha256  # noqa: E402
from .test_gui import pump, qapp, stack  # noqa: E402,F401 - fixtures


def _start_and_pause(qapp, controller, url):
    item = controller.add(url)
    assert pump(qapp, lambda: item.downloaded > 300_000), "never got going"
    controller.pause_item(item.db_id)
    assert pump(qapp, lambda: item.state is TaskState.PAUSED)
    return item


def test_an_expired_link_carries_on_from_a_new_address(qapp, server, stack):
    controller, _settings, db = stack
    data = make_payload(3 * 1024 * 1024 + 99, seed=71)
    server.add_file("expiring.bin", data)
    server.add_file("renewed.bin", data)
    old = server.url_for("expiring.bin", slow=150)
    item = _start_and_pause(qapp, controller, old)
    kept = item.downloaded

    # The signed link runs out: resuming with it now fails.
    del server.state.files["expiring.bin"]
    controller.start_item(item.db_id)
    assert pump(qapp, lambda: item.state is TaskState.ERROR)

    seen: list[int] = []
    controller.itemChanged.connect(
        lambda changed: seen.append(changed.downloaded)
        if changed is item and changed.state is TaskState.DOWNLOADING else None
    )
    new = server.url_for("renewed.bin")
    assert controller.refresh_address(item.db_id, new, cookie="fresh=1") is True
    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)

    assert item.filename == "expiring.bin", "the download keeps its name"
    assert sha256(item.path) == sha256(data)
    assert seen and min(seen) >= kept, "it started over instead of resuming"
    row = db.get_download(item.db_id)
    assert row["url"] == new
    headers = db.query("SELECT * FROM download_headers WHERE download_id = ?", (item.db_id,))
    assert headers[0]["cookie"] == "fresh=1"


def test_a_running_download_switches_address_without_losing_bytes(qapp, server, stack):
    controller, _settings, _db = stack
    data = make_payload(3 * 1024 * 1024 + 7, seed=72)
    server.add_file("live-a.bin", data)
    server.add_file("live-b.bin", data)
    item = controller.add(server.url_for("live-a.bin", slow=150))
    assert pump(qapp, lambda: item.downloaded > 300_000)

    assert controller.refresh_address(item.db_id, server.url_for("live-b.bin")) is True
    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    assert item.url.endswith("live-b.bin")
    assert sha256(item.path) == sha256(data)


def test_refresh_is_refused_where_it_cannot_apply(qapp, server, stack):
    controller, _settings, _db = stack
    data = make_payload(200_000, seed=73)
    url = server.add_file("done.bin", data)
    done = controller.add(url)
    assert pump(qapp, lambda: done.state is TaskState.COMPLETED)

    assert controller.can_refresh(done.db_id) is False
    assert controller.refresh_address(done.db_id, url) is False
    with pytest.raises(ValueError):
        controller.refresh_address(done.db_id, "file:///etc/passwd")
    stream = controller.add("https://example.invalid/live/master.m3u8", start_now=False)
    assert controller.can_refresh(stream.db_id) is False


# ---------------------------------------------------------------- the window


@pytest.fixture
def window(qapp, stack):
    from app.ui.main_window import MainWindow

    controller, settings, _db = stack
    win = MainWindow(controller, settings)
    win.notices = []
    win._notify = lambda title, body: win.notices.append(body)
    yield win
    win._ticker.stop()
    win.deleteLater()


def _paused(controller, url="https://files.example/expired.iso?sig=old", size=None):
    item = controller.add(url, start_now=False, referer="https://files.example/page")
    item.size = size
    return item


def test_the_next_captured_link_becomes_the_new_address(window, monkeypatch):
    controller = window.controller
    item = _paused(controller, size=1000)
    calls = []
    monkeypatch.setattr(controller, "refresh_address",
                        lambda db_id, url, **kw: calls.append((db_id, url, kw)) or True)
    window._awaiting_refresh = (item.db_id, time.monotonic() + 60)

    window.handle_ipc_download({
        "type": "download", "url": "https://files.example/expired.iso?sig=new",
        "cookie": "session=fresh", "size": 1000,
    })

    assert calls == [(item.db_id, "https://files.example/expired.iso?sig=new", {
        "referer": "https://files.example/page", "cookie": "session=fresh",
        "user_agent": None,
    })]
    assert len(controller.items()) == 1, "it must not become a second download"
    assert window._awaiting_refresh is None


def test_a_different_file_is_not_taken_for_the_new_address(window, monkeypatch):
    controller = window.controller
    item = _paused(controller, size=1000)
    monkeypatch.setattr(controller, "refresh_address",
                        lambda *a, **kw: pytest.fail("attached the wrong file"))
    window.settings.set("ask_before_download", False)
    monkeypatch.setattr(controller, "start_item", lambda db_id: None)
    window._awaiting_refresh = (item.db_id, time.monotonic() + 60)

    window.handle_ipc_download({"type": "download", "url": "https://x.example/other.zip",
                                "size": 5})

    assert len(controller.items()) == 2, "the other file is an ordinary download"
    assert window._awaiting_refresh is not None, "still waiting for the right one"


def test_a_stale_wait_is_forgotten(window, monkeypatch):
    controller = window.controller
    item = _paused(controller)
    monkeypatch.setattr(controller, "refresh_address",
                        lambda *a, **kw: pytest.fail("the wait had expired"))
    window._awaiting_refresh = (item.db_id, time.monotonic() - 1)
    assert window._take_refresh({"url": "https://files.example/new"}) is False
    assert window._awaiting_refresh is None


def test_the_dialog_wants_a_new_http_address(qapp, stack):
    from app.ui.refresh_dialog import RefreshDialog

    controller, _settings, _db = stack
    item = _paused(controller)
    dialog = RefreshDialog(item)
    dialog.new_url.setText(item.url)
    assert not dialog.ok_button.isEnabled(), "the same address is no refresh"
    dialog.new_url.setText("ftp://elsewhere/file")
    assert not dialog.ok_button.isEnabled()
    dialog.new_url.setText("https://files.example/expired.iso?sig=new")
    assert dialog.ok_button.isEnabled()
    assert dialog.browser_button.isEnabled(), "the source page is known"

    bare = controller.add("https://x.example/a.bin", start_now=False)
    assert not RefreshDialog(bare).browser_button.isEnabled()
    dialog.deleteLater()


def test_a_video_captured_meanwhile_is_not_the_new_address(window, monkeypatch):
    controller = window.controller
    item = _paused(controller)
    monkeypatch.setattr(controller, "refresh_address",
                        lambda *a, **kw: pytest.fail("a stream replaced a file's address"))
    window._awaiting_refresh = (item.db_id, time.monotonic() + 60)
    assert window._take_refresh({"type": "media", "url": "https://v.example/a/master.m3u8"}) is False
    assert window._awaiting_refresh is not None
