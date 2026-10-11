"""Remote control: the web page and API a phone on the same network uses."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from app.remote import page, server as remote
from app.remote.server import RemoteServer, is_local

TOKEN = "s3cret-token"


class FakeApi:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.acted: list[tuple[int, str]] = []
        self.all: list[str] = []
        self.known = {1, 2}

    def status(self):
        return {"speed": 1024.0, "active": 1, "words": {}}

    def items(self):
        return [{"id": 1, "filename": "<img src=x onerror=alert(1)>.iso", "state": "downloading"}]

    def add(self, url):
        self.added.append(url)

    def act(self, item_id, action):
        if item_id not in self.known:
            return False
        self.acted.append((item_id, action))
        return True

    def act_all(self, action):
        self.all.append(action)


@pytest.fixture
def served():
    api = FakeApi()
    srv = RemoteServer(api, TOKEN, port=0, host="127.0.0.1")
    srv.start()
    yield srv, api, f"http://127.0.0.1:{srv.port}"
    srv.stop()


def call(base, path, *, method="GET", body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(base + path, data=data, method=method)
    if token is not None:
        request.add_header("X-Boltdown-Token", token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


# ------------------------------------------------------------------ the page


def test_the_page_and_its_files_need_no_token(served):
    _, _, base = served
    for path, kind in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css")):
        status, headers, body = call(base, path, token=None)
        assert status == 200 and kind in headers["Content-Type"]
        assert body
    status, headers, body = call(base, "/", token=None)
    assert TOKEN.encode() not in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_the_script_never_writes_html():
    # File names come from the internet; they may only ever be text.
    assert "innerHTML" not in page.SCRIPT
    assert "insertAdjacentHTML" not in page.SCRIPT
    assert "document.write" not in page.SCRIPT
    # And no inline script, which the CSP would block anyway.
    assert "<script>" not in page.HTML


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_script_parses(tmp_path):
    script = tmp_path / "app.js"
    script.write_text(page.SCRIPT, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------- the API


def test_the_api_wants_the_token(served):
    _, _, base = served
    assert call(base, "/api/items", token=None)[0] == 401
    assert call(base, "/api/items", token="wrong")[0] == 401
    status, _, body = call(base, "/api/items")
    assert status == 200
    assert json.loads(body)["items"][0]["id"] == 1


def test_too_many_wrong_tokens_lock_the_address_out(served):
    _, _, base = served
    for _ in range(remote.MAX_FAILURES):
        assert call(base, "/api/status", token="guess")[0] == 401
    # Locked out now, even with the right token.
    assert call(base, "/api/status")[0] == 429


def test_adding_a_download(served):
    _, api, base = served
    status, _, _ = call(base, "/api/add", method="POST", body={"url": " https://x.example/a.zip "})
    assert status == 200 and api.added == ["https://x.example/a.zip"]
    call(base, "/api/add", method="POST", body={"url": "magnet:?xt=urn:btih:abc"})
    assert api.added[-1].startswith("magnet:")
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "", None, 5):
        assert call(base, "/api/add", method="POST", body={"url": bad})[0] == 400
    assert call(base, "/api/add", method="POST", raw=b"{not json")[0] == 400
    assert call(base, "/api/add", method="POST", body=["https://x.example/a.zip"])[0] == 400


def test_a_post_without_the_token_does_nothing(served):
    # What another web site could make a visitor's browser send.
    _, api, base = served
    assert call(base, "/api/add", method="POST", body={"url": "https://x/a"}, token=None)[0] == 401
    assert call(base, "/api/items/1/remove", method="POST", body={}, token=None)[0] == 401
    assert api.added == [] and api.acted == []


def test_actions_on_downloads(served):
    _, api, base = served
    for action in ("pause", "resume", "remove"):
        assert call(base, f"/api/items/1/{action}", method="POST", body={})[0] == 200
    assert api.acted == [(1, "pause"), (1, "resume"), (1, "remove")]
    assert call(base, "/api/items/99/pause", method="POST", body={})[0] == 404
    assert call(base, "/api/items/1/delete-file", method="POST", body={})[0] == 404
    assert call(base, "/api/all/pause", method="POST", body={})[0] == 200
    assert call(base, "/api/all/remove", method="POST", body={})[0] == 404
    assert api.all == ["pause"]


def test_a_huge_body_is_refused(served):
    _, api, base = served
    big = json.dumps({"url": "https://x/" + "a" * (remote.MAX_BODY + 10)}).encode()
    assert call(base, "/api/add", method="POST", raw=big)[0] == 413
    assert api.added == []


def test_an_error_in_the_app_is_a_500_not_a_dead_server(served):
    srv, api, base = served

    def broken():
        raise RuntimeError("boom")

    api.items = broken
    assert call(base, "/api/items")[0] == 500
    assert call(base, "/api/status")[0] == 200


def test_unknown_paths(served):
    _, _, base = served
    assert call(base, "/../etc/passwd")[0] == 404
    assert call(base, "/api/nothing")[0] == 404
    assert call(base, "/api/nothing", method="POST", body={})[0] == 404


def test_only_the_local_network(monkeypatch, served):
    _, _, base = served
    monkeypatch.setattr(remote, "is_local", lambda address: False)
    assert call(base, "/", token=None)[0] == 403
    assert call(base, "/api/items")[0] == 403


@pytest.mark.parametrize("address, ok", [
    ("127.0.0.1", True), ("192.168.1.20", True), ("10.0.0.5", True), ("172.16.3.1", True),
    ("169.254.10.1", True), ("::1", True), ("fe80::1%eth0", True), ("fd00::1", True),
    ("::ffff:192.168.1.2", True),
    ("8.8.8.8", False), ("1.1.1.1", False), ("2606:4700::1111", False),
    ("::ffff:8.8.8.8", False), ("nonsense", False),
])
def test_what_counts_as_local(address, ok):
    assert is_local(address) is ok


def test_links_carry_the_token_after_the_hash(served):
    srv, _, _ = served
    links = srv.links()
    assert links and all(link.endswith(f":{srv.port}/#t={TOKEN}") for link in links)


def test_a_token_is_required():
    with pytest.raises(ValueError):
        RemoteServer(FakeApi(), "")


def test_tokens_are_long_and_different():
    first, second = remote.new_token(), remote.new_token()
    assert first != second and len(first) >= 20


# --------------------------------------------------------- the window's side

PySide6 = pytest.importorskip("PySide6")

from app.core.task import TaskState  # noqa: E402

from .test_gui import pump, qapp, stack  # noqa: E402,F401 - fixtures


def in_thread(work):
    """Run `work` as the server would: off the GUI thread."""
    box = {}

    def run():
        try:
            box["result"] = work()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_the_bridge_carries_calls_to_the_gui_thread(qapp, stack):
    from app.ui.remote_bridge import ControllerApi

    controller, _settings, _db = stack
    item = controller.add("https://x.example/a.iso", filename="a.iso", start_now=False)
    api = ControllerApi(controller)

    thread, box = in_thread(api.items)
    assert pump(qapp, lambda: not thread.is_alive(), timeout=10)
    assert [i["filename"] for i in box["result"]] == ["a.iso"]

    thread, box = in_thread(lambda: api.act(item.db_id, "remove"))
    assert pump(qapp, lambda: not thread.is_alive(), timeout=10)
    assert box["result"] is True and controller.item(item.db_id) is None

    thread, box = in_thread(lambda: api.act(12345, "pause"))
    assert pump(qapp, lambda: not thread.is_alive(), timeout=10)
    assert box["result"] is False

    thread, box = in_thread(api.status)
    assert pump(qapp, lambda: not thread.is_alive(), timeout=10)
    assert box["result"]["words"]["state_" + TaskState.PAUSED.value]


def test_errors_come_back_to_the_server_thread(qapp, stack):
    from app.ui.remote_bridge import ControllerApi

    controller, _settings, _db = stack

    def refuse(url):
        raise ValueError("no thanks")

    api = ControllerApi(controller, add=refuse)
    thread, box = in_thread(lambda: api.add("https://x.example/b.zip"))
    assert pump(qapp, lambda: not thread.is_alive(), timeout=10)
    assert isinstance(box.get("error"), ValueError)


def test_the_settings_switch_it_on_and_off(qapp, stack):
    from app.ui.remote_bridge import RemoteControl

    controller, settings, _db = stack
    settings.update({"remote_enabled": True, "remote_port": 0})
    control = RemoteControl(settings, controller)
    control.apply()
    try:
        assert control.server is not None and control.server.running
        token = settings.get("remote_token")
        assert token and control.links()[0].endswith(token)

        # A new link: the old token stops working.
        settings.set("remote_token", "")
        control.apply()
        assert settings.get("remote_token") != token

        settings.set("remote_enabled", False)
        control.apply()
        assert control.server is None and control.links() == []
    finally:
        control.stop()


def test_a_busy_port_is_reported_not_raised(qapp, stack):
    import socket

    from app.ui.remote_bridge import RemoteControl

    controller, settings, _db = stack
    holder = socket.socket()
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    try:
        settings.update({"remote_enabled": True, "remote_port": holder.getsockname()[1]})
        control = RemoteControl(settings, controller)
        control.apply()
        assert control.server is None and control.error
    finally:
        holder.close()


def test_the_window_adds_a_phone_link_without_a_dialog(qapp, stack, server):
    from app.ui.main_window import MainWindow

    controller, settings, _db = stack
    settings.set("ask_before_download", True)
    window = MainWindow(controller, settings)
    window._notify = lambda *a: None
    try:
        url = server.add_file("from-phone.zip", b"PK" + bytes(5000))
        window._add_from_remote(url)
        added = [i for i in controller.items() if i.url == url]
        assert len(added) == 1
        assert pump(qapp, lambda: added[0].state is TaskState.COMPLETED, timeout=20)
    finally:
        window._ticker.stop()
        window.remote.stop()
        window.deleteLater()
