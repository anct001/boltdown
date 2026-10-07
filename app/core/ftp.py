"""FTP and FTPS downloads, split across connections like HTTP ones.

FTP resumes with `REST <offset>` before `RETR`, so each connection is a
segment: it starts at its own offset and is closed once its range is in.
Servers often allow only a few logins per address (421 beyond that), so
segments go in a queue and connections take them in turn - a server that
allows one connection still gets the whole file, one segment after another.

ftplib blocks, so every connection runs in a thread of its own; the event
loop only waits on them, hands out the speed limit and samples progress.
Everything else - the `.part` file, resume metadata, pause, cancel, the
finished file - is `TaskRunner`'s.
"""

from __future__ import annotations

import asyncio
import ftplib
import posixpath
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from ..util.log import get_logger
from .errors import (
    AuthRequiredError,
    CancelledByUser,
    DownloadError,
    FatalError,
    NotFoundError,
    ServerBusyError,
    TransientError,
)
from .segment import Segment, backoff_delay
from .task import TaskRunner, TaskState
from .writer import write_at

log = get_logger(__name__)

SCHEMES = ("ftp", "ftps")
CHUNK = 64 * 1024
TIMEOUT = 30.0


def is_ftp(url: str) -> bool:
    return urlsplit(url).scheme.lower() in SCHEMES


@dataclass(slots=True)
class FtpTarget:
    """Where the file is and how to log in."""

    tls: bool
    host: str
    port: int
    user: str
    password: str
    #: directories to CWD into, one at a time (RFC 1738), then the name
    dirs: list[str]
    name: str

    @classmethod
    def from_url(cls, url: str, auth: tuple[str, str] | None = None) -> "FtpTarget":
        parts = urlsplit(url)
        if parts.scheme.lower() not in SCHEMES or not parts.hostname:
            raise FatalError(f"not an FTP address: {url}")
        # ";type=i" may end the path; binary is the only mode used anyway.
        path = parts.path.split(";", 1)[0]
        pieces = [unquote(p) for p in path.split("/")]
        name = pieces[-1] if pieces else ""
        if not name:
            raise FatalError("the FTP address names a folder, not a file")
        user = unquote(parts.username) if parts.username else None
        password = unquote(parts.password) if parts.password else None
        if user is None and auth:
            user, password = auth
        return cls(
            tls=parts.scheme.lower() == "ftps",
            host=parts.hostname,
            port=parts.port or 21,
            user=user or "anonymous",
            password=password if password is not None else "anonymous@",
            dirs=[p for p in pieces[1:-1] if p],
            name=name,
        )

    @property
    def path(self) -> str:
        return posixpath.join("/", *self.dirs, self.name)


@dataclass(slots=True)
class FtpProbe:
    """What `TaskRunner` asks of a probe, for the resume metadata."""

    url: str
    final_url: str
    filename: str
    size: int | None
    resumable: bool
    etag: str | None = None
    last_modified: str | None = None


def connect(target: FtpTarget, timeout: float = TIMEOUT) -> ftplib.FTP:
    """Log in, binary mode, in the file's folder. Raises DownloadError kinds."""
    try:
        if target.tls:
            ftp: ftplib.FTP = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=timeout)
        else:
            ftp = ftplib.FTP(timeout=timeout)
        ftp.connect(target.host, target.port)
        ftp.login(target.user, target.password)
        if target.tls:
            ftp.prot_p()  # type: ignore[attr-defined]
        ftp.voidcmd("TYPE I")
        for directory in target.dirs:
            ftp.cwd(directory)
        return ftp
    except ftplib.all_errors as exc:
        raise _translate(exc) from exc


def _translate(exc: BaseException) -> DownloadError:
    text = str(exc).strip() or type(exc).__name__
    code = text[:3]
    if code == "421":
        return ServerBusyError(f"FTP server busy: {text}")
    if code == "530":
        return AuthRequiredError(
            f"FTP login refused ({text}) - put the user name and password in the "
            "address (ftp://user:pass@host/...) or in a site rule"
        )
    if code in ("550", "553"):
        return NotFoundError(f"FTP: {text}")
    if isinstance(exc, ftplib.error_perm):
        return FatalError(f"FTP: {text}")
    return TransientError(f"FTP: {text}")


def probe_ftp(target: FtpTarget, url: str) -> FtpProbe:
    """Size, date and whether REST works - on one connection."""
    ftp = connect(target)
    try:
        try:
            size = ftp.size(target.name)
        except ftplib.error_perm as exc:
            # 550 is "no such file"; 500/502 a server that has no SIZE.
            if str(exc).startswith(("550", "553")):
                raise _translate(exc) from exc
            size = None
        try:
            stamp = ftp.sendcmd(f"MDTM {target.name}")
            modified = stamp[4:].strip() if stamp.startswith("213") else None
        except ftplib.error_perm:
            modified = None
        try:
            ftp.sendcmd("REST 0")
            resumable = size is not None
        except ftplib.error_perm:
            resumable = False
    except ftplib.all_errors as exc:
        raise _translate(exc) from exc
    finally:
        _close(ftp)
    return FtpProbe(
        url=url, final_url=url, filename=target.name, size=size,
        resumable=resumable, last_modified=modified,
    )


def _close(ftp: ftplib.FTP | None) -> None:
    if ftp is None:
        return
    try:
        ftp.close()
    except Exception:  # noqa: BLE001 - closing must never raise
        pass


class FtpTaskRunner(TaskRunner):
    """One FTP download; see the module docstring."""

    async def _fetch(self) -> None:
        target = FtpTarget.from_url(self.request.url, self.request.auth)
        loop = asyncio.get_running_loop()
        result = await self._probe_ftp(target)
        self._probe = result  # type: ignore[assignment]
        self._apply_probe(result)  # type: ignore[arg-type]
        self._prepare_files(result)  # type: ignore[arg-type]
        if self._all_complete():
            log.info("task %d already complete on disk", self.id)
            return
        self._set_state(TaskState.DOWNLOADING)

        stop = threading.Event()
        queue: list[Segment] = [s for s in self.segments if not s.is_complete]
        lock = threading.Lock()
        self._count_lock = lock
        busy = {"active": 0}
        assert self._target is not None
        fd = self._target.open_fd()
        workers = max(1, min(self.request.connections, len(queue)))
        # A pool of its own: the shared default one may have fewer threads
        # than this download has connections.
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"ftp{self.id}")
        threads = [
            loop.run_in_executor(
                pool, self._worker, loop, target, queue, lock, busy, stop, fd, n
            )
            for n in range(workers)
        ]
        monitor = asyncio.create_task(self._monitor(), name=f"task{self.id}-monitor")
        waiter = asyncio.create_task(self._stop.wait())
        gathered = asyncio.gather(*threads, return_exceptions=True)
        try:
            done, _ = await asyncio.wait({gathered, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                raise CancelledByUser("stopped")
            outcomes = gathered.result()
        finally:
            stop.set()
            # Every thread is out before the file is closed under it.
            await asyncio.gather(gathered, return_exceptions=True)
            pool.shutdown(wait=False)
            waiter.cancel()
            monitor.cancel()
            await asyncio.gather(monitor, waiter, return_exceptions=True)
            self._target.close_fd(fd)
            self._recount()
            self._save_meta()
        # A segment of unknown size is done once it read to the end, which
        # gives it an end; one that still has none never got there.
        left = [s for s in self.segments if s.end is None or not s.is_complete]
        if left:
            errors = [o for o in outcomes if isinstance(o, BaseException)]
            if errors:
                raise errors[0]
            raise TransientError(f"FTP: {len(left)} segment(s) unfinished")

    async def _probe_ftp(self, target: FtpTarget) -> FtpProbe:
        attempt = 0
        while True:
            if self._stop.is_set():
                raise CancelledByUser("stopped")
            try:
                return await asyncio.to_thread(probe_ftp, target, self.request.url)
            except TransientError as exc:
                attempt += 1
                if attempt > self.request.max_retries:
                    raise
                delay = backoff_delay(attempt)
                log.warning("FTP probe retry %d in %.1fs (%s)", attempt, delay, exc)
                await asyncio.sleep(delay)

    # --------------------------------------------------------- worker thread

    def _worker(self, loop, target, queue, lock, busy, stop, fd, number) -> None:
        """Take segments until none are left; one login, re-made on errors."""
        ftp: ftplib.FTP | None = None
        failures = 0
        try:
            while not stop.is_set():
                with lock:
                    segment = queue.pop(0) if queue else None
                if segment is None:
                    return
                try:
                    if ftp is None:
                        ftp = connect(target)
                    with lock:
                        busy["active"] += 1
                    try:
                        self._fetch_segment(ftp, loop, target, segment, stop, fd)
                    finally:
                        with lock:
                            busy["active"] -= 1
                    failures = 0
                    if not segment.is_complete and segment.end is not None and not stop.is_set():
                        raise TransientError("FTP: data connection closed early")
                    if segment.end is not None and not stop.is_set():
                        # A transfer stopped short of the end leaves the
                        # control connection mid-reply; a new login is
                        # cheaper than untangling it.
                        _close(ftp)
                        ftp = None
                except ServerBusyError:
                    _close(ftp)
                    ftp = None
                    with lock:
                        queue.insert(0, segment)
                        others = busy["active"]
                    if others:
                        # The server's limit is reached and other connections
                        # are working: they will take this segment in turn.
                        log.info("task %d: FTP connection %d not allowed; others carry on",
                                 self.id, number)
                        return
                    failures += 1
                    if failures > self.request.max_retries:
                        raise
                    stop.wait(backoff_delay(failures))
                except (TransientError, OSError, EOFError, ftplib.error_temp,
                        ftplib.error_reply) as exc:
                    _close(ftp)
                    ftp = None
                    with lock:
                        queue.insert(0, segment)
                    failures += 1
                    if failures > self.request.max_retries:
                        raise exc if isinstance(exc, DownloadError) else _translate(exc)
                    delay = backoff_delay(failures)
                    log.warning("task %d: FTP segment %d retry in %.1fs (%s)",
                                self.id, segment.index, delay, exc)
                    stop.wait(delay)
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    _close(ftp)

    def _fetch_segment(self, ftp, loop, target, segment: Segment, stop, fd) -> None:
        rest = segment.current if segment.current else None
        if rest and not self.resumable:
            raise FatalError("FTP server cannot resume (no REST)")
        try:
            conn = ftp.transfercmd(f"RETR {target.name}", rest=rest)
        except ftplib.all_errors as exc:
            raise _translate(exc) from exc
        conn.settimeout(TIMEOUT)
        finished = False
        try:
            while not stop.is_set():
                want = CHUNK
                if segment.remaining is not None:
                    want = min(want, segment.remaining)
                    if want <= 0:
                        finished = True
                        break
                self._take_tokens(loop, want, stop)
                data = conn.recv(want)
                if not data:
                    finished = True
                    break
                write_at(fd, data, segment.current)
                with self._count_lock:
                    segment.done += len(data)
                    self._downloaded += len(data)
        except socket.timeout as exc:
            raise TransientError("FTP: data connection stalled") from exc
        finally:
            conn.close()
        if finished and segment.end is None:
            # Unknown size: the server closing the data connection is the end.
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                pass
            segment.end = segment.start + segment.done - 1
            self.size = segment.done

    def _take_tokens(self, loop, amount: int, stop: threading.Event) -> None:
        if not self._bucket.rate:
            return
        future = asyncio.run_coroutine_threadsafe(self._bucket.acquire(amount), loop)
        while not stop.is_set():
            try:
                future.result(timeout=0.25)
                return
            except TimeoutError:
                continue
        future.cancel()

