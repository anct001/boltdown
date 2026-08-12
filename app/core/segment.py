"""One byte range of a download, and the worker that fetches it."""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from typing import Callable

import httpx

from ..util.log import get_logger
from .errors import (
    CancelledByUser,
    FatalError,
    RangeNotSatisfiableError,
    TransientError,
    classify_status,
)
from .ratelimit import TokenBucket
from .writer import write_at

log = get_logger(__name__)

CHUNK_SIZE = 64 * 1024
FLUSH_BYTES = 1 << 20  # commit to disk every 1 MiB ...
FLUSH_INTERVAL = 0.5  # ... or every 500 ms, whichever comes first
MAX_RETRIES = 5


@dataclass(slots=True)
class Segment:
    """The byte range `start..end` inclusive.

    `end is None` means "until EOF" and is only used when the server did not
    tell us the size (single segment, no resume possible).
    """

    index: int
    start: int
    end: int | None = None
    done: int = 0

    @property
    def current(self) -> int:
        """Absolute offset of the next byte to write."""
        return self.start + self.done

    @property
    def total(self) -> int | None:
        return None if self.end is None else self.end - self.start + 1

    @property
    def remaining(self) -> int | None:
        return None if self.end is None else self.end - self.current + 1

    @property
    def is_complete(self) -> bool:
        rem = self.remaining
        return rem is not None and rem <= 0

    def to_dict(self) -> dict:
        return {"i": self.index, "start": self.start, "end": self.end, "done": self.done}

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        return cls(
            index=int(data["i"]),
            start=int(data["start"]),
            end=None if data.get("end") is None else int(data["end"]),
            done=int(data.get("done", 0)),
        )


def plan_segments(
    size: int | None, connections: int, min_chunk: int = 1 << 20
) -> list[Segment]:
    """Split `size` bytes across at most `connections` segments."""
    if size is None:
        return [Segment(index=0, start=0, end=None)]
    if size <= 0:
        return [Segment(index=0, start=0, end=-1)]
    count = max(1, min(connections, size // min_chunk or 1))
    base = size // count
    segments: list[Segment] = []
    offset = 0
    for i in range(count):
        length = base if i < count - 1 else size - offset
        segments.append(Segment(index=i, start=offset, end=offset + length - 1))
        offset += length
    return segments


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 8.0) -> float:
    """Exponential backoff with +/-25% jitter."""
    delay = min(cap, base * (2 ** (attempt - 1)))
    return delay * random.uniform(0.75, 1.25)


class SegmentWorker:
    """Streams segments to disk, retrying transient failures.

    A worker outlives a single segment: when its range finishes, the task
    runner hands it a slice stolen from a slower segment (dynamic
    segmentation), so the same worker object is reused.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        fd: int,
        bucket: TokenBucket,
        stop_event: asyncio.Event,
        on_commit: Callable[[int], None],
        resumable: bool,
        max_retries: int = MAX_RETRIES,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self.client = client
        self.url = url
        self.fd = fd
        self.bucket = bucket
        self.stop_event = stop_event
        self.on_commit = on_commit
        self.resumable = resumable
        self.max_retries = max_retries
        self.chunk_size = chunk_size
        self._buffer = bytearray()
        self._buffer_offset = 0
        self._last_flush = 0.0
        self._active: Segment | None = None

    async def run(self, segment: Segment) -> None:
        """Download `segment` to completion or raise."""
        loop = asyncio.get_running_loop()
        if self._buffer:  # leftovers belong to the segment we just finished
            with contextlib.suppress(OSError):
                self.flush_sync()
        self._active = segment
        attempt = 0
        while True:
            if self.stop_event.is_set():
                raise CancelledByUser("stopped")
            if segment.is_complete:
                return

            committed_before = segment.done
            try:
                eof = await self._stream_once(loop, segment)
            except (CancelledByUser, RangeNotSatisfiableError, FatalError):
                raise
            except (TransientError, httpx.HTTPError) as exc:
                if segment.done > committed_before:
                    attempt = 0  # we made progress; the connection just died
                attempt += 1
                if attempt > self.max_retries:
                    raise TransientError(
                        f"segment {segment.index} failed after "
                        f"{self.max_retries} retries: {exc}"
                    ) from exc
                delay = backoff_delay(attempt)
                log.warning(
                    "segment %d retry %d/%d in %.1fs (%s)",
                    segment.index, attempt, self.max_retries, delay, exc,
                )
                await self._sleep_or_stop(delay)
                continue

            if segment.end is None and eof:
                # Size was unknown; EOF defines the end of the resource.
                segment.end = segment.start + segment.done - 1
                return
            if segment.is_complete:
                return

            # Body ended before we received everything we asked for.
            if segment.done > committed_before:
                attempt = 0
            attempt += 1
            if attempt > self.max_retries:
                raise TransientError(
                    f"segment {segment.index} truncated after "
                    f"{self.max_retries} retries"
                )
            log.warning("segment %d ended early, retrying", segment.index)
            await self._sleep_or_stop(backoff_delay(attempt))

    async def _sleep_or_stop(self, delay: float) -> None:
        """Sleep, but wake immediately (and abort) if we are asked to stop."""
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
        except (asyncio.TimeoutError, TimeoutError):
            return
        raise CancelledByUser("stopped")

    def _range_header(self, segment: Segment) -> dict[str, str]:
        if not self.resumable and segment.start == 0 and segment.done == 0:
            return {}
        if segment.end is None:
            return {"Range": f"bytes={segment.current}-"}
        return {"Range": f"bytes={segment.current}-{segment.end}"}

    def _uncommitted_remaining(self, segment: Segment) -> int | None:
        """Bytes still needed, counting what is sitting in the buffer."""
        rem = segment.remaining
        return None if rem is None else rem - len(self._buffer)

    async def _stream_once(
        self, loop: asyncio.AbstractEventLoop, segment: Segment
    ) -> bool:
        """Fetch from the current offset. Returns True if the body hit EOF."""
        # Any bytes left over from a previous attempt belong at their original
        # offset - commit them before the new request moves `current`.
        self.flush_sync()
        headers = self._range_header(segment)
        request = self.client.build_request("GET", self.url, headers=headers)
        response = await self.client.send(request, stream=True)
        try:
            err = classify_status(response.status_code)
            if err is not None:
                raise err
            if headers and response.status_code == 200 and segment.current > 0:
                raise FatalError(
                    "server ignored the Range header; cannot resume this segment"
                )

            self._last_flush = loop.time()
            eof = True
            async for chunk in response.aiter_bytes(self.chunk_size):
                if self.stop_event.is_set():
                    eof = False
                    break
                if not chunk:
                    continue
                remaining = self._uncommitted_remaining(segment)
                if remaining is not None:
                    if remaining <= 0:
                        eof = False
                        break
                    if len(chunk) > remaining:
                        # A concurrent split shrank this segment mid-flight,
                        # or the server sent more than we asked for.
                        chunk = chunk[:remaining]
                await self.bucket.acquire(len(chunk))
                self._append(segment, chunk)
                if self._should_flush(loop):
                    await self._flush(loop, segment)
                remaining = self._uncommitted_remaining(segment)
                if remaining is not None and remaining <= 0:
                    eof = False
                    break
            await self._flush(loop, segment)
            return eof
        finally:
            # If the stream died mid-body the buffer still holds valid bytes;
            # committing them here is what makes a retry resume rather than
            # replay (and stops stale bytes leaking into the next attempt).
            with contextlib.suppress(OSError):
                self.flush_sync()
            await response.aclose()

    def _append(self, segment: Segment, chunk: bytes) -> None:
        if not self._buffer:
            self._buffer_offset = segment.current
        self._buffer.extend(chunk)

    def _should_flush(self, loop: asyncio.AbstractEventLoop) -> bool:
        if len(self._buffer) >= FLUSH_BYTES:
            return True
        return bool(self._buffer) and (loop.time() - self._last_flush) >= FLUSH_INTERVAL

    async def _flush(self, loop: asyncio.AbstractEventLoop, segment: Segment) -> None:
        """Commit the buffer to disk, then advance `done`.

        Order matters: `done` (and therefore the resume metadata) must never
        claim more than what is actually on disk.
        """
        if not self._buffer:
            return
        data = bytes(self._buffer)
        offset = self._buffer_offset
        self._buffer.clear()
        await loop.run_in_executor(None, write_at, self.fd, data, offset)
        segment.done += len(data)
        self._last_flush = loop.time()
        self.on_commit(len(data))

    def flush_sync(self) -> None:
        """Last-resort flush used when the event loop is going away."""
        if not self._buffer or self._active is None:
            return
        data = bytes(self._buffer)
        offset = self._buffer_offset
        self._buffer.clear()
        write_at(self.fd, data, offset)
        self._active.done += len(data)
        self.on_commit(len(data))
