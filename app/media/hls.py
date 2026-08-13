"""Downloading an HLS stream with the same parallelism as a plain file.

An `.m3u8` is a list of hundreds of small files, so the byte-range trick the
core engine uses does not apply: here the unit of work is one segment. A pool
of workers pulls segment indices off a queue, decrypts if the playlist is
AES-128, and stores each finished segment under its own name. The rename to
its final name is atomic, so a segment file either exists complete or not at
all - that alone is the resume mechanism, no sidecar needed.

Assembling is a byte concatenation (valid for both MPEG-TS and fMP4 with an
`#EXT-X-MAP` header), optionally remuxed into `.mp4` by ffmpeg.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from ..core.errors import CancelledByUser, FatalError, TransientError, classify_status
from ..core.ratelimit import TokenBucket, Unlimited
from ..core.segment import backoff_delay
from ..util.log import get_logger
from . import ffmpeg as ffmpeg_mod
from .m3u8 import MasterPlaylist, MediaPlaylist, MediaSegment, Playlist
from .m3u8 import parse_playlist, pick_variant

log = get_logger(__name__)

PART_SUFFIX = ".part"
DONE_SUFFIX = ".bin"
CHUNK_SIZE = 64 * 1024
MAX_PLAYLIST_BYTES = 8 << 20


class UnsupportedStream(FatalError):
    """Encrypted with a scheme we cannot handle, or otherwise undownloadable."""


def decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC with PKCS#7 padding - one HLS segment is one unit."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise UnsupportedStream(
            "this stream is AES-128 encrypted; install the 'cryptography' "
            "package to download it"
        ) from exc
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    if not plain:
        return plain
    pad = plain[-1]
    # Only strip a run that is actually valid padding: a segment whose last
    # byte happens to look like padding must not lose real bytes.
    if 1 <= pad <= 16 and len(plain) >= pad and plain[-pad:] == bytes([pad]) * pad:
        return plain[:-pad]
    return plain


@dataclass(slots=True)
class HlsStatus:
    total_segments: int = 0
    completed_segments: int = 0
    downloaded: int = 0

    @property
    def estimated_size(self) -> int | None:
        """Extrapolate the total from the segments already on disk.

        A playlist never states its byte size, so this is the only way to draw
        a progress bar. It converges quickly because HLS segments are cut to a
        fixed duration.
        """
        if not self.completed_segments or not self.total_segments:
            return None
        average = self.downloaded / self.completed_segments
        return int(average * self.total_segments)


class HlsDownloader:
    """Fetches one media playlist into one file."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        output: Path,
        work_dir: Path,
        connections: int = 8,
        bucket: TokenBucket | None = None,
        stop_event: asyncio.Event | None = None,
        on_bytes: Callable[[int], None] | None = None,
        max_height: int | None = None,
        max_retries: int = 5,
        ffmpeg_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.client = client
        self.url = url
        self.output = output
        self.work_dir = work_dir
        self.connections = max(1, min(32, connections))
        self.bucket = bucket or Unlimited()
        self.stop_event = stop_event or asyncio.Event()
        self.on_bytes = on_bytes
        self.max_height = max_height
        self.max_retries = max_retries
        self.ffmpeg_path = ffmpeg_path

        self.status = HlsStatus()
        self.playlist: MediaPlaylist | None = None
        self.master: MasterPlaylist | None = None
        self._keys: dict[str, bytes] = {}
        self._key_lock = asyncio.Lock()

    # ------------------------------------------------------------- playlists

    async def fetch_playlist(self, url: str) -> Playlist:
        text, final_url = await self._get_text(url)
        # Relative segment URIs resolve against the URL we ended up at, not
        # the one we asked for - CDNs redirect playlists constantly.
        return parse_playlist(text, base_url=final_url)

    async def _get_text(self, url: str) -> tuple[str, str]:
        attempt = 0
        while True:
            self._check_stop()
            try:
                response = await self.client.get(url)
                err = classify_status(response.status_code)
                if err is not None:
                    raise err
                body = response.content[:MAX_PLAYLIST_BYTES]
                return body.decode("utf-8", "replace"), str(response.url)
            except (TransientError, httpx.HTTPError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise TransientError(f"cannot read playlist: {exc}") from exc
                await self._sleep_or_stop(backoff_delay(attempt))

    async def resolve(self) -> MediaPlaylist:
        """Fetch `self.url`, descending into the chosen variant if needed."""
        playlist = await self.fetch_playlist(self.url)
        if isinstance(playlist, MasterPlaylist):
            self.master = playlist
            variant = pick_variant(playlist, self.max_height)
            if variant is None:
                raise UnsupportedStream("master playlist has no variants")
            log.info("hls: variant %s (%d bps)", variant.label, variant.bandwidth)
            playlist = await self.fetch_playlist(variant.url)
            if isinstance(playlist, MasterPlaylist):
                raise UnsupportedStream("nested master playlists are not supported")
        bad = playlist.unsupported_key
        if bad is not None:
            raise UnsupportedStream(
                f"unsupported encryption: {bad.method} ({bad.key_format})"
            )
        if not playlist.segments:
            raise UnsupportedStream("playlist contains no segments")
        if playlist.is_live:
            log.warning(
                "hls: playlist has no #EXT-X-ENDLIST; downloading the current "
                "window of %d segments", len(playlist.segments),
            )
        self.playlist = playlist
        self.status.total_segments = len(playlist.segments)
        return playlist

    # -------------------------------------------------------------- download

    async def run(self) -> Path:
        playlist = self.playlist or await self.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._scan_existing(playlist)

        queue: asyncio.Queue[MediaSegment] = asyncio.Queue()
        for segment in playlist.segments:
            if not self._part_path(segment).exists():
                queue.put_nowait(segment)

        if not queue.empty():
            workers = [
                asyncio.create_task(self._worker(queue), name=f"hls-{i}")
                for i in range(min(self.connections, queue.qsize()))
            ]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                self.stop_event.set()
                await asyncio.gather(*workers, return_exceptions=True)
                raise

        self._check_stop()
        return await self.assemble(playlist)

    def _scan_existing(self, playlist: MediaPlaylist) -> None:
        """Count what a previous run already finished."""
        done = 0
        total = 0
        for segment in playlist.segments:
            path = self._part_path(segment)
            if path.exists():
                done += 1
                total += path.stat().st_size
        self.status.completed_segments = done
        self.status.downloaded = total
        if done:
            log.info("hls: resuming with %d/%d segments on disk", done, len(playlist.segments))

    def _part_path(self, segment: MediaSegment) -> Path:
        return self.work_dir / f"{segment.index:06d}{DONE_SUFFIX}"

    async def _worker(self, queue: asyncio.Queue[MediaSegment]) -> None:
        while True:
            try:
                segment = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._fetch_segment(segment)
            finally:
                queue.task_done()

    async def _fetch_segment(self, segment: MediaSegment) -> None:
        attempt = 0
        while True:
            self._check_stop()
            try:
                data = await self._read_segment(segment)
            except (CancelledByUser, FatalError):
                raise
            except (TransientError, httpx.HTTPError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise TransientError(
                        f"segment {segment.index} failed after {self.max_retries} "
                        f"retries: {exc}"
                    ) from exc
                delay = backoff_delay(attempt)
                log.warning("hls segment %d retry %d (%s)", segment.index, attempt, exc)
                await self._sleep_or_stop(delay)
                continue

            if segment.key is not None:
                key = await self._key_for(segment)
                data = decrypt_aes128(data, key, segment.iv)

            final = self._part_path(segment)
            tmp = final.with_suffix(PART_SUFFIX)
            tmp.write_bytes(data)
            os.replace(tmp, final)
            # Count only what is on disk: a retried segment must not inflate
            # the progress bar with bytes that were thrown away.
            self.status.completed_segments += 1
            self.status.downloaded += len(data)
            if self.on_bytes is not None:
                self.on_bytes(len(data))
            return

    async def _read_segment(self, segment: MediaSegment) -> bytes:
        headers = {}
        if segment.byterange is not None:
            offset, length = segment.byterange
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        request = self.client.build_request("GET", segment.url, headers=headers)
        response = await self.client.send(request, stream=True)
        try:
            err = classify_status(response.status_code)
            if err is not None:
                raise err
            buffer = bytearray()
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                if self.stop_event.is_set():
                    raise CancelledByUser("stopped")
                await self.bucket.acquire(len(chunk))
                buffer.extend(chunk)
            return bytes(buffer)
        finally:
            await response.aclose()

    async def _key_for(self, segment: MediaSegment) -> bytes:
        assert segment.key is not None and segment.key.uri
        uri = segment.key.uri
        async with self._key_lock:
            cached = self._keys.get(uri)
            if cached is not None:
                return cached
            response = await self.client.get(uri)
            err = classify_status(response.status_code)
            if err is not None:
                raise err
            key = response.content
            if len(key) != 16:
                raise UnsupportedStream(f"AES key must be 16 bytes, got {len(key)}")
            self._keys[uri] = key
            return key

    # -------------------------------------------------------------- assembly

    async def assemble(self, playlist: MediaPlaylist) -> Path:
        """Concatenate the segments, then remux into the wanted container."""
        parts = [self._part_path(s) for s in playlist.segments]
        missing = [p for p in parts if not p.exists()]
        if missing:
            raise TransientError(f"{len(missing)} segments are still missing")

        raw = self.work_dir / "stream.raw"
        # Off the event loop: joining a feature-length stream moves gigabytes,
        # and every other download shares this thread.
        await asyncio.to_thread(_join, parts, raw)

        self.output.parent.mkdir(parents=True, exist_ok=True)
        binary = ffmpeg_mod.find_ffmpeg(self.ffmpeg_path)
        if binary is None:
            # No ffmpeg: the raw stream is still playable, just not an .mp4.
            fallback = self.output.with_suffix(_raw_suffix(playlist))
            os.replace(raw, fallback)
            log.warning("ffmpeg not found; saved the raw stream as %s", fallback.name)
            self.output = fallback
        else:
            try:
                await ffmpeg_mod.remux(raw, self.output, ffmpeg=binary)
            except ffmpeg_mod.FfmpegError:
                fallback = self.output.with_suffix(_raw_suffix(playlist))
                os.replace(raw, fallback)
                log.warning("remux failed; kept the raw stream as %s", fallback.name)
                self.output = fallback
            else:
                raw.unlink(missing_ok=True)
        return self.output

    def cleanup(self) -> None:
        """Drop the segment scratch directory (only after a success)."""
        with contextlib.suppress(OSError):
            shutil.rmtree(self.work_dir)

    # --------------------------------------------------------------- helpers

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise CancelledByUser("stopped")

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            return
        raise CancelledByUser("stopped")


def _raw_suffix(playlist: MediaPlaylist) -> str:
    """`.ts` for MPEG-TS, `.mp4` for fragmented MP4 (an `#EXT-X-MAP` header)."""
    first = playlist.segments[0] if playlist.segments else None
    if first is not None and first.is_map:
        return ".mp4"
    return ".ts"


__all__ = [
    "HlsDownloader",
    "HlsStatus",
    "UnsupportedStream",
    "decrypt_aes128",
]


def _join(parts: list[Path], destination: Path) -> None:
    """Append every part into one file. Runs on a worker thread."""
    with open(destination, "wb") as out:
        for part in parts:
            with open(part, "rb") as fh:
                shutil.copyfileobj(fh, out, CHUNK_SIZE)
