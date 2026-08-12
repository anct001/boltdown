"""Token bucket rate limiting.

Used both globally (all downloads) and per task. `acquire` serialises on a
lock so waiters are served FIFO, which keeps one segment from starving while
others hammer the bucket.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    #: Smallest bucket we will ever build - a few chunks, so a slow limit
    #: still hands out enough tokens for one read instead of stalling.
    MIN_CAPACITY = 256 * 1024

    def __init__(self, rate: float | None, capacity: float | None = None) -> None:
        """`rate` is bytes/second; None means unlimited.

        Capacity defaults to one second of traffic, which is the usual token
        bucket compromise: short bursts pass through, the average holds.
        """
        self._rate = rate
        self._capacity = (
            capacity if capacity is not None else max(rate or 0, self.MIN_CAPACITY)
        )
        self._tokens = float(self._capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float | None:
        return self._rate

    def set_rate(self, rate: float | None) -> None:
        self._rate = rate
        if rate is not None:
            self._capacity = max(rate, self.MIN_CAPACITY)
            self._tokens = min(self._tokens, self._capacity)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if self._rate:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    async def acquire(self, amount: int) -> None:
        if not self._rate or amount <= 0:
            return
        # A request larger than the bucket could never be satisfied.
        amount = min(amount, int(self._capacity))
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                deficit = amount - self._tokens
                await asyncio.sleep(deficit / self._rate)


class ChainedBucket:
    """Applies several buckets in order (global limit, then per-task limit)."""

    def __init__(self, *buckets: TokenBucket | None) -> None:
        self._buckets = [b for b in buckets if b is not None]

    @property
    def rate(self) -> float | None:
        rates = [b.rate for b in self._buckets if b.rate]
        return min(rates) if rates else None

    async def acquire(self, amount: int) -> None:
        for bucket in self._buckets:
            await bucket.acquire(amount)


class Unlimited(TokenBucket):
    """Convenience no-op bucket."""

    def __init__(self) -> None:
        super().__init__(None)

    async def acquire(self, amount: int) -> None:  # pragma: no cover - trivial
        return
