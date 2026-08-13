"""A single download: probing, segment orchestration, resume and finalising."""

from __future__ import annotations

import asyncio
import contextlib
import os
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
from .segment import Segment, SegmentWorker, backoff_delay, plan_segments
from .writer import TargetFile

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ..media.detect import MediaKind

log = get_logger(__name__)

MIN_SPLIT = 2 << 20  # never create a stolen slice smaller than 2 MiB
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
            spec = self.request.to_spec()
            self._set_state(TaskState.PROBING)
            async with build_client(
                spec, max_connections=self.request.connections + 4
            ) as client:
                result = await self._probe_with_retry(client, spec)
                self._probe = result
                self._apply_probe(result)
                self._prepare_files(result)
                if self._all_complete():
                    log.info("task %d already complete on disk", self.id)
                else:
                    self._set_state(TaskState.DOWNLOADING)
                    await self._download(client, result)
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

    # ---------------------------------------------------------------- internals

    async def _probe_with_retry(self, client, spec) -> ProbeResult:
        """A flaky server must not kill the task before it even starts."""
        attempt = 0
        while True:
            if self._stop.is_set():
                raise CancelledByUser("stopped")
            try:
                return await probe(client, spec)
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

    def _all_complete(self) -> bool:
        return bool(self.segments) and all(s.is_complete for s in self.segments)

    async def _download(self, client, result: ProbeResult) -> None:
        assert self._target is not None
        pending = [s for s in self.segments if not s.is_complete]
        if not pending:
            return

        workers = [
            asyncio.create_task(
                self._worker_loop(client, result, seg), name=f"task{self.id}-seg{seg.index}"
            )
            for seg in pending
        ]
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

    async def _worker_loop(self, client, result: ProbeResult, segment: Segment) -> None:
        assert self._target is not None
        fd = self._target.open_fd()
        worker = SegmentWorker(
            client=client,
            url=result.final_url,
            fd=fd,
            bucket=self._bucket,
            stop_event=self._stop,
            on_commit=self._on_commit,
            resumable=self.resumable,
            max_retries=self.request.max_retries,
        )
        try:
            current: Segment | None = segment
            while current is not None:
                await worker.run(current)
                current = await self._steal_work()
        finally:
            with contextlib.suppress(Exception):
                worker.flush_sync()
            self._target.close_fd(fd)

    async def _steal_work(self) -> Segment | None:
        """Dynamic segmentation: carve the tail off the slowest segment.

        Without this a single slow connection holds the whole download hostage
        while the other seven workers sit idle - the main reason naive
        multi-segment downloaders are no faster than a single stream.
        """
        async with self._split_lock:
            if self._stop.is_set():
                return None
            candidates = [
                s for s in self.segments if not s.is_complete and s.remaining is not None
            ]
            if not candidates:
                return None
            victim = max(candidates, key=lambda s: s.remaining or 0)
            remaining = victim.remaining or 0
            if remaining < 2 * MIN_SPLIT:
                return None
            mid = victim.current + remaining // 2
            if mid - victim.current < MIN_SPLIT or (victim.end or 0) - mid + 1 < MIN_SPLIT:
                return None
            stolen = Segment(index=len(self.segments), start=mid, end=victim.end)
            victim.end = mid - 1
            self.segments.append(stolen)
            log.debug(
                "task %d split segment %d -> new segment %d [%d..%s]",
                self.id, victim.index, stolen.index, stolen.start, stolen.end,
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
