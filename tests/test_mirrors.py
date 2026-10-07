"""One file from several addresses at once."""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError
from app.core.segment import SegmentWorker
from app.core.task import DownloadRequest, TaskRunner, TaskState

from .conftest import make_payload, sha256


def _other_host(url: str) -> str:
    return url.replace("127.0.0.1", "localhost")


async def _run(url, tmp_path, mirrors, connections=8):
    runner = TaskRunner(1, DownloadRequest(
        url=url, save_dir=tmp_path, connections=connections, mirrors=mirrors,
    ))
    assert await runner.run() is TaskState.COMPLETED
    return runner


async def test_segments_are_fetched_from_every_source(server, tmp_path):
    data = make_payload(6 * 1024 * 1024 + 3, seed=91)
    url = server.add_file("main.bin", data)
    mirror = _other_host(server.add_file("copy.bin", data))
    before = (server.state.requests["main.bin"], server.state.requests["copy.bin"])

    runner = await _run(url, tmp_path, [mirror])

    assert sha256(runner.dest_path) == sha256(data)
    assert runner.dest_path.name == "main.bin"
    assert len(runner._sources) == 2
    assert server.state.requests["main.bin"] > before[0]
    assert server.state.requests["copy.bin"] - before[1] >= 2, "the mirror only got its probe"


async def test_a_mirror_of_another_size_is_left_out(server, tmp_path):
    data = make_payload(3 * 1024 * 1024, seed=92)
    url = server.add_file("sized.bin", data)
    other = _other_host(server.add_file("other-size.bin", data + b"!"))
    missing = _other_host(server.url_for("does-not-exist.bin"))

    runner = await _run(url, tmp_path, [other, missing, "ftp://x/y", url])

    assert runner._sources == [runner._probe.final_url]
    assert sha256(runner.dest_path) == sha256(data)


async def test_a_mirror_that_stops_serving_costs_only_its_connection(
    server, tmp_path, monkeypatch
):
    data = make_payload(4 * 1024 * 1024 + 11, seed=93)
    url = server.add_file("steady.bin", data)
    mirror = _other_host(server.add_file("flaky-copy.bin", data))
    real = SegmentWorker._stream_once

    async def mirror_dies(self, loop, segment, primed=None):
        if "flaky-copy" in self.url:
            raise NotFoundError("HTTP 404: resource not found")
        return await real(self, loop, segment, primed)

    monkeypatch.setattr(SegmentWorker, "_stream_once", mirror_dies)
    runner = await _run(url, tmp_path, [mirror])
    assert sha256(runner.dest_path) == sha256(data)


async def test_the_main_address_failing_still_fails(server, tmp_path, monkeypatch):
    data = make_payload(2 * 1024 * 1024, seed=94)
    url = server.add_file("gone.bin", data)
    real = SegmentWorker._stream_once

    async def all_gone(self, loop, segment, primed=None):
        if primed is not None:
            await primed.aclose()
        raise NotFoundError("HTTP 404: resource not found")

    monkeypatch.setattr(SegmentWorker, "_stream_once", all_gone)
    runner = TaskRunner(1, DownloadRequest(url=url, save_dir=tmp_path, connections=4))
    assert await runner.run() is TaskState.ERROR
    assert real is not None


def test_mirrors_are_kept_with_the_download(tmp_path):
    pytest.importorskip("PySide6")
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui.controller import Controller, clean_mirrors

    assert clean_mirrors(
        [" https://b/x ", "https://a/x", "ftp://c/x", "https://b/x", ""], "https://a/x"
    ) == ["https://b/x"]

    db = Database(tmp_path / "m.db")
    settings = Settings(db)
    controller = Controller(db, settings)
    item = controller.add("https://a.example/f.iso", start_now=False,
                          mirrors=["https://b.example/f.iso", "https://c.example/f.iso"])
    assert db.get_mirrors(item.db_id) == ["https://b.example/f.iso", "https://c.example/f.iso"]
    assert controller._build_request(item).mirrors == item.mirrors

    again = Controller(db, settings)
    again.restore()
    assert again.item(item.db_id).mirrors == item.mirrors
    db.close()
