from __future__ import annotations

import time

from app.core.engine import Engine
from app.core.task import DownloadRequest, TaskState

from .conftest import make_payload, sha256


def test_engine_runs_several_downloads(server, tmp_path):
    files = {}
    for i in range(3):
        data = make_payload(400_000 + i * 100_000, seed=100 + i)
        name = f"engine-{i}.bin"
        server.add_file(name, data)
        files[name] = data

    events: list[str] = []
    with Engine(max_concurrent=2, on_event=lambda e: events.append(e.type)) as engine:
        ids = [
            engine.submit(
                DownloadRequest(url=server.url_for(name), save_dir=tmp_path, connections=4)
            )
            for name in files
        ]
        assert engine.wait_idle(timeout=60)
        for task_id in ids:
            assert engine.state(task_id) is TaskState.COMPLETED

    for name, data in files.items():
        assert sha256(tmp_path / name) == sha256(data)
    assert "added" in events and "completed" in events


def test_engine_pause_and_resume(server, tmp_path):
    data = make_payload(2 * 1024 * 1024, seed=200)
    server.add_file("engine-pause.bin", data)

    with Engine(max_concurrent=1, speed_limit=900_000) as engine:
        task_id = engine.submit(
            DownloadRequest(
                url=server.url_for("engine-pause.bin"), save_dir=tmp_path, connections=4
            )
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            snap = engine.snapshot(task_id)
            if snap and snap.downloaded > 300_000:
                break
            time.sleep(0.05)

        engine.pause(task_id)
        assert engine.wait_idle(timeout=20)
        assert engine.state(task_id) is TaskState.PAUSED
        assert (tmp_path / "engine-pause.bin.part").exists()

        engine.set_speed_limit(None)
        engine.resume(task_id)
        assert engine.wait_idle(timeout=60)
        assert engine.state(task_id) is TaskState.COMPLETED

    assert sha256(tmp_path / "engine-pause.bin") == sha256(data)


def test_engine_reports_failures_without_stopping(server, tmp_path):
    data = make_payload(120_000, seed=300)
    server.add_file("engine-ok.bin", data)

    with Engine(max_concurrent=2) as engine:
        bad = engine.submit(
            DownloadRequest(url=f"{server.base_url}/nope.bin", save_dir=tmp_path)
        )
        good = engine.submit(
            DownloadRequest(url=server.url_for("engine-ok.bin"), save_dir=tmp_path)
        )
        assert engine.wait_idle(timeout=60)
        assert engine.state(bad) is TaskState.ERROR
        assert engine.state(good) is TaskState.COMPLETED

    assert sha256(tmp_path / "engine-ok.bin") == sha256(data)
