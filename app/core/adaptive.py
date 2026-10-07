"""Give the network back when something else needs it.

A download that fills the line also fills the router's queue, and every
other packet waits behind it: a video call stutters, a game lags, pages
crawl. The queue shows up as round-trip time, so - like LEDBAT, the
algorithm behind Windows Update and BitTorrent's uTP - the throttle watches
the round trip to a fixed host:

* the lowest round trip seen recently is the line with nothing queued;
* when the round trip climbs well above that (`target`), the downloads are
  holding the queue, and the speed is cut to 70% of what is flowing now;
* while it stays close to the floor, the speed creeps back up, and once it is
  well past what the line gave unthrottled, the limit is lifted.

The arithmetic lives in `Throttle`, which is given numbers and returns a
limit, so it is tested without a network; `measure_rtt` is the one place
that touches the network.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import deque

#: how far above the quiet round trip counts as "the line is busy"
DEFAULT_TARGET = 0.075
#: never throttle below this: a download that crawls helps nobody
FLOOR = 64 * 1024
#: the quiet round trip is the lowest seen within this window
BASELINE_WINDOW = 300.0
#: a cut needs this long to show in the round trip before the next one
CUT_INTERVAL = 2.0
#: where the round trip is measured by default (TCP connect to Cloudflare)
DEFAULT_HOST = "1.1.1.1:443"


class Throttle:
    """Round trips and speeds in, a speed limit (or None) out."""

    def __init__(self, target: float = DEFAULT_TARGET, floor: float = FLOOR) -> None:
        self.target = target
        self.floor = floor
        self.limit: float | None = None
        #: the fastest the downloads ran without a limit; how far to climb back
        self.peak = 0.0
        self._samples: deque[tuple[float, float]] = deque()
        self._recent: deque[float] = deque(maxlen=3)
        self._last_cut = float("-inf")

    @property
    def baseline(self) -> float | None:
        return min((rtt for _, rtt in self._samples), default=None)

    def update(self, rtt: float | None, speed: float, now: float | None = None) -> float | None:
        """One measurement: the round trip (None if it failed) and the speed now."""
        now = time.monotonic() if now is None else now
        if rtt is not None:
            self._samples.append((now, rtt))
            self._recent.append(rtt)
        while self._samples and self._samples[0][0] < now - BASELINE_WINDOW:
            self._samples.popleft()

        if speed <= 0:
            # Nothing is downloading: nothing to hold back, and the next
            # download starts at full speed until the line says otherwise.
            self.limit = None
            return None
        if self.limit is None:
            self.peak = max(self.peak * 0.999, speed)
        if rtt is None or len(self._recent) < 2:
            return self.limit

        # The median of the last few rides over a single slow packet.
        delay = statistics.median(self._recent) - (self.baseline or 0.0)
        if delay > self.target:
            if now - self._last_cut >= CUT_INTERVAL:
                flowing = min(speed, self.limit) if self.limit else speed
                self.limit = max(self.floor, flowing * 0.7)
                self._last_cut = now
        elif delay < self.target / 2 and self.limit is not None:
            self.limit += max(self.floor, self.limit * 0.08)
            if self.peak and self.limit > self.peak * 1.2:
                self.limit = None
        return self.limit


def parse_host(text: str) -> tuple[str, int]:
    """"host:port", "[v6]:port" or a bare host (port 443)."""
    text = (text or DEFAULT_HOST).strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest.lstrip(":") or 443)
    host, sep, port = text.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host, int(port)
    return text, 443


async def measure_rtt(host: str, port: int, timeout: float = 2.0) -> float | None:
    """The time a TCP handshake takes - one round trip - or None if it fails."""
    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError, TimeoutError):
        return None
    elapsed = time.perf_counter() - started
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return elapsed
