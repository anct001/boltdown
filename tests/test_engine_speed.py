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
        self.holds_connection = False
        self.parked = None


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
         "--only", "latency", "slow_link", "flaky", "small_files", "conn_limit",
         "single_conn", "mirrors", "--json", str(out)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(out.read_text(encoding="utf-8"))


def test_every_benchmark_file_arrives_intact(bench_results):
    assert all(r["intact"] for r in bench_results.values()), bench_results


# Held a little below what the engine measures (0.92-0.98) so a busy CI
# machine does not fail them; the numbers before these changes were 0.58,
# 0.13 and 0.47.
# conn_limit measured 0.79-0.82 (0.49 before); single_conn 1.0 (it failed).
@pytest.mark.parametrize("name, floor", [
    ("latency", 0.8), ("slow_link", 0.7), ("flaky", 0.75),
    ("conn_limit", 0.6), ("single_conn", 0.8),
    # two servers at 8 MB/s each: 0.97 with the mirror, 0.48 without
    ("mirrors", 0.75),
])
def test_the_engine_keeps_close_to_ideal(bench_results, name, floor):
    assert bench_results[name]["efficiency"] >= floor, bench_results[name]


def test_one_connection_carries_on_from_segment_to_segment(bench_results):
    """With one connection allowed, the open stream runs on into the next
    segment instead of a new request per segment, each risking a refusal."""
    single = bench_results["single_conn"]
    assert single["requests"] <= single["segments"] + 3, single


def test_small_files_take_one_request_each(bench_results):
    assert bench_results["small_files"]["requests_per_file"] == 1.0, bench_results["small_files"]


# ---------------------------------------------------------------- start-up


async def test_warm_up_touches_no_network(monkeypatch):
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("warm-up must not send anything")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    from app.core.http_client import warm_up

    await warm_up()


def test_the_engine_warms_up_when_it_starts(monkeypatch):
    """The first link clicked after starting the application used to pay for
    loading httpx's transport and the CA bundle - a few hundred ms on
    Windows, measured on CI - before its first request went out."""
    import threading

    from app.core import http_client
    from app.core.engine import Engine

    warmed = threading.Event()

    async def fake_warm_up():
        warmed.set()

    monkeypatch.setattr(http_client, "warm_up", fake_warm_up)
    engine = Engine()
    engine.start()
    try:
        assert warmed.wait(5), "the engine never warmed up"
    finally:
        engine.stop()


# ------------------------------------------------- server connection limits


class _Holder(_FakeWorker):
    def __init__(self, segment, holds: bool) -> None:
        super().__init__(segment, rate=1_000_000)
        self.holds_connection = holds
        self.parked = None


def test_a_refusal_at_the_servers_peak_is_its_limit(tmp_path):
    runner = _runner(tmp_path)
    me = _Holder(None, holds=False)
    other = _Holder(None, holds=True)
    runner._workers = [me, other]
    runner._note_served()          # one connection served at once, so far
    assert runner._should_decline(me) is True


def test_a_refusal_below_the_peak_is_a_slot_not_yet_freed(tmp_path):
    """Two were served at once before; with one now, the 503 is the server
    still tearing the other down - retry, do not give the segment away."""
    runner = _runner(tmp_path)
    a, b, me = _Holder(None, True), _Holder(None, True), _Holder(None, False)
    runner._workers = [a, b, me]
    runner._note_served()
    b.holds_connection = False
    assert runner._should_decline(me) is False


def test_a_refusal_with_nothing_served_is_a_busy_server(tmp_path):
    runner = _runner(tmp_path)
    me = _Holder(None, holds=False)
    runner._workers = [me, _Holder(None, holds=False)]
    assert runner._should_decline(me) is False


async def test_a_segment_handed_back_is_adopted_whole_before_any_split(tmp_path):
    runner = _runner(tmp_path)
    served = Segment(index=0, start=0, end=10_000_000 - 1, done=1_000_000)
    handed_back = Segment(index=1, start=10_000_000, end=20_000_000 - 1, done=0)
    runner.segments = [served, handed_back]
    owner = _Holder(served, holds=True)
    me = _Holder(None, holds=False)
    runner._workers = [owner, me]
    runner._owned = {0: owner}

    adopted = await runner._steal_work(me)

    assert adopted is handed_back
    assert (adopted.start, adopted.end) == (10_000_000, 20_000_000 - 1)
    assert len(runner.segments) == 2, "nothing was split"
    assert runner._owned[1] is me


async def test_a_refused_worker_only_comes_back_for_orphans(tmp_path):
    runner = _runner(tmp_path)
    seg = Segment(index=0, start=0, end=40_000_000 - 1, done=0)
    runner.segments = [seg]
    owner = _Holder(seg, holds=True)
    me = _Holder(None, holds=False)
    runner._workers = [owner, me]
    runner._owned = {0: owner}
    assert await runner._steal_work(me, orphans_only=True) is None
    assert len(runner.segments) == 1


async def test_waiting_for_a_slot_ends_with_the_download(tmp_path):
    """A refused worker waiting out its turn must not hold a finished
    download open for the rest of its wait."""
    runner = _runner(tmp_path)
    runner.segments = [Segment(index=0, start=0, end=99, done=100)]
    started = time.monotonic()
    assert await runner._wait_for_turn(5.0) is False
    assert time.monotonic() - started < 0.5


def test_429_and_503_mean_busy_not_broken():
    from app.core.errors import ServerBusyError, TransientError, classify_status

    for status in (429, 503):
        err = classify_status(status)
        assert isinstance(err, ServerBusyError) and isinstance(err, TransientError)
    assert not isinstance(classify_status(502), ServerBusyError)


async def test_a_lowered_limit_hands_the_segment_back_instead_of_failing():
    """Two connections were served at once; now the server allows one. The
    refused worker retries (a slot may be about to free up), and when the
    retries run out with another connection still served it gives the
    segment back - it never fails the download."""
    from app.core.errors import ServerBusyError
    from app.core.segment import SegmentDeclined

    worker = SegmentWorker(
        client=None, url="http://example.invalid/x", fd=-1, bucket=None,
        stop_event=asyncio.Event(), on_commit=lambda n: None, resumable=True,
        max_retries=2, should_decline=lambda: False,
    )
    worker.holds_elsewhere = lambda: True

    async def refuse(*args, **kwargs):
        raise ServerBusyError("HTTP 503")

    worker._stream_once = refuse
    with pytest.raises(SegmentDeclined):
        await worker.run(Segment(index=3, start=0, end=999))
