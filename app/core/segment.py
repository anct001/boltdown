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
#: after a connection dropped *mid-body*, wait only this long before asking
#: again - the server was answering a moment ago, so backing off for a second
#: (as for a server that refuses outright) only leaves the line idle
RECONNECT_DELAY = 0.05
#: weight of the newest sample in a connection's speed estimate
RATE_WEIGHT = 0.4


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
        #: bytes being written to disk right now, not yet counted in `done`
        self._inflight = 0
        #: bytes received on this worker's connections, ever
        self.received = 0
        #: bytes/s over the last moments; 0 until the first sample
        self.rate = 0.0
        #: seconds from sending the last request to its response headers
        self.ttfb: float | None = None
        self._sample: tuple[float, int] | None = None

    @property
    def active(self) -> Segment | None:
        return self._active

    @property
    def unwritten(self) -> int:
        """Bytes this worker holds past `segment.current` - not yet counted."""
        return len(self._buffer) + self._inflight

    def sample(self, now: float) -> None:
        """Update `rate`; called a few times a second by the task."""
        if self._sample is not None:
            then, seen = self._sample
            if now > then:
                instant = (self.received - seen) / (now - then)
                self.rate = instant if self.rate == 0 else (
                    (1 - RATE_WEIGHT) * self.rate + RATE_WEIGHT * instant
                )
        self._sample = (now, self.received)

    async def run(self, segment: Segment, primed: httpx.Response | None = None) -> None:
        """Download `segment` to completion or raise.

        `primed` is a response already streaming this segment from its first
        byte (the probe's own); the first attempt reads from it instead of
        asking again.
        """
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
                eof = await self._stream_once(loop, segment, primed)
            except (CancelledByUser, RangeNotSatisfiableError, FatalError):
                raise
            except (TransientError, httpx.HTTPError) as exc:
                progressed = segment.done > committed_before
                if progressed:
                    attempt = 0  # we made progress; the connection just died
                attempt += 1
                if attempt > self.max_retries:
                    raise TransientError(
                        f"segment {segment.index} failed after "
                        f"{self.max_retries} retries: {exc}"
                    ) from exc
                delay = RECONNECT_DELAY if progressed else backoff_delay(attempt)
                log.warning(
                    "segment %d retry %d/%d in %.1fs (%s)",
                    segment.index, attempt, self.max_retries, delay, exc,
                )
                await self._sleep_or_stop(delay)
                continue
            finally:
                primed = None  # used, or failed: either way never again

            if segment.end is None and eof:
                # Size was unknown; EOF defines the end of the resource.
                segment.end = segment.start + segment.done - 1
                return
            if segment.is_complete:
                return

            # Body ended before we received everything we asked for.
            progressed = segment.done > committed_before
            if progressed:
                attempt = 0
            attempt += 1
            if attempt > self.max_retries:
                raise TransientError(
                    f"segment {segment.index} truncated after "
                    f"{self.max_retries} retries"
                )
            log.warning("segment %d ended early, retrying", segment.index)
            await self._sleep_or_stop(
                RECONNECT_DELAY if progressed else backoff_delay(attempt)
            )

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
        self,
        loop: asyncio.AbstractEventLoop,
        segment: Segment,
        primed: httpx.Response | None = None,
    ) -> bool:
        """Fetch from the current offset. Returns True if the body hit EOF."""
        # Any bytes left over from a previous attempt belong at their original
        # offset - commit them before the new request moves `current`.
        self.flush_sync()
        if primed is not None and segment.current == 0:
            headers: dict[str, str] = {}
            response = primed
        else:
            if primed is not None:
                await primed.aclose()
            headers = self._range_header(segment)
            request = self.client.build_request("GET", self.url, headers=headers)
            sent_at = loop.time()
            response = await self.client.send(request, stream=True)
            self.ttfb = loop.time() - sent_at
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
            # Chunks as they arrive: asking httpx for a fixed size makes it
            # copy every byte through a BytesIO to re-cut them, which at a
            # few hundred MB/s is a real share of the CPU. The buffer below
            # batches them for the disk anyway.
            async for chunk in response.aiter_bytes():
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
        self.received += len(chunk)

    @staticmethod
    def _countable(segment: Segment, offset: int, length: int) -> int:
        """How much of a write at `offset` still belongs to `segment`.

        Another worker may have taken the tail of this segment while these
        bytes were on their way to disk. They are the same bytes it will
        write, so writing them twice is harmless - but counting them twice
        would make the download look longer than the file.
        """
        if segment.end is None:
            return length
        return max(0, min(length, segment.end - offset + 1))

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
        # Hand the buffer itself to the writer and start a new one, rather
        # than copying a megabyte on every flush.
        data, self._buffer = self._buffer, bytearray()
        offset = self._buffer_offset
        self._inflight = len(data)
        try:
            await loop.run_in_executor(None, write_at, self.fd, data, offset)
        finally:
            self._inflight = 0
        counted = self._countable(segment, offset, len(data))
        segment.done += counted
        self._last_flush = loop.time()
        self.on_commit(counted)

    def flush_sync(self) -> None:
        """Last-resort flush used when the event loop is going away."""
        if not self._buffer or self._active is None:
            return
        data, self._buffer = self._buffer, bytearray()
        offset = self._buffer_offset
        write_at(self.fd, data, offset)
        counted = self._countable(self._active, offset, len(data))
        self._active.done += counted
        self.on_commit(counted)
