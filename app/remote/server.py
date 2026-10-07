"""Control Boltdown from a phone or another computer on the same network.

A small web page (`page.py`) and a JSON API behind it:

    GET  /api/status                  speed, counts, the page's words
    GET  /api/items                   every download
    POST /api/add          {"url"}    add a download
    POST /api/items/<id>/<action>     pause | resume | remove
    POST /api/all/<action>            pause | resume

Who may use it:

* only addresses on the local network (private, loopback, link-local) - a
  router that forwards the port by mistake still does not open it to the
  internet;
* only with the token, sent as the `X-Boltdown-Token` header on every API
  call. The link carries it after `#`, which browsers never send to a server
  nor put in a Referer, and the page keeps it in its own storage. There are
  no cookies, so another web site cannot make a visitor's browser act here;
* ten wrong tokens from one address in a minute, and that address waits.

The server knows nothing of Qt: it calls a `RemoteApi`, and the window's
adapter carries those calls over to the GUI thread.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from ..util.links import is_download_link
from ..util.log import get_logger
from . import page

log = get_logger(__name__)

DEFAULT_PORT = 9614
TOKEN_HEADER = "X-Boltdown-Token"
MAX_BODY = 64 * 1024
#: wrong tokens allowed per address per minute
MAX_FAILURES = 10
ITEM_ACTIONS = ("pause", "resume", "remove")
ALL_ACTIONS = ("pause", "resume")
_ITEM = re.compile(r"^/api/items/(\d+)/([a-z]+)$")
_ALL = re.compile(r"^/api/all/([a-z]+)$")


class RemoteApi(Protocol):
    def status(self) -> dict[str, Any]: ...
    def items(self) -> list[dict[str, Any]]: ...
    def add(self, url: str) -> None: ...
    def act(self, item_id: int, action: str) -> bool: ...
    def act_all(self, action: str) -> None: ...


def new_token() -> str:
    return secrets.token_urlsafe(18)


def is_local(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def lan_addresses() -> list[str]:
    """This computer's addresses on the local network, best guess first."""
    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket sends nothing; it only picks the interface
        # the default route would use.
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found and is_local(address) and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found or ["127.0.0.1"]


class _Handler(BaseHTTPRequestHandler):
    server: "_HttpServer"
    protocol_version = "HTTP/1.1"
    server_version = "Boltdown"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:  # pragma: no cover
        log.debug("remote %s: " + fmt, self.client_address[0], *args)

    # ------------------------------------------------------------- replies

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    # ------------------------------------------------------------- gates

    def _allowed(self) -> bool:
        if not is_local(self.client_address[0]):
            self._error(403, "only the local network may use this")
            return False
        return True

    def _authorised(self) -> bool:
        address = self.client_address[0]
        if self.server.locked_out(address):
            self._error(429, "too many wrong tokens; wait a minute")
            return False
        given = self.headers.get(TOKEN_HEADER, "")
        if given and hmac.compare_digest(given.encode(), self.server.token.encode()):
            return True
        self.server.failed(address)
        self._error(401, "wrong or missing token")
        return False

    # ------------------------------------------------------------- verbs

    def do_HEAD(self) -> None:  # noqa: N802 - http.server's naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, page.HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/app.js":
            self._send(200, page.SCRIPT.encode("utf-8"), "text/javascript; charset=utf-8")
        elif path == "/app.css":
            self._send(200, page.STYLE.encode("utf-8"), "text/css; charset=utf-8")
        elif path == "/api/status":
            if self._authorised():
                self._call(lambda: self.server.api.status())
        elif path == "/api/items":
            if self._authorised():
                self._call(lambda: {"items": self.server.api.items()})
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed() or not self._authorised():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(413, "too large")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._error(400, "not JSON")
            return
        if not isinstance(body, dict):
            self._error(400, "expected an object")
            return
        path = self.path.split("?", 1)[0]
        api = self.server.api

        if path == "/api/add":
            url = body.get("url")
            if not is_download_link(url):
                self._error(400, "not a download address")
                return
            self._call(lambda: (api.add(url.strip()), {"ok": True})[1])
            return
        if match := _ITEM.match(path):
            item_id, action = int(match.group(1)), match.group(2)
            if action not in ITEM_ACTIONS:
                self._error(404, "unknown action")
                return
            self._call(lambda: {"ok": True} if api.act(item_id, action) else None)
            return
        if match := _ALL.match(path):
            action = match.group(1)
            if action not in ALL_ACTIONS:
                self._error(404, "unknown action")
                return
            self._call(lambda: (api.act_all(action), {"ok": True})[1])
            return
        self._error(404, "not found")

    def _call(self, work) -> None:
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the server
            log.exception("remote call failed")
            self._error(500, str(exc) or type(exc).__name__)
            return
        if result is None:
            self._error(404, "no such download")
        else:
            self._json(200, result)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    # SO_REUSEADDR means "rebind after a restart" on Unix, but on Windows it
    # lets a second program bind a port that is in use - and take over the
    # connections meant for it. There the port is claimed exclusively.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address, api: RemoteApi, token: str, family: int) -> None:
        self.address_family = family
        super().__init__(address, _Handler)
        self.api = api
        self.token = token
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        if self.address_family == socket.AF_INET6:
            # One socket for IPv4 and IPv6 where the system allows it.
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:  # pragma: no cover - platform dependent
                pass
        super().server_bind()

    def failed(self, address: str) -> None:
        with self._lock:
            now = time.monotonic()
            recent = [t for t in self._failures.get(address, []) if now - t < 60]
            recent.append(now)
            self._failures[address] = recent

    def locked_out(self, address: str) -> bool:
        with self._lock:
            now = time.monotonic()
            recent = [t for t in self._failures.get(address, []) if now - t < 60]
            self._failures[address] = recent
            return len(recent) >= MAX_FAILURES


class RemoteServer:
    """Start, stop, and say where to point a phone."""

    def __init__(
        self, api: RemoteApi, token: str, *, port: int = DEFAULT_PORT, host: str = ""
    ) -> None:
        if not token:
            raise ValueError("a token is required")
        self.api = api
        self.token = token
        self.port = port
        self.host = host
        self._server: _HttpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        if self._server is not None:
            return
        family = socket.AF_INET
        host = self.host or "0.0.0.0"
        if not self.host and socket.has_ipv6:
            family, host = socket.AF_INET6, "::"
        try:
            self._server = _HttpServer((host, self.port), self.api, self.token, family)
        except OSError:
            if family != socket.AF_INET6:
                raise
            self._server = _HttpServer(("0.0.0.0", self.port), self.api, self.token, socket.AF_INET)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="remote-control", daemon=True
        )
        self._thread.start()
        log.info("remote control listening on port %d", self.port)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def links(self) -> list[str]:
        """What to open on the phone - one per address of this computer."""
        return [f"http://{address}:{self.port}/#t={self.token}" for address in lan_addresses()]
