"""Browser cookies must not follow a redirect to somebody else's site."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core.http_client import RequestSpec, build_client, cookie_allowed


@pytest.mark.parametrize(
    "origin, target, allowed",
    [
        ("https://example.com/a", "https://example.com/b", True),
        ("https://www.example.com/a", "https://dl.example.com/b", True),
        ("https://drive.google.com/x", "https://drive.usercontent.google.com/y", True),
        ("https://shop.example.co.uk/a", "https://cdn.example.co.uk/b", True),
        ("https://a.example.com.vn/", "https://b.example.com.vn/", True),
        ("https://example.com/a", "https://evil.com/b", False),
        ("https://example.com/a", "https://example.com.evil.com/b", False),
        ("https://example.com/a", "http://example.com/b", False),
        ("https://one.co.uk/a", "https://two.co.uk/b", False),
        ("https://victim.github.io/", "https://attacker.github.io/", False),
        ("https://a.s3.amazonaws.com/", "https://b.s3.amazonaws.com/", False),
        ("http://127.0.0.1:1/", "http://127.0.0.2:1/", False),
        ("http://127.0.0.1:1/", "http://127.0.0.1:2/", True),
        ("https://example.com/", "not a url", False),
    ],
)
def test_cookie_allowed(origin, target, allowed):
    assert cookie_allowed(origin, target) is allowed


class _Recorder(BaseHTTPRequestHandler):
    seen: list

    def log_message(self, *args):  # noqa: D401 - keep pytest output quiet
        pass

    def do_GET(self):
        self.seen.append((self.headers.get("Host"), self.path, self.headers.get("Cookie")))
        location = self.server.redirect_to.get(self.path)
        if location:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture
def server():
    seen: list = []
    handler = type("H", (_Recorder,), {"seen": seen})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.redirect_to = {}
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, seen
    httpd.shutdown()
    httpd.server_close()


async def test_the_cookie_survives_a_same_site_redirect(server):
    """httpx drops the Cookie header on *every* redirect, so a link that
    bounced to the real file on the same site arrived without the session -
    and a login page was saved instead of the file."""
    httpd, seen = server
    port = httpd.server_address[1]
    httpd.redirect_to["/file"] = f"http://127.0.0.1:{port}/real"
    spec = RequestSpec(url=f"http://127.0.0.1:{port}/file", cookie="session=secret")
    async with build_client(spec) as client:
        await client.get(spec.url)
    assert [c for _host, _path, c in seen] == ["session=secret", "session=secret"]


async def test_the_cookie_does_not_follow_a_redirect_to_another_site(server):
    httpd, seen = server
    port = httpd.server_address[1]
    # 127.0.0.1 and localhost are one machine but two sites, which is all a
    # cross-site redirect needs to be in a test.
    httpd.redirect_to["/file"] = f"http://localhost:{port}/elsewhere"
    spec = RequestSpec(url=f"http://127.0.0.1:{port}/file", cookie="session=secret")
    async with build_client(spec) as client:
        response = await client.get(spec.url)
    assert response.status_code == 200
    assert seen[0][2] == "session=secret"
    assert seen[1][1] == "/elsewhere" and seen[1][2] is None, seen


async def test_the_cookie_comes_back_on_the_way_home(server):
    httpd, seen = server
    port = httpd.server_address[1]
    httpd.redirect_to["/file"] = f"http://localhost:{port}/bounce"
    httpd.redirect_to["/bounce"] = f"http://127.0.0.1:{port}/real"
    spec = RequestSpec(url=f"http://127.0.0.1:{port}/file", cookie="session=secret")
    async with build_client(spec) as client:
        await client.get(spec.url)
    assert [c for _host, _path, c in seen] == ["session=secret", None, "session=secret"]


async def test_a_cookie_given_as_a_header_is_scoped_too(server):
    httpd, seen = server
    port = httpd.server_address[1]
    httpd.redirect_to["/file"] = f"http://localhost:{port}/elsewhere"
    spec = RequestSpec(url=f"http://127.0.0.1:{port}/file", headers={"cookie": "p=1"})
    async with build_client(spec) as client:
        await client.get(spec.url)
    assert seen[0][2] == "p=1" and seen[1][2] is None
