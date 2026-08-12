from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.resume import ResumeMeta
from app.core.segment import Segment
from app.core.task import MIN_SPLIT, DownloadRequest, TaskRunner, TaskState

from .conftest import make_payload, sha256


def _request(url: str, save_dir: Path, **kw) -> DownloadRequest:
    options = dict(url=url, save_dir=save_dir, connections=8)
    options.update(kw)
    return DownloadRequest(**options)


class _Recorder:
    """Captures engine events so tests can assert on start-of-download state."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []

    def __call__(self, event, snapshot) -> None:
        self.events.append((event, snapshot.downloaded))

    def downloaded_at(self, event: str) -> int | None:
        for name, value in self.events:
            if name == event:
                return value
        return None


async def _run(url: str, tmp_path: Path, **kw) -> TaskRunner:
    recorder = kw.pop("recorder", None)
    runner = TaskRunner(1, _request(url, tmp_path, **kw), on_event=recorder)
    await runner.run()
    return runner


# --------------------------------------------------------------- happy paths


async def test_multi_segment_download_is_byte_exact(server, tmp_path):
    data = make_payload(6 * 1024 * 1024 + 777, seed=1)
    server.add_file("multi.bin", data)
    before = server.state.range_requests["multi.bin"]

    runner = await _run(server.url_for("multi.bin"), tmp_path)

    assert runner.state is TaskState.COMPLETED
    assert runner.dest_path == tmp_path / "multi.bin"
    assert sha256(runner.dest_path) == sha256(data)
    assert len(runner.segments) >= 6, "expected the file to be split"
    assert server.state.range_requests["multi.bin"] > before + 1
    assert not (tmp_path / "multi.bin.part").exists()
    assert not (tmp_path / "multi.bin.part.idmdown").exists()


async def test_single_connection_download(server, tmp_path):
    data = make_payload(1_500_000, seed=2)
    server.add_file("single.bin", data)
    runner = await _run(server.url_for("single.bin"), tmp_path, connections=1)
    assert runner.state is TaskState.COMPLETED
    assert sha256(runner.dest_path) == sha256(data)


async def test_falls_back_to_one_stream_when_range_unsupported(server, tmp_path):
    data = make_payload(2 * 1024 * 1024, seed=3)
    server.add_file("norange.bin", data)
    runner = await _run(server.url_for("norange.bin", norange=1), tmp_path)
    assert runner.state is TaskState.COMPLETED
    assert runner.resumable is False
    assert len(runner.segments) == 1
    assert sha256(runner.dest_path) == sha256(data)


async def test_download_without_content_length(server, tmp_path):
    data = make_payload(700_000, seed=4)
    server.add_file("nolength.bin", data)
    runner = await _run(server.url_for("nolength.bin", nolength=1), tmp_path)
    assert runner.state is TaskState.COMPLETED
    assert runner.size == len(data)
    assert runner.dest_path.stat().st_size == len(data)
    assert sha256(runner.dest_path) == sha256(data)


async def test_filename_comes_from_content_disposition(server, tmp_path):
    data = make_payload(200_000, seed=5)
    server.add_file("disp-src.bin", data, disposition="Kế hoạch.pdf")
    runner = await _run(server.url_for("disp-src.bin"), tmp_path)
    assert runner.dest_path.name == "Kế hoạch.pdf"


async def test_categories_sort_into_subfolders(server, tmp_path):
    data = make_payload(100_000, seed=6)
    server.add_file("clip.mp4", data)
    runner = await _run(server.url_for("clip.mp4"), tmp_path, use_categories=True)
    assert runner.dest_path == tmp_path / "Video" / "clip.mp4"
    assert sha256(runner.dest_path) == sha256(data)


async def test_existing_file_is_not_overwritten(server, tmp_path):
    data = make_payload(50_000, seed=7)
    server.add_file("dup.bin", data)
    (tmp_path / "dup.bin").write_bytes(b"previous")
    runner = await _run(server.url_for("dup.bin"), tmp_path)
    assert runner.dest_path.name == "dup (1).bin"
    assert (tmp_path / "dup.bin").read_bytes() == b"previous"


# ------------------------------------------------------------------ failures


async def test_retries_after_a_dropped_connection(server, tmp_path):
    data = make_payload(2 * 1024 * 1024, seed=8)
    server.add_file("drop.bin", data)
    runner = await _run(
        server.url_for("drop.bin", drop=90_000, dropcount=2), tmp_path, connections=2
    )
    assert runner.state is TaskState.COMPLETED
    assert sha256(runner.dest_path) == sha256(data)


async def test_recovers_from_temporary_server_errors(server, tmp_path):
    data = make_payload(300_000, seed=9)
    server.add_file("flaky.bin", data)
    runner = await _run(server.url_for("flaky.bin", fail=2), tmp_path, connections=1)
    assert runner.state is TaskState.COMPLETED
    assert sha256(runner.dest_path) == sha256(data)


async def test_missing_resource_ends_in_error(server, tmp_path):
    runner = await _run(f"{server.base_url}/missing.bin", tmp_path)
    assert runner.state is TaskState.ERROR
    assert "404" in (runner.error or "")


# -------------------------------------------------------------- pause/resume


async def _run_until(runner: TaskRunner, threshold: int, timeout: float = 30.0):
    task = asyncio.create_task(runner.run())
    deadline = asyncio.get_running_loop().time() + timeout
    while runner._downloaded < threshold:
        if task.done() or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.02)
    return task


async def test_pause_then_resume_completes_the_file(server, tmp_path):
    data = make_payload(3 * 1024 * 1024, seed=10)
    server.add_file("pause.bin", data)
    url = server.url_for("pause.bin")

    first = TaskRunner(1, _request(url, tmp_path, connections=4, speed_limit=1_500_000))
    task = await _run_until(first, 400_000)
    first.request_pause()
    assert await task is TaskState.PAUSED

    part = tmp_path / "pause.bin.part"
    meta = ResumeMeta.load(tmp_path / "pause.bin.part.idmdown")
    assert part.exists() and meta is not None
    assert 0 < meta.downloaded < len(data)
    assert meta.downloaded == first._downloaded

    recorder = _Recorder()
    second = TaskRunner(1, _request(url, tmp_path, connections=4), on_event=recorder)
    assert await second.run() is TaskState.COMPLETED
    assert recorder.downloaded_at("downloading") == meta.downloaded, "should resume, not restart"
    assert sha256(second.dest_path) == sha256(data)
    assert not part.exists()


async def test_cancel_removes_partial_files(server, tmp_path):
    data = make_payload(3 * 1024 * 1024, seed=11)
    server.add_file("cancel.bin", data)
    runner = TaskRunner(
        1, _request(server.url_for("cancel.bin"), tmp_path, connections=2,
                    speed_limit=1_500_000)
    )
    task = await _run_until(runner, 300_000)
    runner.request_cancel()
    assert await task is TaskState.CANCELLED
    assert not (tmp_path / "cancel.bin.part").exists()
    assert not (tmp_path / "cancel.bin.part.idmdown").exists()
    assert not (tmp_path / "cancel.bin").exists()


async def test_changed_resource_restarts_from_zero(server, tmp_path):
    data = make_payload(3 * 1024 * 1024, seed=12)
    server.add_file("changed.bin", data)
    url = server.url_for("changed.bin")

    first = TaskRunner(1, _request(url, tmp_path, connections=4, speed_limit=1_500_000))
    task = await _run_until(first, 400_000)
    first.request_pause()
    await task
    assert first._downloaded > 0

    server.state.etag = '"v2-changed"'
    try:
        recorder = _Recorder()
        second = TaskRunner(1, _request(url, tmp_path, connections=4), on_event=recorder)
        assert await second.run() is TaskState.COMPLETED
        assert recorder.downloaded_at("downloading") == 0, "stale data must be discarded"
        assert sha256(second.dest_path) == sha256(data)
    finally:
        server.state.etag = '"v1"'


async def test_stale_part_file_without_metadata_is_discarded(server, tmp_path):
    data = make_payload(1_200_000, seed=13)
    server.add_file("stale.bin", data)
    (tmp_path / "stale.bin.part").write_bytes(b"\x00" * 999)

    recorder = _Recorder()
    runner = await _run(server.url_for("stale.bin"), tmp_path, recorder=recorder)
    assert runner.state is TaskState.COMPLETED
    assert recorder.downloaded_at("downloading") == 0
    assert sha256(runner.dest_path) == sha256(data)


# --------------------------------------------------------- dynamic splitting


async def test_steal_work_splits_the_slowest_segment(tmp_path):
    runner = TaskRunner(1, _request("http://example.invalid/x", tmp_path))
    runner.segments = [
        Segment(index=0, start=0, end=9_999_999, done=9_999_999),   # finished
        Segment(index=1, start=10_000_000, end=39_999_999, done=0),  # 30 MB left
    ]
    stolen = await runner._steal_work()

    assert stolen is not None
    victim = runner.segments[1]
    assert victim.end == stolen.start - 1
    assert stolen.start == 10_000_000 + 15_000_000
    assert stolen.end == 39_999_999
    assert victim.remaining + stolen.remaining == 30_000_000
    assert len(runner.segments) == 3


async def test_steal_work_refuses_tiny_splits(tmp_path):
    runner = TaskRunner(1, _request("http://example.invalid/x", tmp_path))
    runner.segments = [Segment(index=0, start=0, end=MIN_SPLIT, done=0)]
    assert await runner._steal_work() is None


async def test_steal_work_returns_none_when_everything_is_done(tmp_path):
    runner = TaskRunner(1, _request("http://example.invalid/x", tmp_path))
    runner.segments = [Segment(index=0, start=0, end=99, done=100)]
    assert await runner._steal_work() is None


# ------------------------------------------------------------- rate limiting


async def test_speed_limit_is_respected(server, tmp_path):
    data = make_payload(1_500_000, seed=14)
    server.add_file("limited.bin", data)
    loop = asyncio.get_running_loop()
    start = loop.time()
    runner = await _run(
        server.url_for("limited.bin"), tmp_path, connections=4, speed_limit=700_000
    )
    elapsed = loop.time() - start
    assert runner.state is TaskState.COMPLETED
    assert elapsed >= 1.0, f"1.5 MB at 700 KB/s should take >1s, took {elapsed:.2f}s"
    assert sha256(runner.dest_path) == sha256(data)


# ----------------------------------------------------- names and collisions


async def test_two_downloads_of_the_same_file_do_not_share_a_part_file(server, tmp_path):
    """The bug: both tasks wrote `report.pdf.part`, the loser died on rename."""
    data = make_payload(512 * 1024, seed=930)
    url = server.add_file("collide/report.pdf", data)

    first = TaskRunner(1, _request(f"{url}?slow=2", tmp_path))
    second = TaskRunner(2, _request(url, tmp_path))
    states = await asyncio.gather(first.run(), second.run())

    assert states == [TaskState.COMPLETED, TaskState.COMPLETED]
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["report (1).pdf", "report.pdf"]
    for name in names:
        assert sha256(tmp_path / name) == sha256(data)
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.idmdown"))


async def test_a_finished_file_is_never_overwritten(server, tmp_path):
    data = make_payload(64 * 1024, seed=931)
    url = server.add_file("collide/again.bin", data)
    (tmp_path / "again.bin").write_bytes(b"an older download")

    runner = await _run(url, tmp_path)
    assert runner.state is TaskState.COMPLETED
    assert (tmp_path / "again.bin").read_bytes() == b"an older download"
    assert sha256(tmp_path / "again (1).bin") == sha256(data)


async def test_an_explicit_name_beats_content_disposition(server, tmp_path):
    data = make_payload(32 * 1024, seed=932)
    url = server.add_file("collide/named.bin", data, disposition="server-choice.bin")

    runner = await _run(url, tmp_path, filename="tên tôi đặt.bin")
    assert runner.state is TaskState.COMPLETED
    assert runner.dest_path is not None and runner.dest_path.name == "tên tôi đặt.bin"
    assert sha256(tmp_path / "tên tôi đặt.bin") == sha256(data)


async def test_resuming_reuses_the_part_file_instead_of_a_new_name(server, tmp_path):
    """A second run must continue the first one, not start `file (1)`."""
    data = make_payload(6 * 1024 * 1024, seed=933)
    url = server.add_file("collide/resume-me.bin", data)

    # One throttled connection, so there is time to pause it mid-flight.
    stopped = TaskRunner(1, _request(f"{url}?slow=30", tmp_path, connections=1))
    task = asyncio.create_task(stopped.run())
    for _ in range(400):
        await asyncio.sleep(0.02)
        if stopped.snapshot().downloaded > 256 * 1024:
            break
    stopped.request_pause()
    assert await task is TaskState.PAUSED
    assert (tmp_path / "resume-me.bin.part").exists()

    finished = await _run(url, tmp_path)
    assert finished.state is TaskState.COMPLETED
    assert [p.name for p in tmp_path.iterdir()] == ["resume-me.bin"]
    assert sha256(tmp_path / "resume-me.bin") == sha256(data)
