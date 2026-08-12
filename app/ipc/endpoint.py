"""Loopback IPC: how a second process reaches the running application.

The server binds 127.0.0.1 on an ephemeral port and publishes `{port, token,
pid}` to `%LOCALAPPDATA%\\IDMClone\\ipc.json`. Every request must carry the
token, so another user account on the same machine cannot drive the app even
though the port is technically reachable from localhost.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable

from ..util.log import get_logger
from ..util.paths import data_dir
from .protocol import ProtocolError, decode_line, encode_line

log = get_logger(__name__)

ENDPOINT_FILE = "ipc.json"
CONNECT_TIMEOUT = 2.0


def endpoint_path() -> Path:
    return data_dir() / ENDPOINT_FILE


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10.0

    def handle(self) -> None:
        server: "IpcServer" = self.server.owner  # type: ignore[attr-defined]
        for raw in self.rfile:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = decode_line(raw)
            except ProtocolError as exc:
                self._reply({"ok": False, "error": str(exc)})
                continue
            if message.get("token") != server.token:
                log.warning("rejected IPC message with a bad token")
                self._reply({"ok": False, "error": "bad token"})
                return
            message.pop("token", None)
            try:
                response = server.dispatch(message)
            except Exception as exc:  # noqa: BLE001 - never kill the server
                log.exception("IPC handler failed")
                response = {"ok": False, "error": str(exc)}
            self._reply(response)

    def _reply(self, payload: dict[str, Any]) -> None:
        try:
            self.wfile.write(encode_line(payload))
            self.wfile.flush()
        except OSError:
            pass


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class IpcServer:
    """Runs inside the application; hands messages to `handler`."""

    def __init__(self, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._handler = handler
        self.token = secrets.token_hex(16)
        self._server: _TCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        return self._handler(message)

    def start(self) -> int:
        self._server = _TCPServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="idmclone-ipc", daemon=True
        )
        self._thread.start()
        self._publish()
        log.info("IPC listening on 127.0.0.1:%d", self.port)
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._unpublish()

    def _publish(self) -> None:
        path = endpoint_path()
        payload = {"port": self.port, "token": self.token, "pid": os.getpid()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)

    def _unpublish(self) -> None:
        try:
            current = json.loads(endpoint_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if current.get("pid") == os.getpid():
            try:
                endpoint_path().unlink()
            except OSError:
                pass

    def __enter__(self) -> "IpcServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


# ----------------------------------------------------------------------- client


def read_endpoint() -> dict[str, Any] | None:
    try:
        data = json.loads(endpoint_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "port" not in data or "token" not in data:
        return None
    return data


def send(message: dict[str, Any], timeout: float = CONNECT_TIMEOUT) -> dict[str, Any] | None:
    """Send one message to the running app. None means nobody is listening."""
    endpoint = read_endpoint()
    if endpoint is None:
        return None
    payload = dict(message)
    payload["token"] = endpoint["token"]
    try:
        with socket.create_connection(
            ("127.0.0.1", int(endpoint["port"])), timeout=timeout
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall(encode_line(payload))
            with sock.makefile("rb") as stream:
                line = stream.readline()
    except OSError as exc:
        log.debug("IPC send failed: %s", exc)
        return None
    if not line:
        return None
    try:
        return decode_line(line.strip())
    except ProtocolError:
        return None


def is_running() -> bool:
    return send({"type": "ping"}) is not None
