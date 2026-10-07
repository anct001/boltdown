"""Site logins: a user name and password per host, kept protected."""

from __future__ import annotations

import base64
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.core.http_client import SiteAuth
from app.core.task import DownloadRequest, TaskRunner, TaskState
from app.util.credentials import protect, unprotect

from .conftest import make_payload, sha256

DATA = make_payload(700_000, seed=101)


def test_a_password_is_never_stored_as_typed():
    stored = protect("hunter2 ü")
    assert "hunter2" not in stored
    assert unprotect(stored) == "hunter2 ü"
    assert protect("") == "" and unprotect("") == "" and unprotect(None) == ""
    assert unprotect("garbage") == ""


def _drive(auth: SiteAuth, challenge: str | None, status: int = 401):
    """Run the auth flow by hand: what it sends, given what the server says."""
    request = httpx.Request("GET", "https://files.example/a.iso")
    flow = auth.auth_flow(request)
    first = next(flow)
    sent = [first.headers.get("authorization")]
    headers = {"www-authenticate": challenge} if challenge else {}
    try:
        second = flow.send(httpx.Response(status, headers=headers, request=first))
        sent.append(second.headers.get("authorization"))
    except StopIteration:
        pass
    return sent


def test_the_server_picks_basic():
    auth = SiteAuth("anna", "secret")
    sent = _drive(auth, 'Basic realm="files"')
    assert sent[0] is None, "no password before the server asks for one"
    assert sent[1] == "Basic " + base64.b64encode(b"anna:secret").decode()
    # and from then on it goes along at once
    assert _drive(auth, None, status=200)[0].startswith("Basic ")


def test_the_server_picks_digest():
    auth = SiteAuth("anna", "secret")
    sent = _drive(auth, 'Digest realm="files", nonce="abc123", qop="auth"')
    assert sent[1].startswith("Digest ") and 'username="anna"' in sent[1]
    assert "secret" not in sent[1], "a digest never carries the password"
    assert auth.scheme == "digest"


def test_no_challenge_means_no_password():
    auth = SiteAuth("anna", "secret")
    assert _drive(auth, None, status=200) == [None]
    assert auth.scheme is None


class _Guarded(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    expected = "Basic " + base64.b64encode(b"anna:secret").decode()

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != self.expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="files"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = 0, len(DATA) - 1
        header = self.headers.get("Range")
        if header:
            first, _, last = header[6:].partition("-")
            start, end = int(first), int(last) if last else len(DATA) - 1
        body = DATA[start:end + 1]
        self.send_response(206 if header else 200)
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def guarded():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Guarded)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/private.bin"
    httpd.shutdown()
    httpd.server_close()


async def test_a_login_opens_a_protected_download(guarded, tmp_path):
    runner = TaskRunner(1, DownloadRequest(
        url=guarded, save_dir=tmp_path, connections=4, auth=("anna", "secret"),
    ))
    assert await runner.run() is TaskState.COMPLETED
    assert sha256(runner.dest_path) == sha256(DATA)


async def test_without_a_login_the_error_says_where_to_add_one(guarded, tmp_path):
    runner = TaskRunner(1, DownloadRequest(url=guarded, save_dir=tmp_path))
    assert await runner.run() is TaskState.ERROR
    assert "Site rules" in runner.error


def test_a_rule_supplies_the_login(tmp_path):
    pytest.importorskip("PySide6")
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui.controller import Controller

    db = Database(tmp_path / "l.db")
    db.save_profile("*.files.example", username="anna", password=protect("secret"))
    stored = db.query("SELECT password FROM site_profiles")[0]["password"]
    assert "secret" not in stored
    controller = Controller(db, Settings(db))
    item = controller.add("https://dl.files.example/a.iso", start_now=False)
    assert controller._build_request(item).auth == ("anna", "secret")
    other = controller.add("https://elsewhere.example/a.iso", start_now=False)
    assert controller._build_request(other).auth is None
    db.close()


def test_an_older_database_gains_the_login_columns(tmp_path):
    from app.storage.db import SCHEMA_VERSION, Database

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '4');
            CREATE TABLE site_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1, connections INTEGER,
                speed_limit INTEGER, user_agent TEXT, referer TEXT, cookie TEXT,
                proxy TEXT, note TEXT);
            INSERT INTO site_profiles (pattern) VALUES ('example.com');
        """)
    db = Database(path)
    columns = {r["name"] for r in db.query("PRAGMA table_info(site_profiles)")}
    assert {"username", "password"} <= columns
    assert db.query("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] == str(SCHEMA_VERSION)
    assert db.list_profiles()[0]["pattern"] == "example.com"
    db.close()
