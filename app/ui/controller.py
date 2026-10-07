"""Bridge between the async engine, SQLite and the Qt widgets.

Engine callbacks arrive on the engine's loop thread. Touching widgets from
there would crash, so every callback is bounced through a Qt signal with an
explicit queued connection; `_on_engine_event` therefore always runs on the
GUI thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal

from ..core.categories import category_for, target_dir
from ..core.engine import Engine, EngineEvent
from ..core.profiles import SiteProfile, apply_to
from ..core.resume import ResumeMeta, meta_path_for
from ..core.task import PART_SUFFIX, DownloadRequest, TaskSnapshot, TaskState
from ..media.detect import classify, suggested_name
from ..storage.db import Database
from ..storage.settings import Settings
from ..util import filenames, proxy
from ..util.checksums import Expected, Verdict, parse_expected
from ..util.log import get_logger

log = get_logger(__name__)

DB_SYNC_INTERVAL = 2.0
LIVE_STATES = (TaskState.QUEUED, TaskState.PROBING, TaskState.DOWNLOADING)


@dataclass
class DownloadItem:
    """One row of the main table - mirrors one row of the `downloads` table."""

    db_id: int
    url: str
    filename: str
    save_path: str
    size: int | None = None
    downloaded: int = 0
    state: TaskState = TaskState.QUEUED
    speed: float = 0.0
    eta: float | None = None
    connections: int = 8
    speed_limit: int | None = None
    error: str | None = None
    added_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    referer: str | None = None
    cookie: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    use_categories: bool = True
    segments: list[tuple[int, int, int | None]] = field(default_factory=list)
    engine_id: int | None = None
    max_height: int | None = None
    audio_only: bool = False
    queue_id: int | None = None
    #: set when the user pauses this item by hand, so its queue leaves it alone
    manual_pause: bool = False
    #: the file name is settled - do not let a re-probe rename it
    name_locked: bool = False

    @property
    def category(self) -> str:
        return category_for(self.filename)

    @property
    def percent(self) -> float:
        if not self.size:
            return 0.0
        return min(100.0, self.downloaded * 100.0 / self.size)

    @property
    def path(self) -> Path:
        return Path(self.save_path) / self.filename

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def is_media(self) -> bool:
        return classify(self.url).is_media

    @property
    def is_pending(self) -> bool:
        """Waiting for its turn: a queue may still start this one.

        A failed download is *not* pending: retrying it automatically would
        spin the queue on a 404 forever.
        """
        return (
            self.state in (TaskState.PAUSED, TaskState.QUEUED)
            and not self.manual_pause
        )


@dataclass(slots=True)
class QueueInfo:
    """One row of the `queues` table, plus whether it is running right now."""

    id: int
    name: str
    max_concurrent: int = 1
    enabled: bool = True
    position: int = 0
    running: bool = False


class Controller(QObject):
    itemAdded = Signal(object)
    itemChanged = Signal(object)
    itemRemoved = Signal(int)
    queuesChanged = Signal()
    #: a running queue has just finished everything it had - carries queue id
    queueFinished = Signal(int)
    #: internal - carries engine events across the thread boundary
    _engineEvent = Signal(object)

    def __init__(self, db: Database, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self._items: dict[int, DownloadItem] = {}
        self._by_engine_id: dict[int, int] = {}
        self._last_sync: dict[int, float] = {}
        self._running_queues: set[int] = set()
        #: downloads to start again once their pause lands (a new address)
        self._restart_when_paused: set[int] = set()

        self.engine = Engine(
            max_concurrent=settings.max_concurrent,
            speed_limit=settings.speed_limit,
            on_event=self._engineEvent.emit,
        )
        self._engineEvent.connect(self._on_engine_event, Qt.QueuedConnection)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.engine.start()
        self.restore()
        if self.settings.get("use_system_proxy"):
            # Read the PAC file now, so the first download does not have to.
            _pac.prefetch(proxy.system_proxy().pac_url)

    def shutdown(self) -> None:
        for item in self._items.values():
            if item.is_live:
                # A pause is what the user gets back on next launch.
                self._persist(item, state=TaskState.PAUSED.value)
        self.engine.stop(timeout=20)
        # Nothing should reach a half-destroyed Qt object afterwards.
        self.engine.set_event_listener(None)

    def restore(self) -> None:
        """Rebuild the list from SQLite; nothing auto-starts."""
        for row in reversed(self.db.list_downloads()):
            state = TaskState(row["state"]) if row["state"] in _STATE_VALUES else TaskState.PAUSED
            if state in LIVE_STATES:
                state = TaskState.PAUSED
            headers = self.db.query(
                "SELECT * FROM download_headers WHERE download_id = ?", (row["id"],)
            )
            extra = headers[0] if headers else None
            item = DownloadItem(
                db_id=row["id"],
                url=row["url"],
                filename=row["filename"],
                save_path=row["save_path"],
                size=row["size"],
                downloaded=row["downloaded"],
                state=state,
                connections=row["connections"],
                speed_limit=row["speed_limit"],
                error=row["error"],
                added_at=row["added_at"],
                finished_at=row["finished_at"],
                referer=extra["referer"] if extra else None,
                cookie=extra["cookie"] if extra else None,
                user_agent=extra["user_agent"] if extra else None,
                proxy=extra["proxy"] if extra else None,
                queue_id=row["queue_id"],
                name_locked=bool(row["name_locked"]),
            )
            self._items[item.db_id] = item
            self.itemAdded.emit(item)

    # ------------------------------------------------------------------ query

    def items(self) -> list[DownloadItem]:
        return list(self._items.values())

    def item(self, db_id: int) -> DownloadItem | None:
        return self._items.get(db_id)

    def total_speed(self) -> float:
        return sum(i.speed for i in self._items.values() if i.is_live)

    def active_count(self) -> int:
        return sum(1 for i in self._items.values() if i.is_live)

    # ---------------------------------------------------------------- mutate

    def add(
        self,
        url: str,
        *,
        save_dir: Path | str | None = None,
        filename: str | None = None,
        connections: int | None = None,
        speed_limit: int | None = None,
        referer: str | None = None,
        cookie: str | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
        start_now: bool = True,
        max_height: int | None = None,
        audio_only: bool = False,
        queue_id: int | None = None,
        checksum: str | None = None,
    ) -> DownloadItem:
        expected = parse_expected(checksum) if checksum else None
        if checksum and expected is None:
            raise ValueError(f"not a checksum: {checksum!r}")
        # A queued download waits for its queue, never for the Add dialog.
        if queue_id is not None:
            start_now = False
        save_dir = Path(save_dir) if save_dir else self.settings.download_dir
        chosen = filenames.sanitize(filename) if filename else None
        guess = chosen or _media_name(url) or filenames.from_url(url) or "download"
        db_id = self.db.add_download(
            url=url,
            filename=guess,
            save_path=str(save_dir),
            state=TaskState.QUEUED.value if start_now else TaskState.PAUSED.value,
            category=category_for(guess),
            connections=connections or self.settings.connections,
            speed_limit=speed_limit,
            cookie=cookie,
            referer=referer,
            user_agent=user_agent,
            proxy=proxy,
            name_locked=chosen is not None,
            queue_id=queue_id,
        )
        item = DownloadItem(
            db_id=db_id,
            url=url,
            filename=guess,
            save_path=str(save_dir),
            state=TaskState.QUEUED if start_now else TaskState.PAUSED,
            connections=connections or self.settings.connections,
            speed_limit=speed_limit,
            referer=referer,
            cookie=cookie,
            user_agent=user_agent,
            proxy=proxy,
            use_categories=bool(self.settings.get("use_categories")),
            max_height=max_height,
            audio_only=audio_only,
            queue_id=queue_id,
            name_locked=chosen is not None,
        )
        if expected is not None:
            self.db.set_checksum(db_id, expected.algorithm, expected.digest)
        self._items[db_id] = item
        self.itemAdded.emit(item)
        if start_now:
            self.start_item(db_id)
        elif queue_id is not None and queue_id in self._running_queues:
            self.pump_queues()
        return item

    def start_item(self, db_id: int) -> None:
        item = self._items.get(db_id)
        if item is None or item.state is TaskState.COMPLETED:
            return
        # `item.is_live` covers QUEUED, which is also the state a freshly added
        # item sits in - ask the engine whether a task is genuinely running.
        if item.engine_id is not None and self.engine.state(item.engine_id) in LIVE_STATES:
            return
        item.error = None
        item.manual_pause = False
        item.state = TaskState.QUEUED
        engine_id = self.engine.submit(self._build_request(item))
        item.engine_id = engine_id
        self._by_engine_id[engine_id] = db_id
        self._persist(item, state=item.state.value)
        self.itemChanged.emit(item)

    def pause_item(self, db_id: int, manual: bool = True) -> None:
        item = self._items.get(db_id)
        if item is None or item.engine_id is None or not item.is_live:
            return
        item.manual_pause = manual
        self.engine.pause(item.engine_id)

    def pause_all(self) -> None:
        for db_id in list(self._items):
            self.pause_item(db_id)

    def resume_all(self) -> None:
        for db_id, item in list(self._items.items()):
            if item.state in (TaskState.PAUSED, TaskState.ERROR):
                self.start_item(db_id)

    def remove(self, db_id: int, delete_file: bool = False) -> None:
        item = self._items.pop(db_id, None)
        if item is None:
            return
        if item.engine_id is not None:
            self.engine.cancel(item.engine_id)
            self._by_engine_id.pop(item.engine_id, None)
        self.db.archive(db_id)
        self.db.delete_download(db_id)
        if delete_file:
            self._delete_files(item)
        self.itemRemoved.emit(db_id)

    # -------------------------------------------------------------- checksums

    def expected_checksum(self, db_id: int) -> Expected | None:
        row = self.db.get_checksum(db_id)
        if row is None:
            return None
        return Expected(row["algorithm"], row["expected"], row["source"])

    def record_checksum(self, db_id: int, verdict: Verdict) -> None:
        """Keep what a finished file hashed to; a mismatch is an error.

        The download stays "completed" - every byte the server sent is on
        disk - but the error says the bytes are not the ones published, so
        it shows in the list and nothing goes on to unpack or run it.
        """
        expected = verdict.expected
        if self.db.get_checksum(db_id) is None or expected.source != "entered":
            self.db.set_checksum(db_id, expected.algorithm, expected.digest, expected.source)
        self.db.record_checksum(db_id, verdict.actual, verdict.ok)
        item = self._items.get(db_id)
        if item is None:
            return
        if not verdict.ok:
            item.error = (
                f"{expected.algorithm.upper()} mismatch: expected {expected.digest}, "
                f"got {verdict.actual}"
            )
            self._persist(item, state=item.state.value)
        self.itemChanged.emit(item)

    def request_for(self, db_id: int) -> DownloadRequest | None:
        """The request this download would be made with: proxy, agent, TLS."""
        item = self._items.get(db_id)
        return self._build_request(item) if item is not None else None

    def can_refresh(self, db_id: int) -> bool:
        """Can this download be pointed at a new address and carry on?

        Plain files only: a finished one has nothing left to fetch, and a
        stream or a yt-dlp page re-reads its own addresses every time.
        """
        item = self._items.get(db_id)
        return (
            item is not None
            and item.state is not TaskState.COMPLETED
            and not item.is_media
        )

    def refresh_address(
        self,
        db_id: int,
        url: str,
        *,
        referer: str | None = None,
        cookie: str | None = None,
        user_agent: str | None = None,
        start: bool = True,
    ) -> bool:
        """IDM's "Refresh download address": same file, new link.

        Signed and expiring links (file hosts, cloud drives, CDNs) stop
        working after a while, and resuming then gets a 403 or 410. The bytes
        on disk are still good; only the address is stale. The new address
        replaces the old one along with its session, and the resume metadata
        is told so - the part file is recognised as this download's, and the
        server's size and ETag still decide whether its bytes are reused.
        """
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("only http(s) addresses can be used")
        if not self.can_refresh(db_id):
            return False
        item = self._items[db_id]
        was_live = item.engine_id is not None and item.is_live

        item.url = url
        item.cookie = cookie
        item.referer = referer
        item.user_agent = user_agent
        item.error = None
        self.db.set_address(db_id, url, cookie=cookie, referer=referer, user_agent=user_agent)
        self._adopt_part_file(item)
        log.info("download %d now points at %s", db_id, url)

        if was_live:
            # The running task still holds the old address; the new one is
            # used from the next start, as soon as this pause has landed.
            self._restart_when_paused.add(db_id)
            self.pause_item(db_id, manual=False)
        elif start:
            self.start_item(db_id)
        else:
            self.itemChanged.emit(item)
        return True

    def _adopt_part_file(self, item: DownloadItem) -> None:
        """Tell the resume metadata that the new address is this download's.

        Without it, a server that answers the new link with a different ETag
        makes the part file look like another download's, and the task would
        start over under a new name beside it.
        """
        for base in {Path(item.save_path),
                     target_dir(Path(item.save_path), item.filename, item.use_categories)}:
            meta_path = meta_path_for(base / (item.filename + PART_SUFFIX))
            meta = ResumeMeta.load(meta_path)
            if meta is None:
                continue
            meta.url = item.url
            try:
                meta.save(meta_path)
            except OSError as exc:  # pragma: no cover - disk level failure
                log.warning("could not update %s: %s", meta_path, exc)

    def redownload(self, db_id: int) -> None:
        item = self._items.get(db_id)
        if item is None:
            return
        self._delete_files(item)
        item.downloaded = 0
        item.finished_at = None
        item.state = TaskState.PAUSED
        self.start_item(db_id)

    def set_speed_limit(self, limit: int | None) -> None:
        self.engine.set_speed_limit(limit)
        self.settings.set("speed_limit", limit)

    # ------------------------------------------------------------------ queues

    def queues(self) -> list[QueueInfo]:
        return [
            QueueInfo(
                id=row["id"],
                name=row["name"],
                max_concurrent=row["max_concurrent"],
                enabled=bool(row["enabled"]),
                position=row["position"],
                running=row["id"] in self._running_queues,
            )
            for row in self.db.list_queues()
        ]

    def queue(self, queue_id: int) -> QueueInfo | None:
        return next((q for q in self.queues() if q.id == queue_id), None)

    def create_queue(self, name: str, max_concurrent: int = 1) -> int:
        queue_id = self.db.add_queue(name, max_concurrent=max_concurrent)
        self.queuesChanged.emit()
        return queue_id

    def update_queue(self, queue_id: int, **fields: object) -> None:
        self.db.update_queue(queue_id, **fields)
        self.queuesChanged.emit()
        self.pump_queues()

    def delete_queue(self, queue_id: int) -> None:
        self.stop_queue(queue_id)
        self.db.delete_queue(queue_id)
        for item in self._items.values():
            if item.queue_id == queue_id:
                item.queue_id = None
                self.itemChanged.emit(item)
        self.queuesChanged.emit()

    def queue_items(self, queue_id: int) -> list[DownloadItem]:
        """Everything in the queue, in the order it will be started."""
        return sorted(
            (i for i in self._items.values() if i.queue_id == queue_id),
            key=lambda i: i.db_id,
        )

    def assign_queue(self, db_id: int, queue_id: int | None) -> None:
        item = self._items.get(db_id)
        if item is None or item.queue_id == queue_id:
            return
        item.queue_id = queue_id
        self.db.set_download_queue(db_id, queue_id)
        self.itemChanged.emit(item)
        self.pump_queues()

    def start_queue(self, queue_id: int) -> None:
        # Starting a queue is an explicit instruction: it overrides earlier
        # manual pauses of the files inside it.
        for item in self.queue_items(queue_id):
            item.manual_pause = False
        self._running_queues.add(queue_id)
        self.queuesChanged.emit()
        self.pump_queues()

    def stop_queue(self, queue_id: int) -> None:
        if queue_id in self._running_queues:
            self._running_queues.discard(queue_id)
            self.queuesChanged.emit()
        for item in self.queue_items(queue_id):
            if item.is_live:
                # Not a manual pause: restarting the queue must pick these up.
                self.pause_item(item.db_id, manual=False)

    def is_queue_running(self, queue_id: int) -> bool:
        return queue_id in self._running_queues

    def pump_queues(self) -> None:
        """Top every running queue up to its concurrency, and notice the end.

        Called after each state change rather than on a timer: a queue must
        start the next file the moment one finishes, not up to a tick later.
        """
        for info in self.queues():
            if info.id not in self._running_queues:
                continue
            items = self.queue_items(info.id)
            live = [i for i in items if i.is_live]
            waiting = [i for i in items if not i.is_live and i.is_pending]
            for item in waiting[: max(0, info.max_concurrent - len(live))]:
                self.start_item(item.db_id)
                live.append(item)
            # An empty queue is not a queue that "finished": firing here would
            # shut the machine down the moment an empty schedule came due.
            if items and not live and not waiting:
                self._running_queues.discard(info.id)
                self.queuesChanged.emit()
                self.queueFinished.emit(info.id)

    # -------------------------------------------------------------- internals

    def profiles(self) -> list[SiteProfile]:
        """Per-host rules, newest read from the database each time.

        Cheap enough (a handful of rows) and means editing a profile takes
        effect on the next download without a restart.
        """
        return [
            SiteProfile(
                id=row["id"],
                pattern=row["pattern"],
                enabled=bool(row["enabled"]),
                connections=row["connections"],
                speed_limit=row["speed_limit"],
                user_agent=row["user_agent"],
                referer=row["referer"],
                cookie=row["cookie"],
                proxy=row["proxy"],
                note=row["note"] or "",
            )
            for row in self.db.list_profiles()
        ]

    def _build_request(self, item: DownloadItem) -> DownloadRequest:
        # A rule for this host fills in whatever the download itself did not
        # specify - connections, speed cap, cookie, referer, proxy.
        tuned = apply_to(item.url, self.profiles(), {
            "connections": item.connections,
            "speed_limit": item.speed_limit,
            "user_agent": item.user_agent,
            "referer": item.referer,
            "cookie": item.cookie,
            "proxy": item.proxy,
        })
        chosen_proxy = proxy.resolve(
            tuned["proxy"] or self.settings.get("proxy"),
            use_system=bool(self.settings.get("use_system_proxy")),
            fetch_pac=_pac.read,
        )
        if chosen_proxy.needs_socks_package:
            log.warning("socks proxy requested but socksio is not installed")
        return DownloadRequest(
            url=item.url,
            save_dir=Path(item.save_path),
            # A name the user typed (or one a probe already settled on, which
            # the .part file is named after) has to survive; otherwise let
            # Content-Disposition decide.
            filename=item.filename if item.name_locked else None,
            connections=tuned["connections"],
            speed_limit=tuned["speed_limit"],
            use_categories=item.use_categories,
            cookie=tuned["cookie"],
            referer=tuned["referer"],
            user_agent=tuned["user_agent"],
            proxy=chosen_proxy.url,
            verify_tls=bool(self.settings.get("verify_tls")),
            # The pipeline is derived from the URL, so a restored row picks
            # the right one again without another column in the database.
            max_height=item.max_height or self.settings.video_quality,
            audio_only=item.audio_only,
            ffmpeg_path=self.settings.ffmpeg_path,
        )

    def _delete_files(self, item: DownloadItem) -> None:
        base = Path(item.save_path)
        candidates = [item.path, base / (item.filename + ".part")]
        candidates.append(meta_path_for(base / (item.filename + ".part")))
        for path in candidates:
            try:
                path.unlink()
            except (FileNotFoundError, OSError):
                pass

    def _on_engine_event(self, event: EngineEvent) -> None:
        snap: TaskSnapshot | None = event.snapshot
        if snap is None:
            return
        db_id = self._by_engine_id.get(event.task_id)
        if db_id is None:
            return
        item = self._items.get(db_id)
        if item is None:
            return

        item.state = snap.state
        item.size = snap.size
        item.downloaded = snap.downloaded
        item.speed = snap.speed if snap.state is TaskState.DOWNLOADING else 0.0
        item.eta = snap.eta
        item.error = snap.error
        item.segments = snap.segments
        if snap.filename and snap.filename != item.url:
            item.filename = snap.filename
            # From the moment a runner reports a name there is a .part file
            # carrying it; a resume must not let a second probe rename it.
            item.name_locked = True
        if snap.path:
            final = Path(snap.path)
            item.filename = final.name
            item.save_path = str(final.parent)
        if snap.state.is_final:
            item.finished_at = time.time()
            item.speed = 0.0

        self._sync_db(item, force=event.type != "progress")
        if snap.state is TaskState.COMPLETED:
            # The history is what survives clearing the list, so a finished
            # file is recorded now rather than when the row is removed.
            self.db.archive(item.db_id)
        self.itemChanged.emit(item)
        if snap.state.is_final and item.queue_id is not None:
            self.pump_queues()
        # Paused as asked - or stopped some other way meanwhile: either way
        # the task holding the old address is gone, and the new one starts.
        if db_id in self._restart_when_paused and (
            snap.state is TaskState.PAUSED or snap.state.is_final
        ):
            self._restart_when_paused.discard(db_id)
            if snap.state is not TaskState.COMPLETED:  # finished meanwhile
                self.start_item(db_id)

    def _sync_db(self, item: DownloadItem, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_sync.get(item.db_id, 0.0) < DB_SYNC_INTERVAL:
            return
        self._last_sync[item.db_id] = now
        self._persist(item, state=item.state.value)

    def _persist(self, item: DownloadItem, state: str) -> None:
        try:
            self.db.update_progress(item.db_id, item.downloaded, state, item.error)
            self.db.execute(
                "UPDATE downloads SET filename = ?, save_path = ?, size = ?, "
                "category = ?, name_locked = ? WHERE id = ?",
                (item.filename, item.save_path, item.size, item.category,
                 int(item.name_locked), item.db_id),
            )
        except Exception:  # pragma: no cover - a DB hiccup must not kill the UI
            log.exception("could not persist download %d", item.db_id)


#: shared by every download; fetches off the GUI thread and caches the answer
_pac = proxy.PacCache()


def _media_name(url: str) -> str | None:
    """A placeholder name for a stream, until the runner knows the real one."""
    kind = classify(url)
    return suggested_name(url) if kind.is_media else None


_STATE_VALUES = {s.value for s in TaskState}
