"""A single download: probing, segment orchestration, resume and finalising."""

from __future__ import annotations

import asyncio
import contextlib
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from ..util import filenames
from ..util.log import get_logger
from .categories import target_dir
from .errors import CancelledByUser, DownloadError, FatalError, TransientError
from .http_client import RequestSpec, build_client
from .probe import ProbeResult, probe
from .ratelimit import ChainedBucket, TokenBucket
from .resume import ResumeMeta, cleanup, meta_path_for
from .segment import (
    Segment,
    SegmentDeclined,
    SegmentWorker,
    backoff_delay,
    plan_segments,
)
from .writer import TargetFile

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ..media.detect import MediaKind

log = get_logger(__name__)

#: never create a stolen slice smaller than this. Small enough that the tail
#: of a download can still be shared out (IDM keeps splitting down to a few
#: hundred kilobytes); the time check in `_steal_work` stops splits that
#: would cost more in a new request than they save.
MIN_SPLIT = 256 * 1024
#: a split must be expected to finish the victim's remainder at least this
#: much sooner, or the new request is not worth making
MIN_GAIN = 0.25
#: how long a mirror may take to answer its probe before it is left out
MIRROR_PROBE_TIMEOUT = 10.0
#: round trip assumed for a new request before one has been measured
DEFAULT_RTT = 0.1
#: a connection the server refused (its connection limit) asks again after
#: this long, doubling each time it is refused again, up to the maximum
DECLINED_WAIT = 0.5
DECLINED_MAX_WAIT = 5.0
#: how often a refused connection asks anyway, in case the limit has risen
CAPACITY_PROBE = 10.0
#: Paths claimed by running tasks in this process - `.part` files here, work
#: directories in the media runner - so two downloads that resolve to the same
#: name cannot write into the same place.
_CLAIMED: set[Path] = set()
_CLAIM_LOCK = threading.Lock()


def reserve_path(path: Path) -> bool:
    """Claim `path` for this task. False means another task already has it."""
    with _CLAIM_LOCK:
        if path in _CLAIMED:
            return False
        _CLAIMED.add(path)
        return True


def release_path(path: Path | None) -> None:
    if path is None:
        return
    with _CLAIM_LOCK:
        _CLAIMED.discard(path)


PROGRESS_INTERVAL = 0.25
META_INTERVAL = 1.0
PART_SUFFIX = ".part"


class TaskState(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.ERROR, TaskState.CANCELLED)


@dataclass(slots=True)
class DownloadRequest:
    url: str
    save_dir: Path
    filename: str | None = None
    connections: int = 8
    speed_limit: int | None = None
    use_categories: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    cookie: str | None = None
    referer: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    auth: tuple[str, str] | None = None
    verify_tls: bool = True
    max_retries: int = 5
    #: which pipeline handles this URL; None means "decide from the URL"
    media_kind: "MediaKind | None" = None
    max_height: int | None = None    # cap the video rendition, e.g. 1080
    audio_only: bool = False
    ffmpeg_path: str | None = None
    #: other addresses serving the same file; segments are spread across them
    mirrors: list[str] = field(default_factory=list)
    #: video pages: subtitle languages to add ("vi", "en", "all"), and
    #: whether to put the thumbnail in as cover art
    subtitle_langs: list[str] = field(default_factory=list)
    embed_thumbnail: bool = False
    #: fetch over BitTorrent; None decides from the URL (magnet, *.torrent),
    #: False saves a .torrent file as the plain file it is
    torrent: bool | None = None

    def __post_init__(self) -> None:
        if self.media_kind is None:
            # Imported here: the media package pulls in optional dependencies
            # and most downloads are plain files that never need it.
            from ..media.detect import classify

            self.media_kind = classify(self.url)

    @property
    def is_media(self) -> bool:
        return bool(self.media_kind) and self.media_kind != "direct"

    def to_spec(self) -> RequestSpec:
        return RequestSpec(
            url=self.url,
            headers=dict(self.headers),
            cookie=self.cookie,
            referer=self.referer,
            user_agent=self.user_agent,
            proxy=self.proxy,
            auth=self.auth,
            verify_tls=self.verify_tls,
        )


@dataclass(slots=True)
class TaskSnapshot:
    id: int
    url: str
    filename: str
    path: str | None
    state: TaskState
    size: int | None
    downloaded: int
    speed: float
    eta: float | None
    connections: int
    error: str | None
    segments: list[tuple[int, int, int | None]]

    @property
    def percent(self) -> float:
        if not self.size:
            return 0.0
        return min(100.0, self.downloaded * 100.0 / self.size)


class TaskRunner:
    """Owns one download from probe to finished file."""

    def __init__(
        self,
        task_id: int,
        request: DownloadRequest,
        *,
        global_bucket: TokenBucket | ChainedBucket | None = None,
        on_event: Callable[[str, "TaskSnapshot"], None] | None = None,
    ) -> None:
        self.id = task_id
        self.request = request
        self.state = TaskState.QUEUED
        self.error: str | None = None
        self.filename = request.filename or ""
        self.size: int | None = None
        self.resumable = False
        self.segments: list[Segment] = []
        self.dest_path: Path | None = None
        self.part_path: Path | None = None
        self.meta_path: Path | None = None

        self._downloaded = 0
        self._speed = 0.0
        self._last_sample = (time.monotonic(), 0)
        self._stop = asyncio.Event()
        self._split_lock = asyncio.Lock()
        self._pause_requested = False
        self._cancel_requested = False
        self._bucket = ChainedBucket(global_bucket, TokenBucket(request.speed_limit))
        self._on_event = on_event
        self._target: TargetFile | None = None
        self._probe: ProbeResult | None = None
        #: the probe's response, still streaming from byte 0 - segment 0's
        #: first connection, until it is used or closed
        self._primed: httpx.Response | None = None
        self._workers: list[SegmentWorker] = []
        #: addresses the segments are fetched from: the probed URL first, then
        #: every mirror that turned out to serve the same file
        self._sources: list[str] = []
        #: segment index -> the worker downloading it. A segment in no entry
        #: was handed back (the server refused its connection) and waits for
        #: a worker to adopt it.
        self._owned: dict[int, SegmentWorker] = {}
        #: most connections the server has served this task at once
        self._peak_served = 0

    # ---------------------------------------------------------------- control

    def request_pause(self) -> None:
        self._pause_requested = True
        self._stop.set()

    def request_cancel(self) -> None:
        self._cancel_requested = True
        self._stop.set()

    # ------------------------------------------------------------------- info

    def snapshot(self) -> TaskSnapshot:
        eta = None
        if self.size and self._speed > 0:
            eta = max(0.0, (self.size - self._downloaded) / self._speed)
        return TaskSnapshot(
            id=self.id,
            url=self.request.url,
            filename=self.filename or self.request.url,
            path=str(self.dest_path) if self.dest_path else None,
            state=self.state,
            size=self.size,
            downloaded=self._downloaded,
            speed=self._speed,
            eta=eta,
            connections=len([s for s in self.segments if not s.is_complete]) or 1,
            error=self.error,
            segments=[(s.start, s.current, s.end) for s in self.segments],
        )

    def _emit(self, event: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, self.snapshot())
        except Exception:  # pragma: no cover - listener bugs must not kill a task
            log.exception("event listener raised for %s", event)

    def _set_state(self, state: TaskState, event: str | None = None) -> None:
        self.state = state
        self._emit(event or state.value)

    # -------------------------------------------------------------------- run

    async def run(self) -> TaskState:
        self._stop = asyncio.Event()
        self._pause_requested = False
        self._cancel_requested = False
        try:
            self._set_state(TaskState.PROBING)
            await self._fetch()
            if self._cancel_requested:
                raise CancelledByUser("cancelled")
            self._finalize()
            self._set_state(TaskState.COMPLETED)
        except CancelledByUser:
            self._save_meta()
            if self._cancel_requested:
                self._close_target()
                if self.part_path:
                    cleanup(self.part_path)
                self._set_state(TaskState.CANCELLED)
            else:
                self._set_state(TaskState.PAUSED)
        except DownloadError as exc:
            self.error = str(exc)
            log.error("task %d failed: %s", self.id, exc)
            self._save_meta()
            self._set_state(TaskState.ERROR)
        except Exception as exc:  # noqa: BLE001 - surface unexpected bugs as errors
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("task %d crashed", self.id)
            self._save_meta()
            self._set_state(TaskState.ERROR)
        finally:
            self._close_target()
            release_path(self.part_path)
        return self.state

    async def _fetch(self) -> None:
        """Probe, claim the file, download what is missing - over HTTP.

        Everything around it (pause, cancel, errors, the finished file) is
        protocol-free; `FtpTaskRunner` replaces just this.
        """
        spec = self.request.to_spec()
        async with build_client(
            spec, max_connections=self.request.connections + 4
        ) as client:
            result = await self._probe_with_retry(client, spec)
            self._probe = result
            self._primed, result.response = result.response, None
            try:
                self._apply_probe(result)
                self._prepare_files(result)
                if self._all_complete():
                    log.info("task %d already complete on disk", self.id)
                else:
                    self._sources = [result.final_url] + await self._usable_mirrors(
                        client, result
                    )
                    self._set_state(TaskState.DOWNLOADING)
                    await self._download(client, result)
            finally:
                await self._discard_primed()

    # ---------------------------------------------------------------- internals

    async def _probe_with_retry(self, client, spec) -> ProbeResult:
        """A flaky server must not kill the task before it even starts."""
        attempt = 0
        while True:
            if self._stop.is_set():
                raise CancelledByUser("stopped")
            try:
                return await probe(client, spec, keep_body=True)
            except (TransientError, httpx.HTTPError) as exc:
                attempt += 1
                if attempt > self.request.max_retries:
                    raise TransientError(str(exc)) from exc
                delay = backoff_delay(attempt)
                log.warning("probe retry %d in %.1fs (%s)", attempt, delay, exc)
                await asyncio.sleep(delay)

    def _apply_probe(self, result: ProbeResult) -> None:
        if not self.filename:
            self.filename = result.filename
        self.filename = filenames.sanitize(self.filename)
        self.size = result.size
        self.resumable = result.resumable

    def _claim_target(
        self, directory: Path, result: ProbeResult
    ) -> tuple[Path, Path, ResumeMeta | None]:
        """Pick a destination no other task is writing, and reserve it.

        Two downloads that resolve to the same name used to share one `.part`
        file: whichever finished first renamed it away and the other died on
        the missing file. So the name is settled *here*, before a single byte
        is written, and the `.part` path is held for the life of the task.
        """
        for candidate in filenames.variants(self.filename):
            dest = directory / candidate
            part = directory / (candidate + PART_SUFFIX)
            meta = ResumeMeta.load(meta_path_for(part)) if part.exists() else None
            if meta is not None:
                reason = meta.mismatch_reason(result)
                if reason or not self.resumable:
                    # The bytes on disk are not the bytes we want. Restarting
                    # means deleting them, which is only ours to do when the
                    # part file is ours: a paused download of something else
                    # that happens to share the name keeps its progress and we
                    # take the next free name.
                    if not self._is_our_part(meta, result):
                        log.info(
                            "%s belongs to another download (%s); using another name",
                            candidate, reason or "not resumable",
                        )
                        continue
                    log.warning(
                        "restarting %s from scratch: %s",
                        candidate, reason or "server no longer supports ranges",
                    )
                    meta = None
            # A finished file already sits there and we cannot continue into
            # it - that name belongs to an earlier download.
            if meta is None and dest.exists():
                continue
            if not reserve_path(part):
                continue  # another task in this process is writing it
            self.filename = candidate
            return dest, part, meta
        raise FatalError(f"no free file name near {directory / self.filename}")

    def _is_our_part(self, meta: ResumeMeta, result: ProbeResult) -> bool:
        """Was this `.part` file left by *this* download?

        Compared against both URLs at each end, because a redirect means the
        address we asked for and the one we were served are rarely the same.
        Only asked when the metadata does *not* match what the server is now
        offering - a match means the same bytes, whatever URL produced them,
        which matters for CDNs that sign every link afresh.
        """
        ours = {self.request.url, result.final_url} - {"", None}
        theirs = {meta.url, meta.final_url} - {"", None}
        return bool(ours & theirs)

    def _prepare_files(self, result: ProbeResult) -> None:
        directory = target_dir(
            Path(self.request.save_dir), self.filename, self.request.use_categories
        )
        directory.mkdir(parents=True, exist_ok=True)
        self.dest_path, self.part_path, meta = self._claim_target(directory, result)
        self.meta_path = meta_path_for(self.part_path)

        if meta is not None:
            self.segments = meta.segments
            log.info(
                "resuming %s at %s/%s bytes",
                self.filename, meta.downloaded, self.size,
            )
        else:
            cleanup(self.part_path)
            connections = self.request.connections if self.resumable else 1
            self.segments = plan_segments(self.size, max(1, connections))

        self._downloaded = sum(s.done for s in self.segments)
        self._last_sample = (time.monotonic(), self._downloaded)

        self._target = TargetFile(self.part_path)
        self._target.allocate(self.size)
        self._save_meta()

    async def _usable_mirrors(self, client, result: ProbeResult) -> list[str]:
        """The mirrors that serve this very file, resumably.

        Only checkable things are checked: the size, and range support. Two
        servers seldom share an ETag, so a mirror's ETag is not compared -
        which is why a checksum, when the site publishes one, is the real
        proof that every segment came out right.
        """
        wanted = [
            url.strip() for url in self.request.mirrors
            if url.strip().lower().startswith(("http://", "https://"))
            and url.strip() not in (self.request.url, result.final_url)
        ]
        if not wanted or not self.resumable or not self.size:
            return []

        async def check(url: str) -> str | None:
            try:
                found = await asyncio.wait_for(
                    probe(client, RequestSpec(url=url)), timeout=MIRROR_PROBE_TIMEOUT
                )
            except (DownloadError, httpx.HTTPError, asyncio.TimeoutError, TimeoutError) as exc:
                log.info("task %d: mirror %s unusable: %s", self.id, url, exc)
                return None
            if found.size != self.size or not found.resumable:
                log.info(
                    "task %d: mirror %s is another file (size %s, ranges %s)",
                    self.id, url, found.size, found.resumable,
                )
                return None
            return found.final_url

        checked = await asyncio.gather(*(check(url) for url in dict.fromkeys(wanted)))
        usable = [url for url in checked if url]
        if usable:
            log.info("task %d: fetching from %d sources", self.id, 1 + len(usable))
        return usable

    async def _discard_primed(self) -> None:
        primed, self._primed = self._primed, None
        if primed is not None:
            with contextlib.suppress(Exception):
                await primed.aclose()

    def _take_primed(self, segment: Segment) -> httpx.Response | None:
        """The probe's stream, if `segment` is the one it is already sending."""
        if self._primed is None or segment.start != 0 or segment.done != 0:
            return None
        primed, self._primed = self._primed, None
        return primed

    def _all_complete(self) -> bool:
        return bool(self.segments) and all(s.is_complete for s in self.segments)

    async def _download(self, client, result: ProbeResult) -> None:
        assert self._target is not None
        pending = [s for s in self.segments if not s.is_complete]
        if not pending:
            return

        sources = self._sources or [result.final_url]
        workers = []
        for number, seg in enumerate(pending):
            primed = self._take_primed(seg)
            # The probe's stream came from the first source; the rest take
            # the sources in turn, so the connections spread across them.
            source = sources[0] if primed is not None else sources[number % len(sources)]
            workers.append(asyncio.create_task(
                self._worker_loop(client, result, seg, primed, source),
                name=f"task{self.id}-seg{seg.index}",
            ))
        # Resuming past byte 0: nobody wants the probe's stream.
        await self._discard_primed()
        monitor = asyncio.create_task(self._monitor(), name=f"task{self.id}-monitor")
        try:
            await asyncio.gather(*workers)
        except BaseException:
            self._stop.set()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
            self._recount()
            self._save_meta()

    async def _worker_loop(
        self,
        client,
        result: ProbeResult,
        segment: Segment,
        primed: httpx.Response | None = None,
        source: str | None = None,
    ) -> None:
        assert self._target is not None
        fd = self._target.open_fd()
        primary = result.final_url
        worker = SegmentWorker(
            client=client,
            url=source or primary,
            fd=fd,
            bucket=self._bucket,
            stop_event=self._stop,
            on_commit=self._on_commit,
            resumable=self.resumable,
            max_retries=self.request.max_retries,
            should_decline=lambda: self._should_decline(worker),
            on_served=self._note_served,
        )
        worker.holds_elsewhere = lambda: self._holding(besides=worker) > 0
        self._workers.append(worker)
        self._owned[segment.index] = worker
        declined = 0
        try:
            current: Segment | None = segment
            while current is not None:
                try:
                    await worker.run(current, primed)
                except CancelledByUser:
                    raise
                except DownloadError as exc:
                    if worker.url == primary:
                        raise
                    # A mirror that stops serving costs its connection, not
                    # the download: this worker carries on from the main
                    # address, where it left off.
                    log.warning(
                        "task %d: mirror %s failed (%s); back to the main address",
                        self.id, worker.url, exc,
                    )
                    worker.release()
                    worker.url = primary
                    primed = None
                    continue
                except SegmentDeclined as exc:
                    # The server's connection limit is reached. One of the
                    # connections it does serve adopts this segment; this
                    # worker steps back and asks again later, so the task
                    # uses every connection the server allows - and no more
                    # than it allows at any moment.
                    primed = None
                    worker.release()
                    self._owned.pop(current.index, None)
                    # What the server serves now is its limit now, even if it
                    # once allowed more.
                    self._peak_served = min(
                        self._peak_served, max(1, self._holding(besides=worker))
                    )
                    declined += 1
                    wait = min(DECLINED_MAX_WAIT, DECLINED_WAIT * 2 ** (declined - 1))
                    log.info(
                        "task %d: connection for segment %d refused (%s); "
                        "trying again in %.1fs", self.id, current.index, exc, wait,
                    )
                    # Back only for work nobody holds: splitting a served
                    # connection's segment would just be refused again, and
                    # each try would cut the file into smaller pieces.
                    # And only when a slot looks free - fewer connections
                    # held than the server has ever allowed - or, now and
                    # then, to find out whether it allows more by now.
                    current = None
                    since_probe = 0.0
                    while current is None:
                        if not await self._wait_for_turn(wait):
                            return
                        since_probe += wait
                        free = self._holding(besides=worker) < self._peak_served
                        if free or since_probe >= CAPACITY_PROBE:
                            since_probe = 0.0
                            current = await self._steal_work(worker, orphans_only=True)
                        wait = min(DECLINED_MAX_WAIT, wait * 2)
                    continue
                declined = 0
                primed = None
                self._owned.pop(current.index, None)
                current = await self._steal_work(worker)
        finally:
            if primed is not None:
                with contextlib.suppress(Exception):
                    await primed.aclose()
            await worker.close_parked()
            with contextlib.suppress(Exception):
                worker.flush_sync()
            self._workers.remove(worker)
            self._target.close_fd(fd)

    def _holding(self, besides: SegmentWorker | None = None) -> int:
        return sum(1 for w in self._workers if w is not besides and w.holds_connection)

    def _note_served(self) -> None:
        self._peak_served = max(self._peak_served, self._holding())

    def _should_decline(self, worker: SegmentWorker) -> bool:
        """Is a 503/429 for `worker` the server's connection limit?

        Only when the server is already serving this task as many
        connections as it ever has. With fewer, a slot is free on its side or
        about to be - one this task closed a moment ago and the server has
        not finished tearing down - and a short retry gets it.
        """
        others = self._holding(besides=worker)
        return others > 0 and others >= self._peak_served

    async def _wait_for_turn(self, seconds: float) -> bool:
        """Sit out `seconds`; False if the task stopped or has nothing left.

        Polled rather than slept in one go: a worker waiting out a refusal
        must not hold the finished download open for the rest of its wait.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set() or self._all_complete():
                return False
            await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return not (self._stop.is_set() or self._all_complete())

    async def _steal_work(
        self, me: SegmentWorker | None = None, *, orphans_only: bool = False
    ) -> Segment | None:
        """Dynamic segmentation: give an idle connection someone else's tail.

        The victim is the segment expected to finish *last* - remaining bytes
        over its connection's speed - not merely the biggest. The biggest is
        usually on a healthy connection; the one that holds the download
        hostage is a small remainder on a crawling link, and IDM's strength
        is that it goes after exactly that one.

        The remainder is shared so both ends finish together: the idle
        connection pays a round trip before its first byte, then runs at its
        own speed, while the victim keeps going at its. A crawling victim
        ends up keeping only what it already has in hand.

        With no speeds measured yet (the first moments, or in tests) it falls
        back to halving the biggest remainder.
        """
        async with self._split_lock:
            if self._stop.is_set():
                return None
            candidates = [
                s for s in self.segments if not s.is_complete and s.remaining is not None
            ]
            if not candidates:
                return None
            owners = {
                id(w.active): w for w in self._workers if w is not me and w.active is not None
            }
            for index, worker in self._owned.items():
                if worker is not me:
                    owners.update({id(s): worker for s in self.segments if s.index == index})
            if me is not None:
                # A segment handed back by a refused connection is taken
                # whole, before anything is split: it has nobody at all.
                # The one starting where this worker's open stream stands
                # comes first - it needs no new request.
                orphans = [s for s in candidates if id(s) not in owners]
                if orphans:
                    here = me.parked.position if me.parked is not None else None
                    adopted = min(orphans, key=lambda s: (s.current != here, s.current))
                    self._owned[adopted.index] = me
                    return adopted
            if orphans_only:
                return None
            rates = [w.rate for w in self._workers if w.rate > 0]
            typical = statistics.median(rates) if rates else 0.0

            def speed_of(segment: Segment) -> float:
                owner = owners.get(id(segment))
                return owner.rate if owner is not None and owner.rate > 0 else typical

            def finish_in(segment: Segment) -> float:
                speed = speed_of(segment)
                # Without any speed, bytes are the only measure there is.
                return (segment.remaining or 0) / speed if speed > 0 else float(segment.remaining or 0)

            victim = max(candidates, key=finish_in)
            remaining = victim.remaining or 0
            owner = owners.get(id(victim))
            theirs = speed_of(victim)
            mine = me.rate if me is not None and me.rate > 0 else typical

            if theirs > 0 and mine > 0:
                rtt = me.ttfb if me is not None and me.ttfb is not None else DEFAULT_RTT
                # Both finish together: keep/theirs == rtt + (remaining-keep)/mine
                keep = int(theirs * (rtt * mine + remaining) / (mine + theirs))
                keep = min(remaining, max(0, keep))
                alone = remaining / theirs
                shared = max(keep / theirs, rtt + (remaining - keep) / mine)
                if alone - shared < MIN_GAIN:
                    return None
            else:
                keep = remaining // 2
            # Never below what the victim already holds past `current`: those
            # bytes are on their way to disk and will be counted there.
            keep = max(keep, owner.unwritten if owner is not None else 0)
            if remaining - keep < MIN_SPLIT:
                return None
            if theirs <= 0 or mine <= 0:
                # The blind halving keeps a sensible minimum on both sides.
                if keep < MIN_SPLIT:
                    return None

            mid = victim.current + keep
            stolen = Segment(index=len(self.segments), start=mid, end=victim.end)
            victim.end = mid - 1
            self.segments.append(stolen)
            if me is not None:
                self._owned[stolen.index] = me
            log.debug(
                "task %d split segment %d (%.0f KB/s) -> new segment %d [%d..%s]",
                self.id, victim.index, theirs / 1024, stolen.index, stolen.start, stolen.end,
            )
            return stolen

    def _on_commit(self, nbytes: int) -> None:
        self._downloaded += nbytes

    def _recount(self) -> None:
        self._downloaded = sum(s.done for s in self.segments)

    async def _monitor(self) -> None:
        last_meta = time.monotonic()
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            now = time.monotonic()
            prev_time, prev_bytes = self._last_sample
            elapsed = now - prev_time
            if elapsed > 0:
                instant = (self._downloaded - prev_bytes) / elapsed
                # EWMA keeps the readout from flickering between chunks.
                self._speed = instant if self._speed == 0 else 0.7 * self._speed + 0.3 * instant
                self._last_sample = (now, self._downloaded)
            for worker in list(self._workers):
                worker.sample(now)
            self._emit("progress")
            if now - last_meta >= META_INTERVAL:
                self._save_meta()
                last_meta = now

    def _save_meta(self) -> None:
        if self.meta_path is None or not self.segments:
            return
        if self.state in (TaskState.COMPLETED, TaskState.CANCELLED):
            return
        meta = ResumeMeta(
            url=self.request.url,
            final_url=self._probe.final_url if self._probe else self.request.url,
            filename=self.filename,
            size=self.size,
            resumable=self.resumable,
            segments=self.segments,
            etag=self._probe.etag if self._probe else None,
            last_modified=self._probe.last_modified if self._probe else None,
        )
        try:
            meta.save(self.meta_path)
        except OSError as exc:  # pragma: no cover - disk level failure
            log.warning("could not save resume metadata: %s", exc)

    def _close_target(self) -> None:
        if self._target is not None:
            self._target.close()

    def _finalize(self) -> None:
        assert self.part_path is not None and self.dest_path is not None
        self._recount()
        self._close_target()

        if self.size is not None:
            if self._downloaded != self.size:
                raise FatalError(
                    f"incomplete download: {self._downloaded} of {self.size} bytes"
                )
            actual = self.part_path.stat().st_size
            if actual != self.size:
                # Only possible when the size was unknown at allocate time.
                self._target = TargetFile(self.part_path)
                self._target.truncate_to(self.size)
                self._target.close()
        else:
            self.size = self._downloaded

        # The name was reserved before the first byte, so it is normally still
        # free; `unique_path` only matters if something outside the app took it
        # while we were downloading.
        final = filenames.unique_path(self.dest_path)
        os.replace(self.part_path, final)
        self.dest_path = final
        self.filename = final.name
        if self.meta_path is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                self.meta_path.unlink()
        log.info("finished %s (%d bytes)", final, self.size)
