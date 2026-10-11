"""A small FTP server that misbehaves on request, for the FTP tests.

Enough of RFC 959 for a download manager: login, CWD, TYPE, SIZE, MDTM,
REST, PASV/EPSV and RETR. The ways real servers make life hard are
switches on `FtpState`: a connection limit (421), no REST, data connections
cut after a few bytes, a slow line.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field


@dataclass
class FtpState:
    #: "/dir/name" -> bytes
    files: dict[str, bytes] = field(default_factory=dict)
    #: user -> password; anonymous is let in when "anonymous" is a key
    users: dict[str, str] = field(default_factory=lambda: {"anonymous": ""})
    max_clients: int = 0          # 0 = no limit; otherwise 421 beyond it
    no_rest: bool = False
    no_size: bool = False         # 502 to SIZE, as some old servers answer
    rate: int = 0                 # bytes/second per transfer, 0 = unlimited
    cut_after: int = 0            # cut each data connection after this many bytes...
    cuts: int = 0                 # ...this many times in all
    clients: int = 0
    peak_clients: int = 0
    retr: list[tuple[str, int]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("latin-1"))
        self.wfile.flush()

    def handle(self) -> None:
        state = self.server.state
        with state.lock:
            if state.max_clients and state.clients >= state.max_clients:
                refused = True
            else:
                refused = False
                state.clients += 1
                state.peak_clients = max(state.peak_clients, state.clients)
        if refused:
            self.reply("421 Too many connections, try again later")
            return
        try:
            self.session()
        except (ConnectionError, OSError):
            pass
        finally:
            with state.lock:
                state.clients -= 1

    def session(self) -> None:
        state = self.server.state
        self.reply("220 test server ready")
        user = None
        logged_in = False
        cwd = "/"
        rest = 0
        listener: socket.socket | None = None
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb == "USER":
                user = arg
                self.reply("331 password please")
            elif verb == "PASS":
                if user in state.users and (user == "anonymous" or state.users[user] == arg):
                    logged_in = True
                    self.reply("230 logged in")
                else:
                    self.reply("530 Login incorrect")
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            elif not logged_in:
                self.reply("530 Please login with USER and PASS")
            elif verb in ("TYPE", "OPTS", "MODE", "STRU"):
                self.reply("200 ok")
            elif verb == "SYST":
                self.reply("215 UNIX Type: L8")
            elif verb == "FEAT":
                self.reply("211-Features:")
                self.reply(" SIZE")
                self.reply(" MDTM")
                if not state.no_rest:
                    self.reply(" REST STREAM")
                self.reply("211 End")
            elif verb == "NOOP":
                self.reply("200 ok")
            elif verb == "PWD":
                self.reply(f'257 "{cwd}"')
            elif verb == "CWD":
                target = _join(cwd, arg)
                if any(name.startswith(target.rstrip("/") + "/") for name in state.files):
                    cwd = target
                    self.reply("250 ok")
                else:
                    self.reply("550 no such directory")
            elif verb == "SIZE" and state.no_size:
                self.reply("502 SIZE not implemented")
            elif verb == "SIZE":
                data = state.files.get(_join(cwd, arg))
                self.reply(f"213 {len(data)}" if data is not None else "550 no such file")
            elif verb == "MDTM":
                if _join(cwd, arg) in state.files:
                    self.reply("213 20260101120000")
                else:
                    self.reply("550 no such file")
            elif verb == "REST":
                if state.no_rest:
                    self.reply("502 REST not implemented")
                else:
                    rest = int(arg)
                    self.reply(f"350 restarting at {rest}")
            elif verb in ("PASV", "EPSV"):
                if listener is not None:
                    listener.close()
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                port = listener.getsockname()[1]
                if verb == "PASV":
                    self.reply(f"227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})")
                else:
                    self.reply(f"229 Entering Extended Passive Mode (|||{port}|)")
            elif verb == "RETR":
                path = _join(cwd, arg)
                data = state.files.get(path)
                if data is None:
                    self.reply("550 no such file")
                    rest = 0
                    continue
                if listener is None:
                    self.reply("425 use PASV first")
                    continue
                self.reply("150 opening data connection")
                listener.settimeout(10)
                conn, _ = listener.accept()
                listener.close()
                listener = None
                with state.lock:
                    state.retr.append((path, rest))
                    cut = state.cut_after if state.cuts > 0 else 0
                    if cut:
                        state.cuts -= 1
                ok = self.send(conn, data[rest:], cut)
                rest = 0
                self.reply("226 transfer complete" if ok else "426 transfer aborted")
            elif verb == "ABOR":
                self.reply("226 aborted")
            else:
                self.reply("502 not implemented")

    def send(self, conn: socket.socket, data: bytes, cut: int) -> bool:
        rate = self.server.state.rate
        chunk = 16384
        sent = 0
        started = time.monotonic()
        try:
            while sent < len(data):
                piece = data[sent:sent + chunk]
                if cut and sent + len(piece) > cut:
                    conn.sendall(piece[:max(0, cut - sent)])
                    return False
                conn.sendall(piece)
                sent += len(piece)
                if rate:
                    ahead = sent / rate - (time.monotonic() - started)
                    if ahead > 0:
                        time.sleep(ahead)
            return True
        except OSError:
            return False
        finally:
            conn.close()


def _join(cwd: str, arg: str) -> str:
    path = arg if arg.startswith("/") else cwd.rstrip("/") + "/" + arg
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, state: FtpState) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state


class FtpServer:
    def __init__(self) -> None:
        self.state = FtpState()
        self._server = _Server(self.state)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def add_file(self, path: str, data: bytes) -> str:
        path = "/" + path.lstrip("/")
        self.state.files[path] = data
        return f"ftp://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
