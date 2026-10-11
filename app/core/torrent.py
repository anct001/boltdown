"""BitTorrent: magnet links and .torrent files, through libtorrent.

libtorrent is optional (`pip install libtorrent`); without it a torrent
download fails with a message that says so, and nothing else changes.

How a torrent fits the rest of the app:

* one libtorrent session for the whole app, made on first use;
* a download's progress, speed and peers are read from libtorrent twice a
  second, and the app's speed limits - global, per download and the
  adaptive throttle - are handed to it as the torrent's download limit;
* pausing saves libtorrent's resume data next to the download
  (`<name>.boltdown-torrent`), so continuing does not re-hash every piece;
* when the last piece is in, the torrent leaves the session: Boltdown
  downloads, it does not stay behind to seed.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..util import filenames
from ..util.links import is_magnet
from ..util.log import get_logger
from .categories import target_dir
from .errors import CancelledByUser, FatalError, TransientError
from .http_client import build_client
from .task import TaskRunner, TaskState

log = get_logger(__name__)

POLL = 0.5
#: a .torrent file larger than this is not a .torrent file
MAX_TORRENT_FILE = 16 * 1024 * 1024
RESUME_SUFFIX = ".boltdown-torrent"
#: peers to try with every torrent, besides what trackers and DHT find - a
#: seed on the local network, or the tests' own
EXTRA_PEERS: list[tuple[str, int]] = []
#: libtorrent settings; `configure` changes them before the session exists
SETTINGS: dict[str, Any] = {
    "user_agent": "Boltdown",
    "listen_interfaces": "0.0.0.0:6881,[::]:6881",
    "enable_dht": True,
    "enable_lsd": True,
    "enable_upnp": True,
    "enable_natpmp": True,
}

_session = None
_session_lock = threading.Lock()


def available() -> bool:
    try:
        import libtorrent  # noqa: F401
    except ImportError:
        return False
    return True


def _lt():
    try:
        import libtorrent
    except ImportError as exc:
        raise FatalError(
            "torrent downloads need libtorrent: pip install libtorrent"
        ) from exc
    return libtorrent


def configure(**settings: Any) -> None:
    """Change session settings; a running session takes them at once."""
    SETTINGS.update(settings)
    if _session is not None:
        _session.apply_settings(settings)


def session():
    global _session
    with _session_lock:
        if _session is None:
            lt = _lt()
            settings = dict(SETTINGS)
            # Removal and storage alerts are what `_leave` waits on.
            settings.setdefault(
                "alert_mask",
                int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage),
            )
            _session = lt.session(settings)
        return _session


def shutdown() -> None:
    """Drop the session (tests; the app lets it go with the process)."""
    global _session
    with _session_lock:
        _session = None


def wants_torrent(url: str) -> bool:
    """A magnet link, or an address whose path ends in .torrent."""
    if is_magnet(url):
        return True
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and parts.path.lower().endswith(".torrent")


def magnet_name(url: str) -> str | None:
    """The `dn=` a magnet link suggests as the name, if it has one."""
    if not is_magnet(url):
        return None
    query = parse_qs(url.split("?", 1)[1] if "?" in url else "")
    names = query.get("dn") or []
    return filenames.sanitize(unquote(names[0])) if names and names[0] else None


class TorrentTaskRunner(TaskRunner):
    """One torrent, from magnet or .torrent to finished files."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._handle = None
        self._resume_path: Path | None = None

    async def _fetch(self) -> None:
        lt = _lt()
        params = await self._params(lt)
        if not self.filename:
            self.filename = magnet_name(self.request.url) or "torrent"
        directory = target_dir(
            Path(self.request.save_dir), self.filename, self.request.use_categories
        )
        directory.mkdir(parents=True, exist_ok=True)
        params.save_path = str(directory)
        self._resume_path = directory / (filenames.sanitize(self.filename) + RESUME_SUFFIX)
        params = self._with_resume_data(lt, params, directory)

        ses = session()
        handle = ses.add_torrent(params)
        self._handle = handle
        for peer in EXTRA_PEERS:
            with contextlib.suppress(Exception):
                handle.connect_peer(peer)
        try:
            await self._follow(lt, handle, directory)
        except CancelledByUser:
            await self._leave(lt, ses, handle, delete=self._cancel_requested)
            raise
        except BaseException:
            await self._leave(lt, ses, handle, delete=False)
            raise
        await self._leave(lt, ses, handle, delete=False, finished=True)

    async def _params(self, lt):
        """What to add: from the magnet link, or from the .torrent file."""
        url = self.request.url
        if is_magnet(url):
            try:
                return lt.parse_magnet_uri(url)
            except Exception as exc:  # noqa: BLE001 - libtorrent raises RuntimeError
                raise FatalError(f"not a usable magnet link: {exc}") from exc
        data = await self._torrent_file()
        try:
            info = lt.torrent_info(lt.bdecode(data))
        except Exception as exc:  # noqa: BLE001
            raise FatalError(f"not a .torrent file: {exc}") from exc
        params = lt.add_torrent_params()
        params.ti = info
        if not self.filename:
            self.filename = filenames.sanitize(info.name())
        return params

    async def _torrent_file(self) -> bytes:
        spec = self.request.to_spec()
        attempt = 0
        while True:
            try:
                async with build_client(spec) as client:
                    response = await client.get(spec.url)
                    if response.status_code >= 400:
                        raise FatalError(f"HTTP {response.status_code} for the .torrent file")
                    data = response.content
                if len(data) > MAX_TORRENT_FILE:
                    raise FatalError("the .torrent file is too large")
                return data
            except FatalError:
                raise
            except Exception as exc:  # noqa: BLE001 - network trouble: retry
                attempt += 1
                if attempt > self.request.max_retries:
                    raise TransientError(f"could not fetch the .torrent file: {exc}") from exc
                await asyncio.sleep(min(8.0, 2 ** attempt / 2))

    def _with_resume_data(self, lt, params, directory: Path):
        path = self._resume_path
        if path is None or not path.exists():
            return params
        try:
            resumed = lt.read_resume_data(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - stale or damaged: start over
            log.warning("ignoring torrent resume data %s: %s", path, exc)
            return params
        resumed.save_path = str(directory)
        if params.ti is not None and resumed.ti is None:
            resumed.ti = params.ti
        log.info("continuing %s from its resume data", self.filename)
        return resumed

    async def _follow(self, lt, handle, directory: Path) -> None:
        states = lt.torrent_status.states
        downloading = False
        while True:
            if self._stop.is_set():
                raise CancelledByUser("stopped")
            self._apply_limit(handle)
            status = handle.status()
            if status.errc.value():
                raise FatalError(f"torrent: {status.errc.message()}")
            if status.has_metadata:
                name = filenames.sanitize(status.name) or self.filename
                if name != self.filename:
                    self.filename = name
                self.dest_path = directory / name
                self.size = status.total_wanted or None
                self._downloaded = status.total_wanted_done
                self.resumable = True
            self._speed = float(status.download_payload_rate)
            self._peers = status.num_peers
            if status.state in (states.finished, states.seeding) or (
                status.has_metadata and status.total_wanted
                and status.total_wanted_done >= status.total_wanted
            ):
                return
            if not downloading and status.state == states.downloading:
                downloading = True
                self._set_state(TaskState.DOWNLOADING)
            else:
                self._emit("progress")
            await asyncio.sleep(POLL)

    def _apply_limit(self, handle) -> None:
        rate = self._bucket.rate
        limit = int(rate) if rate else 0
        if getattr(self, "_limit", None) != limit:
            handle.set_download_limit(limit if limit > 0 else -1)
            self._limit = limit

    async def _leave(self, lt, ses, handle, *, delete: bool, finished: bool = False) -> None:
        """Take the torrent out of the session, saving or removing its traces.

        libtorrent counts a piece as done once it is hashed, while writing it
        out is still queued on its disk thread; a file read straight after
        "finished" can come back short of its last pieces (the Windows CI
        runners caught it). So the disk cache is flushed - asking for resume
        data with `flush_disk_cache` waits for every queued write - and the
        torrent has left the session, files closed, before this returns.
        """
        if not delete:
            await self._save_resume(lt, ses, handle, keep=not finished)
        if finished or delete:
            with contextlib.suppress(OSError):
                if self._resume_path is not None:
                    self._resume_path.unlink()
        try:
            wanted = hash_key(handle.info_hashes())
        except Exception:  # noqa: BLE001 - an invalid handle has nothing to wait for
            wanted = None
        flags = lt.options_t.delete_files if delete else 0
        with contextlib.suppress(Exception):
            ses.remove_torrent(handle, flags)
        self._handle = None
        if wanted is not None:
            done = (
                ("torrent_deleted_alert", "torrent_delete_failed_alert") if delete
                else ("torrent_removed_alert",)
            )
            await _wait_for(ses, done, wanted, timeout=10.0)

    async def _save_resume(self, lt, ses, handle, *, keep: bool = True, timeout: float = 30.0) -> None:
        """Flush the disk cache; with `keep`, store the resume data too."""
        try:
            wanted = hash_key(handle.info_hashes())
            handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not ask for torrent resume data: %s", exc)
            return
        entry = await _wait_for(
            ses, ("save_resume_data_alert", "save_resume_data_failed_alert"), wanted, timeout
        )
        if entry is None:
            log.warning("torrent %s: no resume data within %.0fs", self.filename, timeout)
        elif entry[0] == "save_resume_data_failed_alert":
            log.warning("torrent resume data not saved: %s", entry[2])
        elif keep and self._resume_path is not None:
            try:
                self._resume_path.write_bytes(entry[2])
            except OSError as exc:
                log.warning("could not save torrent resume data: %s", exc)

    # ----------------------------------------------------- TaskRunner parts

    def _save_meta(self) -> None:
        """libtorrent keeps its own resume data; see `_save_resume`."""

    def _finalize(self) -> None:
        if self.dest_path is None:
            raise FatalError("the torrent finished without saying what it saved")
        self.filename = self.dest_path.name
        log.info("finished torrent %s (%s bytes)", self.dest_path, self.size)

    def snapshot(self):
        snap = super().snapshot()
        snap.connections = getattr(self, "_peers", 0)
        if self.size:
            snap.segments = [(0, self._downloaded, self.size - 1)]
        return snap


#: alerts popped by one torrent that another is waiting for, as plain data:
#: (kind, info-hash, payload). libtorrent's alert objects die at the next
#: `pop_alerts`, so none is ever kept - touching one later crashes.
_pending: list[tuple[str, str | None, Any]] = []
_pending_lock = threading.Lock()
#: how many unclaimed alerts to keep; nobody waits on most of them
_PENDING_LIMIT = 500
#: the alerts anyone waits for, by libtorrent class name
WANTED_ALERTS = (
    "save_resume_data_alert", "save_resume_data_failed_alert",
    "torrent_removed_alert", "torrent_deleted_alert", "torrent_delete_failed_alert",
)


def hash_key(hashes) -> str:
    """A torrent's identity as text: its v1 and v2 hashes in hex.

    Not `str(info_hash_t)`, which is the Python object's address - two
    objects for the same torrent would never compare equal.
    """
    return f"{hashes.v1}/{hashes.v2}"


def _hash_of(alert) -> str | None:
    hashes = getattr(alert, "info_hashes", None)  # removed / deleted alerts
    try:
        return hash_key(hashes if hashes is not None else alert.handle.info_hashes())
    except Exception:  # noqa: BLE001 - a handle that is already gone
        return None


def _copy(lt, alert) -> tuple[str, str | None, Any] | None:
    """What a waiting torrent needs from `alert`, copied out of libtorrent."""
    kind = type(alert).__name__
    if kind not in WANTED_ALERTS:
        return None
    if kind == "save_resume_data_alert":
        payload: Any = bytes(lt.write_resume_data_buf(alert.params))
    else:
        payload = alert.message()
    return kind, _hash_of(alert), payload


def _take(ses, kinds: tuple[str, ...], wanted: str):
    """The first unclaimed alert of `kinds` for torrent `wanted`, claimed.

    Alerts are session-wide, so whoever pops them keeps the rest for the
    other torrents; a claimed alert is gone, so an old one (an earlier
    pause of the same torrent) can never answer a new question.
    """
    lt = _lt()
    with _pending_lock:
        for alert in ses.pop_alerts():
            copied = _copy(lt, alert)
            if copied is not None:
                _pending.append(copied)
        for index, entry in enumerate(_pending):
            if entry[0] in kinds and entry[1] == wanted:
                del _pending[index]
                return entry
        del _pending[:-_PENDING_LIMIT]
    return None


async def _wait_for(ses, kinds, wanted: str, timeout: float):
    """(kind, hash, payload) of the alert, or None once `timeout` has passed."""
    kinds = kinds if isinstance(kinds, tuple) else (kinds,)
    deadline = time.monotonic() + timeout
    while True:
        entry = _take(ses, kinds, wanted)
        if entry is not None or time.monotonic() >= deadline:
            return entry
        await asyncio.sleep(0.05)
