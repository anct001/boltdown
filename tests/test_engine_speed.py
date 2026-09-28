"""What makes the engine fast where IDM is: one round trip to the first byte,
the idle connection going after the slowest tail, and reconnecting at once.

The unit tests pin the mechanisms; the last group runs scripts/bench_engine.py
against its throttling server and holds the results to the ratios IDM reaches.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.http_client import RequestSpec, build_client, shared_ssl_context
from app.core.probe import probe
from app.core.segment import Segment, SegmentWorker
from app.core.task import MIN_SPLIT, DownloadRequest, TaskRunner, TaskState

from .conftest import make_payload, sha256

ROOT = Path(__file__).resolve().parent.parent


class _FakeWorker:
    """Just what `_steal_work` reads off a worker."""

    def __init__(self, segment: Segment | None, rate: float, unwritten: int = 0,
                 ttfb: float | None = 0.05) -> None:
        self.active = segment
        self.rate = rate
        self.unwritten = unwritten
        self.ttfb = ttfb


def _runner(tmp_path: Path) -> TaskRunner:
    return TaskRunner(1, DownloadRequest(url="http://example.invalid/x", save_dir=tmp_path))


# ------------------------------------------------------------- first byte


async def test_the_probe_is_the_first_segment(server, tmp_path):
    """One request for a small file: the probe's own reply is the download.
    It used to take three - a HEAD, a one-byte probe, then the real GET."""
    data = make_payload(300_000, seed=41)
    url = server.add_file("one-trip.bin", data)
    before = server.state.requests["one-trip.bin"]

    runner = TaskRunner(1, DownloadRequest(url=url, save_dir=tmp_path, connections=8))
    assert await runner.run() is TaskState.COMPLETED

    assert sha256(runner.dest_path) == sha256(data)
    assert server.state.requests["one-trip.bin"] - before == 1


async def test_a_big_file_reuses_the_probe_for_segment_zero(server, tmp_path):
    data = make_payload(8 * 1024 * 1024 + 5, seed=42)
    url = server.add_file("reuse.bin", data)
    before = server.state.requests["reuse.bin"]

    runner = TaskRunner(1, DownloadRequest(url=url, save_dir=tmp_path, connections=4))
    assert await runner.run() is TaskState.COMPLETED

    assert sha256(runner.dest_path) == sha256(data)
    # one request per segment (splits included): none spent on probing alone
    assert len(runner.segments) >= 4
    assert server.state.requests["reuse.bin"] - before == len(runner.segments)


async def test_a_server_without_ranges_is_read_from_the_probe(server, tmp_path):
    data = make_payload(700_000, seed=43)
    server.add_file("norange-trip.bin", data)
    url = server.url_for("norange-trip.bin", norange=1)
    before = server.state.requests["norange-trip.bin"]

    runner = TaskRunner(1, DownloadRequest(url=url, save_dir=tmp_path))
    assert await runner.run() is TaskState.COMPLETED
    assert sha256(runner.dest_path) == sha256(data)
    assert server.state.requests["norange-trip.bin"] - before == 1


async def test_the_probe_can_leave_the_body_open(server, payload):
    url = server.add_file("probe-open.bin", payload)
    spec = RequestSpec(url=url)
    async with build_client(spec) as client:
        result = await probe(client, spec, keep_body=True)
        try:
            assert result.resumable and result.size == len(payload)
            first = b""
            async for chunk in result.response.aiter_bytes():
                first += chunk
                if len(first) >= 1000:
                    break
            assert first[:1000] == payload[:1000]
        finally:
            await result.response.aclose()
        closed = await probe(client, spec)
        assert closed.response is None


def test_one_tls_context_serves_every_download():
    """Loading the CA bundle took ~50 ms per download, before any request."""
    assert shared_ssl_context() is shared_ssl_context()


# ---------------------------------------------------- dynamic segmentation


async def test_the_crawling_connection_loses_its_tail(tmp_path):
    """The segment that will finish last is taken on, even when another
    segment has more bytes left, and the crawler keeps only its share."""
    runner = _runner(tmp_path)
    big_fast = Segment(index=0, start=0, end=40_000_000 - 1, done=10_000_000)   # 30 MB at 4 MB/s
    small_slow = Segment(index=1, start=40_000_000, end=48_000_000 - 1, done=0)  # 8 MB at 64 KB/s
    runner.segments = [big_fast, small_slow]
    me = _FakeWorker(None, rate=4_000_000)
    runner._workers = [
        _FakeWorker(big_fast, rate=4_000_000),
        _FakeWorker(small_slow, rate=64_000, unwritten=20_000),
        me,
    ]

    stolen = await runner._steal_work(me)

    assert stolen is not None and stolen.start > small_slow.start, "went after the fast one"
    assert stolen.end == 48_000_000 - 1
    kept = small_slow.end - small_slow.current + 1
    # 64 KB/s against 4 MB/s: the crawler keeps a few percent, never less
    # than what it already holds
    assert 20_000 <= kept < 400_000
    assert small_slow.end == stolen.start - 1
    assert kept + (stolen.end - stolen.start + 1) == 8_000_000


async def test_even_connections_split_the_remainder_evenly(tmp_path):
    runner = _runner(tmp_path)
    seg = Segment(index=0, start=0, end=20_000_000 - 1, done=0)
    runner.segments = [seg]
    me = _FakeWorker(None, rate=2_000_000, ttfb=0.0)
    runner._workers = [_FakeWorker(seg, rate=2_000_000), me]

    stolen = await runner._steal_work(me)

    assert stolen is not None
    assert abs(stolen.start - 10_000_000) < 50_000


async def test_a_split_not_worth_a_new_request_is_skipped(tmp_path):
    """Half a second of work left on a fast link: a new request would cost
    about as much as it saves."""
    runner = _runner(tmp_path)
    seg = Segment(index=0, start=0, end=2_000_000 - 1, done=0)
    runner.segments = [seg]
    me = _FakeWorker(None, rate=4_000_000, ttfb=0.3)
    runner._workers = [_FakeWorker(seg, rate=4_000_000), me]
    assert await runner._steal_work(me) is None


async def test_a_worker_never_counts_bytes_past_its_new_end():
    """A split can land while bytes are on their way to disk. They are the
    same bytes the thief will write, but counting them twice made the
    download look bigger than the file."""
    segment = Segment(index=0, start=1000, end=1999, done=0)
    assert SegmentWorker._countable(segment, 1000, 600) == 600
    segment.end = 1299  # the tail was taken meanwhile
    assert SegmentWorker._countable(segment, 1000, 600) == 300
    assert SegmentWorker._countable(segment, 1400, 600) == 0
    assert SegmentWorker._countable(Segment(index=1, start=0, end=None), 0, 600) == 600


async def test_min_split_leaves_room_to_share_a_tail():
    assert MIN_SPLIT <= 512 * 1024


# ---------------------------------------------------------------- recovery


async def test_a_dropped_connection_is_resumed_at_once(server, tmp_path):
    """The server was sending a moment ago; a second of back-off per cut (as
    for a server refusing outright) made a flaky line half as fast."""
    data = make_payload(2_000_000, seed=44)
    server.add_file("cut.bin", data)
    url = server.url_for("cut.bin", drop=300_000, dropcount=3)

    started = time.monotonic()
    runner = TaskRunner(1, DownloadRequest(url=url, save_dir=tmp_path, connections=1))
    assert await runner.run() is TaskState.COMPLETED
    elapsed = time.monotonic() - started

    assert sha256(runner.dest_path) == sha256(data)
    assert elapsed < 1.5, f"three reconnects took {elapsed:.1f}s"


# ------------------------------------------------------------ the benchmark


def _bench():
    spec = importlib.util.spec_from_file_location("bench_engine", ROOT / "scripts" / "bench_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_bench_file_is_what_the_server_says():
    bench = _bench()
    assert bench.payload(0, 10) + bench.payload(10, 5) == bench.payload(0, 15)
    assert len(bench.payload(bench.BLOCK - 3, 10)) == 10


@pytest.fixture(scope="module")
def bench_results(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("bench") / "engine.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bench_engine.py"),
         "--only", "latency", "slow_link", "flaky", "small_files", "--json", str(out)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(out.read_text(encoding="utf-8"))


def test_every_benchmark_file_arrives_intact(bench_results):
    assert all(r["intact"] for r in bench_results.values()), bench_results


# Held a little below what the engine measures (0.92-0.98) so a busy CI
# machine does not fail them; the numbers before these changes were 0.58,
# 0.13 and 0.47.
@pytest.mark.parametrize("name, floor", [("latency", 0.8), ("slow_link", 0.7), ("flaky", 0.75)])
def test_the_engine_keeps_close_to_ideal(bench_results, name, floor):
    assert bench_results[name]["efficiency"] >= floor, bench_results[name]


def test_small_files_take_one_request_each(bench_results):
    assert bench_results["small_files"]["requests_per_file"] == 1.0, bench_results["small_files"]
