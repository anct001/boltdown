"""The media task runner: same contract as `TaskRunner`, different plumbing.

The engine treats every task the same way (`run`, `snapshot`, `request_pause`,
`request_cancel`), so a video download can be dropped into the existing queue,
rate limiter and GUI without any of them knowing about playlists.

Three shapes end up here:

* an `.m3u8` playlist        -> `HlsDownloader`
* a page yt-dlp understands  -> extract direct URLs, then download them with
  the ordinary segmented `TaskRunner` (which is the fast part)
* an `.mpd` manifest         -> same as above; yt-dlp does the manifest parsing

Video-only plus audio-only tracks are muxed together by ffmpeg at the end.
Everything happens inside a work directory next to the destination, so an
interrupted video resumes exactly like an interrupted file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from ..core.categories import target_dir
from ..core.errors import CancelledByUser, DownloadError, FatalError
from ..core.http_client import build_client
from ..core.ratelimit import ChainedBucket, TokenBucket
from ..core.task import DownloadRequest, TaskRunner, TaskSnapshot, TaskState
from ..util import filenames
from ..util.log import get_logger
from . import ffmpeg as ffmpeg_mod
from . import ytdlp
from .detect import MediaKind, suggested_name
from .hls import HlsDownloader

log = get_logger(__name__)

PROGRESS_INTERVAL = 0.25
WORK_SUFFIX = ".idmedia"


class MediaTaskRunner:
    """Owns one streaming download from playlist to finished file."""

    def __init__(
        self,
        task_id: int,
        request: DownloadRequest,
        *,
        global_bucket: TokenBucket | None = None,
        on_event: Callable[[str, TaskSnapshot], None] | None = None,
    ) -> None:
        self.id = task_id
        self.request = request
        self.state = TaskState.QUEUED
        self.error: str | None = None
        self.filename = request.filename or ""
        self.size: int | None = None
        self.dest_path: Path | None = None

        self._base = 0          # bytes from tracks that are already finished
        self._current = 0       # bytes of the track being downloaded now
        self._speed = 0.0
        self._last_sample = (time.monotonic(), 0)
        self._segments: list[tuple[int, int, int | None]] = []
        self._stop = asyncio.Event()
        self._pause_requested = False
        self._cancel_requested = False
        self._bucket = ChainedBucket(global_bucket, TokenBucket(request.speed_limit))
        self._on_event = on_event

        self._work_dir: Path | None = None
        self._inner: TaskRunner | None = None
        self._hls: HlsDownloader | None = None
        self._size_hint: int | None = None
        self._plan: ytdlp.DownloadPlan | None = None

    # ---------------------------------------------------------------- control

    def request_pause(self) -> None:
        self._pause_requested = True
        self._stop.set()
        if self._inner is not None:
            self._inner.request_pause()

    def request_cancel(self) -> None:
        self._cancel_requested = True
        self._stop.set()
        if self._inner is not None:
            self._inner.request_cancel()

    # ------------------------------------------------------------------- info

    @property
    def downloaded(self) -> int:
        return self._base + self._current

    def snapshot(self) -> TaskSnapshot:
        downloaded = self.downloaded
        eta = None
        if self.size and self._speed > 0:
            eta = max(0.0, (self.size - downloaded) / self._speed)
        return TaskSnapshot(
            id=self.id,
            url=self.request.url,
            filename=self.filename or self.request.url,
            path=str(self.dest_path) if self.dest_path else None,
            state=self.state,
            size=self.size,
            downloaded=downloaded,
            speed=self._speed,
            eta=eta,
            connections=self.request.connections,
            error=self.error,
            segments=self._segments or [(0, downloaded, self.size)],
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
        monitor = asyncio.create_task(self._monitor(), name=f"media{self.id}-monitor")
        try:
            self._set_state(TaskState.PROBING)
            kind = MediaKind(self.request.media_kind or MediaKind.HLS)
            if kind is MediaKind.HLS:
                final = await self._run_hls()
            else:
                final = await self._run_extracted()
            if self._cancel_requested:
                raise CancelledByUser("cancelled")
            self.dest_path = final
            with contextlib.suppress(OSError):
                self.size = final.stat().st_size
            self._base = self.size or self.downloaded
            self._current = 0
            self._cleanup_work_dir()
            self._set_state(TaskState.COMPLETED)
        except CancelledByUser:
            if self._cancel_requested:
                self._cleanup_work_dir()
                self._set_state(TaskState.CANCELLED)
            else:
                self._set_state(TaskState.PAUSED)
        except DownloadError as exc:
            self.error = str(exc)
            log.error("media task %d failed: %s", self.id, exc)
            self._set_state(TaskState.ERROR)
        except Exception as exc:  # noqa: BLE001 - surface unexpected bugs as errors
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("media task %d crashed", self.id)
            self._set_state(TaskState.ERROR)
        finally:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        return self.state

    # ------------------------------------------------------------------- HLS

    async def _run_hls(self) -> Path:
        name = self.request.filename or self._name_from_url()
        output = self._prepare_output(name)
        work = self._work_dir
        assert work is not None

        spec = self.request.to_spec()
        async with build_client(
            spec, max_connections=self.request.connections + 4
        ) as client:
            downloader = HlsDownloader(
                client=client,
                url=self.request.url,
                output=output,
                work_dir=work / "hls",
                connections=self.request.connections,
                bucket=self._bucket,
                stop_event=self._stop,
                on_bytes=self._on_bytes,
                max_height=self.request.max_height,
                max_retries=self.request.max_retries,
                ffmpeg_path=self.request.ffmpeg_path,
            )
            self._hls = downloader
            await downloader.resolve()
            self._set_state(TaskState.DOWNLOADING)
            result = await downloader.run()
        self._hls = None
        self.filename = result.name
        return result

    def _name_from_url(self) -> str:
        extension = "m4a" if self.request.audio_only else "mp4"
        return filenames.sanitize(suggested_name(self.request.url, extension))

    # --------------------------------------------------------- yt-dlp / DASH

    async def _run_extracted(self) -> Path:
        options = ytdlp.build_options(
            proxy=self.request.proxy,
            cookie=self.request.cookie,
            referer=self.request.referer,
            user_agent=self.request.user_agent,
            verify_tls=self.request.verify_tls,
        )
        info = await ytdlp.extract(self.request.url, options)
        plan = ytdlp.select(
            info,
            max_height=self.request.max_height,
            audio_only=self.request.audio_only,
        )
        self._plan = plan
        log.info(
            "media task %d: %s (video=%s audio=%s)",
            self.id, plan.title,
            plan.video.label if plan.video else "-",
            plan.audio.label if plan.audio else "-",
        )

        name = self.request.filename or filenames.sanitize(
            f"{plan.title}.{plan.container}"
        )
        output = self._prepare_output(name)
        work = self._work_dir
        assert work is not None

        sizes = [t.filesize for t in plan.tracks]
        self._size_hint = sum(sizes) if sizes and all(sizes) else None

        self._set_state(TaskState.DOWNLOADING)
        parts: list[Path] = []
        for label, track in (("video", plan.video), ("audio", plan.audio)):
            if track is None:
                continue
            target = work / f"{label}.{track.ext or 'bin'}"
            parts.append(await self._download_track(track, target))
            self._base += self._current
            self._current = 0
            self._segments = []

        if self._cancel_requested:
            raise CancelledByUser("cancelled")

        if len(parts) == 2:
            await ffmpeg_mod.merge_tracks(
                parts[0], parts[1], output, ffmpeg=self.request.ffmpeg_path
            )
        else:
            source = parts[0]
            if source.suffix.lower() != output.suffix.lower():
                output = output.with_suffix(source.suffix)
            os.replace(source, output)
        self.filename = output.name
        return output

    async def _download_track(self, track: ytdlp.Track, target: Path) -> Path:
        """Fetch one track, reusing the ordinary engine for plain URLs."""
        if target.exists():
            # A previous run already finished this track.
            self._base += target.stat().st_size
            return target

        if track.is_hls:
            spec = self.request.to_spec()
            spec.url = track.url
            spec.headers = {**spec.headers, **track.headers}
            async with build_client(
                spec, max_connections=self.request.connections + 4
            ) as client:
                downloader = HlsDownloader(
                    client=client,
                    url=track.url,
                    output=target,
                    work_dir=target.parent / f"{target.stem}.hls",
                    connections=self.request.connections,
                    bucket=self._bucket,
                    stop_event=self._stop,
                    on_bytes=self._on_bytes,
                    max_height=self.request.max_height,
                    max_retries=self.request.max_retries,
                    ffmpeg_path=self.request.ffmpeg_path,
                )
                self._hls = downloader
                await downloader.resolve()
                result = await downloader.run()
                downloader.cleanup()
            self._hls = None
            return result

        sub = DownloadRequest(
            url=track.url,
            save_dir=target.parent,
            filename=target.name,
            connections=self.request.connections,
            speed_limit=None,  # the shared bucket already limits this task
            use_categories=False,
            headers={**self.request.headers, **track.headers},
            cookie=self.request.cookie,
            referer=self.request.referer,
            user_agent=self.request.user_agent,
            proxy=self.request.proxy,
            auth=self.request.auth,
            verify_tls=self.request.verify_tls,
            max_retries=self.request.max_retries,
            media_kind=MediaKind.DIRECT,
        )
        # The sub-request carries no limit of its own, so chaining our bucket
        # in as the "global" one keeps a single limit for the whole task.
        runner = TaskRunner(
            self.id, sub, global_bucket=self._bucket, on_event=self._on_inner_event
        )
        self._inner = runner
        if self._stop.is_set():
            raise CancelledByUser("stopped")
        state = await runner.run()
        self._inner = None
        if state is TaskState.COMPLETED and runner.dest_path is not None:
            self._current = runner.dest_path.stat().st_size
            return runner.dest_path
        if state in (TaskState.PAUSED, TaskState.CANCELLED):
            raise CancelledByUser(runner.error or "stopped")
        raise FatalError(runner.error or f"track download ended as {state.value}")

    # --------------------------------------------------------------- progress

    def _on_bytes(self, nbytes: int) -> None:
        self._current += nbytes

    def _on_inner_event(self, _event: str, snap: TaskSnapshot) -> None:
        self._current = snap.downloaded
        self._segments = [
            (start, cur, end) for start, cur, end in snap.segments
        ]
        if self._size_hint is None and snap.size:
            self.size = self._base + snap.size

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            self._refresh_size()
            now = time.monotonic()
            prev_time, prev_bytes = self._last_sample
            elapsed = now - prev_time
            if elapsed > 0:
                instant = (self.downloaded - prev_bytes) / elapsed
                self._speed = (
                    instant if self._speed == 0 else 0.7 * self._speed + 0.3 * instant
                )
                self._last_sample = (now, self.downloaded)
            if self.state is TaskState.DOWNLOADING:
                self._emit("progress")

    def _refresh_size(self) -> None:
        if self._size_hint is not None:
            self.size = self._size_hint
            return
        if self._hls is not None:
            estimate = self._hls.status.estimated_size
            if estimate:
                self.size = self._base + estimate

    # ---------------------------------------------------------------- helpers

    def _prepare_output(self, name: str) -> Path:
        """Pick the destination and the work directory that sits next to it."""
        self.filename = filenames.sanitize(name)
        directory = target_dir(
            Path(self.request.save_dir), self.filename, self.request.use_categories
        )
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / self.filename
        # Resolve the collision once, up front: the work directory is named
        # after the final file, so a resumed download must land on the same
        # name it picked the first time.
        work = directory / (Path(self.filename).stem + WORK_SUFFIX)
        if output.exists() and not work.exists():
            output = filenames.unique_path(output)
            work = directory / (output.stem + WORK_SUFFIX)
            self.filename = output.name
        work.mkdir(parents=True, exist_ok=True)
        self._work_dir = work
        self.dest_path = output
        return output

    def _cleanup_work_dir(self) -> None:
        if self._work_dir is None:
            return
        with contextlib.suppress(OSError):
            shutil.rmtree(self._work_dir)
        self._work_dir = None
