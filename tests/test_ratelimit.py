from __future__ import annotations

import asyncio
import time

from app.core.ratelimit import ChainedBucket, TokenBucket


async def test_unlimited_bucket_never_waits():
    bucket = TokenBucket(None)
    start = time.monotonic()
    for _ in range(100):
        await bucket.acquire(1 << 20)
    assert time.monotonic() - start < 0.1


async def test_bucket_enforces_average_rate():
    rate = 200_000  # bytes/s
    bucket = TokenBucket(rate, capacity=50_000)
    start = time.monotonic()
    total = 0
    for _ in range(12):
        await bucket.acquire(25_000)
        total += 25_000
    elapsed = time.monotonic() - start
    # 300 KB at 200 KB/s takes ~1.5s minus the initial full bucket (~0.25s).
    assert elapsed >= 1.0
    assert total / elapsed <= rate * 1.4


async def test_acquire_larger_than_capacity_does_not_deadlock():
    bucket = TokenBucket(100_000, capacity=10_000)
    await asyncio.wait_for(bucket.acquire(1_000_000), timeout=2)


async def test_chained_bucket_applies_the_tighter_limit():
    chained = ChainedBucket(
        TokenBucket(400_000, capacity=25_000), TokenBucket(100_000, capacity=25_000)
    )
    assert chained.rate == 100_000
    start = time.monotonic()
    for _ in range(5):  # 125 KB: 25 KB burst, then 100 KB at 100 KB/s
        await chained.acquire(25_000)
    assert time.monotonic() - start >= 0.7


async def test_set_rate_switches_to_unlimited():
    bucket = TokenBucket(1000)
    bucket.set_rate(None)
    start = time.monotonic()
    await bucket.acquire(10_000_000)
    assert time.monotonic() - start < 0.1
