"""A local HTTP server that can misbehave on demand.

Behaviour is selected per request with query parameters so a single running
server can cover every branch of the engine:

    ?norange=1     ignore Range, always send 200 + full body
    ?nohead=1      answer HEAD with 405
    ?nolength=1    no Content-Length and no range support
    ?drop=N        send N bytes then slam the connection shut
    ?fail=N        return 503 for the first N requests to this URL
    ?slow=MS       sleep MS milliseconds every 64 KiB
    ?disp=NAME     add a Content-Disposition filename
    ?nodisp=1      strip the default Content-Disposition
"""

from __future__ import annotations

import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep
from urllib.parse import parse_qs, quote, unquote, urlsplit

CHUNK = 64 * 1024

#: enough of a content-type map for the crawler and the media detector
CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "m3u8": "application/vnd.apple.mpegurl",
    "mpd": "application/dash+xml",
    "txt": "text/plain; charset=utf-8",
}


def _content_type(name: str) -> str:
    extension = name.rpartition(".")[2].lower()
    return CONTENT_TYPES.get(extension, "application/octet-stream")


def _disposition(name: str) -> str:
    """Header values must be latin-1, so non-ASCII names go in `filename*`."""
    fallback = "".join(
        c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # silence stderr spam
        pass

    # ------------------------------------------------------------------ verbs

    def do_HEAD(self) -> None:
        self._respond(body=False)

    def do_GET(self) -> None:
        self._respond(body=True)

    # --------------------------------------------------------------- internals

    def _respond(self, body: bool) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        parts = urlsplit(self.path)
        name = unquote(parts.path.lstrip("/"))
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}

        data = state.files.get(name)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        with state.lock:
            state.requests[name] += 1
            if not body:
                state.head_requests[name] += 1

        if not body and params.get("nohead"):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        fail_times = int(params.get("fail", 0))
        if fail_times:
            with state.lock:
                failed = state.failures[self.path]
                if failed < fail_times:
                    state.failures[self.path] = failed + 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

        no_range = bool(params.get("norange")) or bool(params.get("nolength"))
        no_length = bool(params.get("nolength"))
        total = len(data)

        start, end = 0, total - 1
        partial = False
        range_header = self.headers.get("Range")
        if range_header and not no_range:
            parsed = self._parse_range(range_header, total)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            partial = True
            with state.lock:
                state.range_requests[name] += 1

        payload = data[start : end + 1]

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", _content_type(name))
        if not no_range:
            self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        if state.etag:
            self.send_header("ETag", state.etag)
        if state.last_modified:
            self.send_header("Last-Modified", state.last_modified)
        disp = params.get("disp") or (
            None if params.get("nodisp") else state.disposition.get(name)
        )
        if disp:
            self.send_header("Content-Disposition", _disposition(disp))

        # `drop` only fires on responses big enough to be a real body (so the
        # one-byte probe request is unaffected) and only `dropcount` times, so
        # a retry can succeed.
        drop = int(params.get("drop", 0))
        dropping = False
        if drop and len(payload) > drop:
            with state.lock:
                if state.drops[self.path] < int(params.get("dropcount", 1)):
                    state.drops[self.path] += 1
                    dropping = True

        if no_length:
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()

        if not body:
            return

        slow_ms = int(params.get("slow", 0))
        sent = 0
        try:
            for offset in range(0, len(payload), CHUNK):
                chunk = payload[offset : offset + CHUNK]
                if dropping and sent + len(chunk) > drop:
                    self.wfile.write(chunk[: max(0, drop - sent)])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(chunk)
                sent += len(chunk)
                if slow_ms:
                    sleep(slow_ms / 1000.0)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.close_connection = True

    @staticmethod
    def _parse_range(header: str, total: int) -> tuple[int, int] | None:
        if not header.startswith("bytes="):
            return None
        spec = header[len("bytes=") :].split(",")[0].strip()
        first, _, last = spec.partition("-")
        if not first:
            return None
        start = int(first)
        end = int(last) if last else total - 1
        if start >= total or start > end:
            return None
        return start, min(end, total - 1)


class _State:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.disposition: dict[str, str] = {}
        self.requests: dict[str, int] = defaultdict(int)
        self.head_requests: dict[str, int] = defaultdict(int)
        self.range_requests: dict[str, int] = defaultdict(int)
        self.failures: dict[str, int] = defaultdict(int)
        self.drops: dict[str, int] = defaultdict(int)
        self.etag: str | None = '"v1"'
        self.last_modified: str | None = "Wed, 21 Oct 2026 07:28:00 GMT"
        self.lock = threading.Lock()


class FileServer:
    """Threaded test server; use as a context manager."""

    def __init__(self) -> None:
        self.state = _State()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def add_file(self, name: str, data: bytes, disposition: str | None = None) -> str:
        self.state.files[name] = data
        if disposition:
            self.state.disposition[name] = disposition
        return self.url_for(name)

    def url_for(self, name: str, **params: object) -> str:
        url = f"{self.base_url}/{quote(name)}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        return url

    def start(self) -> "FileServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FileServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
