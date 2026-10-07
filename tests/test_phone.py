"""Notifications to the phone: ntfy and Telegram."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.util import phone
from app.util.credentials import protect


def test_ntfy_is_one_post_to_the_topic():
    target = phone.Target(service=phone.NTFY, ntfy_topic="boltdown-x7k2", ntfy_server="https://ntfy.example/")
    request = phone.build_request(target, "Đã tải xong", "ubuntu.iso (5.7 GB)")
    assert request.method == "POST"
    assert str(request.url) == "https://ntfy.example/boltdown-x7k2"
    assert request.content.decode("utf-8") == "ubuntu.iso (5.7 GB)"
    title = request.headers["Title"]
    assert title.startswith("=?UTF-8?B?")
    assert base64.b64decode(title[10:-2]).decode("utf-8") == "Đã tải xong"


def test_telegram_is_one_send_message():
    target = phone.Target(service=phone.TELEGRAM, telegram_token="123:ABC", telegram_chat="42")
    request = phone.build_request(target, "Done", "a.zip")
    assert str(request.url) == "https://api.telegram.org/bot123:ABC/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "42" and body["text"] == "Done\na.zip"


@pytest.mark.parametrize("target, ready", [
    (phone.Target(), False),
    (phone.Target(service=phone.NTFY), False),
    (phone.Target(service=phone.NTFY, ntfy_topic="t"), True),
    (phone.Target(service=phone.TELEGRAM, telegram_token="x"), False),
    (phone.Target(service=phone.TELEGRAM, telegram_token="x", telegram_chat="1"), True),
])
def test_only_a_complete_setup_sends(target, ready):
    assert target.ready is ready


def test_send_reports_what_went_wrong():
    target = phone.Target(service=phone.NTFY, ntfy_topic="t")
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(200)

    assert phone.send(target, "T", "B", transport=httpx.MockTransport(ok)) is None
    assert len(seen) == 1
    refused = httpx.MockTransport(lambda request: httpx.Response(403))
    assert phone.send(target, "T", "B", transport=refused) == "HTTP 403"
    assert phone.send(phone.Target(), "T", "B") == "not set up"


def test_the_bot_token_is_kept_protected(tmp_path):
    from app.storage.db import Database
    from app.storage.settings import Settings

    db = Database(tmp_path / "p.db")
    settings = Settings(db)
    settings.update({"phone_service": "telegram", "telegram_token": protect("123:SECRET"),
                     "telegram_chat": "42"})
    assert "SECRET" not in str(settings.get("telegram_token"))
    target = phone.Target.from_settings(settings)
    assert target.telegram_token == "123:SECRET" and target.ready
    db.close()


PySide6 = pytest.importorskip("PySide6")

from app.core.task import TaskState  # noqa: E402

from .test_gui import qapp, stack  # noqa: E402,F401 - fixtures


@pytest.fixture
def window(qapp, stack, monkeypatch):
    from app.ui.main_window import MainWindow

    controller, settings, _db = stack
    settings.update({"phone_service": "ntfy", "ntfy_topic": "t"})
    win = MainWindow(controller, settings)
    win._notify = lambda *a: None
    win.sent = []
    monkeypatch.setattr(phone, "send_later",
                        lambda target, title, body, done=None: win.sent.append((title, body)))
    yield win
    win._ticker.stop()
    win.deleteLater()


def test_the_phone_hears_of_finished_and_failed_downloads(window):
    controller = window.controller
    item = controller.add("https://x.example/a.iso", filename="a.iso", start_now=False)
    item.state, item.size = TaskState.COMPLETED, 2048
    window._on_item_changed(item)
    failed = controller.add("https://x.example/b.zip", filename="b.zip", start_now=False)
    failed.state, failed.error = TaskState.ERROR, "HTTP 404: resource not found"
    window._on_item_changed(failed)
    window._on_item_changed(failed)  # a second change is no second message

    titles = [title for title, _ in window.sent]
    assert titles == ["Boltdown: Đã tải xong", "Boltdown: Tải thất bại"]
    assert "a.iso" in window.sent[0][1] and "404" in window.sent[1][1]


def test_events_switched_off_stay_quiet(window):
    window.settings.set("phone_on_finished", False)
    item = window.controller.add("https://x.example/c.iso", start_now=False)
    item.state = TaskState.COMPLETED
    window._on_item_changed(item)
    assert window.sent == []


def test_nothing_is_sent_without_a_service(window):
    window.settings.set("phone_service", "off")
    item = window.controller.add("https://x.example/d.iso", start_now=False)
    item.state = TaskState.COMPLETED
    window._on_item_changed(item)
    assert window.sent == []
