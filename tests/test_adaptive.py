"""The throttle that gives the line back when other traffic needs it."""

from __future__ import annotations

import asyncio
import itertools
import time

import pytest

from app.core import adaptive
from app.core.adaptive import FLOOR, Throttle, parse_host
from app.core.engine import Engine
from app.core.task import DownloadRequest, TaskState

from .conftest import make_payload, sha256

MB = 1024 * 1024


def feed(throttle: Throttle, samples, speed: float, start: float = 0.0, step: float = 1.0):
    now = start
    for rtt in samples:
        throttle.update(rtt, speed, now)
        now += step
    return now


def test_a_quiet_line_is_left_alone():
    throttle = Throttle()
    feed(throttle, [0.020, 0.022, 0.021, 0.025, 0.019] * 4, speed=10 * MB)
    assert throttle.limit is None
    assert throttle.baseline == pytest.approx(0.019)


def test_a_full_queue_cuts_the_speed_to_seventy_percent():
    throttle = Throttle(target=0.075)
    now = feed(throttle, [0.020] * 5, speed=10 * MB)
    feed(throttle, [0.200, 0.210, 0.220], speed=10 * MB, start=now)
    assert throttle.limit == pytest.approx(7 * MB)


def test_cuts_wait_for_the_last_one_to_show():
    throttle = Throttle()
    now = feed(throttle, [0.020] * 5, speed=10 * MB)
    # Three busy samples half a second apart: one cut, not three.
    feed(throttle, [0.300] * 4, speed=10 * MB, start=now, step=0.5)
    assert throttle.limit == pytest.approx(7 * MB)
    feed(throttle, [0.300] * 2, speed=7 * MB, start=now + 2.5, step=2.0)
    assert throttle.limit < 7 * MB


def test_it_never_goes_below_the_floor():
    throttle = Throttle()
    now = feed(throttle, [0.020] * 5, speed=200 * 1024)
    feed(throttle, [0.500] * 20, speed=100 * 1024, start=now, step=2.0)
    assert throttle.limit == FLOOR


def test_the_speed_comes_back_and_the_limit_goes_once_quiet():
    throttle = Throttle()
    now = feed(throttle, [0.020] * 5, speed=10 * MB)
    now = feed(throttle, [0.300] * 3, speed=10 * MB, start=now)
    # The median of three needs two quiet samples to see the queue drain.
    now = feed(throttle, [0.022] * 2, speed=throttle.limit, start=now)
    held = throttle.limit
    assert held is not None
    feed(throttle, [0.022] * 3, speed=held, start=now)
    assert throttle.limit > held
    feed(throttle, [0.021] * 60, speed=held, start=now + 3)
    assert throttle.limit is None


def test_idle_means_no_limit():
    throttle = Throttle()
    now = feed(throttle, [0.020] * 5, speed=10 * MB)
    feed(throttle, [0.300] * 3, speed=10 * MB, start=now)
    assert throttle.limit is not None
    assert throttle.update(0.300, 0.0, now + 10) is None


def test_a_lost_measurement_changes_nothing():
    throttle = Throttle()
    now = feed(throttle, [0.020] * 5, speed=10 * MB)
    feed(throttle, [0.300] * 3, speed=10 * MB, start=now)
    held = throttle.limit
    assert throttle.update(None, 10 * MB, now + 4) == held


def test_the_baseline_forgets_old_samples():
    throttle = Throttle()
    throttle.update(0.005, 1 * MB, 0.0)
    throttle.update(0.040, 1 * MB, adaptive.BASELINE_WINDOW + 10)
    assert throttle.baseline == pytest.approx(0.040)


@pytest.mark.parametrize("text, expected", [
    ("1.1.1.1:443", ("1.1.1.1", 443)),
    ("example.com", ("example.com", 443)),
    ("example.com:80", ("example.com", 80)),
    ("[2606:4700::1111]:53", ("2606:4700::1111", 53)),
    ("2606:4700::1111", ("2606:4700::1111", 443)),
    ("", ("1.1.1.1", 443)),
])
def test_hosts(text, expected):
    assert parse_host(text) == expected


def test_measuring_a_round_trip_against_a_real_listener():
    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            rtt = await adaptive.measure_rtt("127.0.0.1", port)
        server.close()
        return rtt, await adaptive.measure_rtt("127.0.0.1", port, timeout=0.5)

    rtt, closed = asyncio.run(go())
    assert rtt is not None and 0 <= rtt < 1
    assert closed is None


def test_the_engine_slows_a_download_when_the_line_fills(server, tmp_path):
    data = make_payload(10 * MB, seed=31)
    server.add_file("adaptive.bin", data)
    # Quiet, then a video call fills the line for a few seconds, then quiet.
    rtts = itertools.chain([0.020] * 3, [0.400] * 15, itertools.repeat(0.020))
    seen: list[float | None] = []

    async def fake(host, port):
        return next(rtts)

    engine = Engine(max_concurrent=1, speed_limit=2 * MB)
    engine._measure = fake
    engine._adapt_interval = 0.2
    engine.set_adaptive(True, target_ms=75)
    with engine:
        task_id = engine.submit(DownloadRequest(
            url=server.url_for("adaptive.bin"), save_dir=tmp_path, connections=2
        ))
        deadline = time.time() + 30
        while engine.state(task_id) is not TaskState.COMPLETED and time.time() < deadline:
            seen.append(engine.adaptive_limit)
            time.sleep(0.1)
        assert engine.wait_idle(timeout=60)
        assert engine.state(task_id) is TaskState.COMPLETED
        engine.set_adaptive(False)
        time.sleep(0.1)
        assert engine.adaptive_limit is None
    assert any(limit and limit < 2 * MB for limit in seen), seen
    assert sha256(tmp_path / "adaptive.bin") == sha256(data)


def test_switched_off_the_engine_never_measures(server, tmp_path):
    data = make_payload(300_000, seed=32)
    server.add_file("adaptive-off.bin", data)
    calls = []

    async def fake(host, port):
        calls.append(host)
        return 0.5

    engine = Engine()
    engine._measure = fake
    with engine:
        engine.submit(DownloadRequest(url=server.url_for("adaptive-off.bin"), save_dir=tmp_path))
        assert engine.wait_idle(timeout=30)
    assert calls == []
    assert engine.adaptive_limit is None
