"""Torrents, against a real libtorrent seed on this machine."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.core import torrent as torrent_mod
from app.core.engine import Engine
from app.core.task import DownloadRequest, TaskState
from app.core.torrent import magnet_name, wants_torrent

from .conftest import make_payload, sha256

MB = 1024 * 1024


# ------------------------------------------------------------- without a net


@pytest.mark.parametrize("url, expected", [
    ("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x", True),
    ("https://example.com/files/linux.iso.torrent", True),
    ("https://example.com/files/LINUX.TORRENT?sig=1", True),
    ("https://example.com/files/linux.iso", False),
    ("ftp://example.com/a.torrent", False),
])
def test_which_addresses_are_torrents(url, expected):
    assert wants_torrent(url) is expected


def test_a_magnet_name():
    url = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Ubuntu%2024.04%20%3Cdesktop%3E"
    assert magnet_name(url) == "Ubuntu 24.04 _desktop_"
    assert magnet_name("magnet:?xt=urn:btih:0123") is None
    assert magnet_name("https://example.com/a") is None


def test_without_libtorrent_the_download_says_what_is_missing(monkeypatch, tmp_path):
    import builtins

    real = builtins.__import__

    def no_libtorrent(name, *args, **kwargs):
        if name == "libtorrent":
            raise ImportError("no libtorrent here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_libtorrent)
    runner = torrent_mod.TorrentTaskRunner(1, DownloadRequest(
        url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", save_dir=tmp_path
    ))
    assert asyncio.run(runner.run()) is TaskState.ERROR
    assert "pip install libtorrent" in runner.error
    assert not torrent_mod.available()


def test_a_torrent_flag_of_false_saves_the_file_itself(server, tmp_path):
    server.add_file("plain.torrent", b"d4:infod4:name1:xee")
    with Engine() as engine:
        task_id = engine.submit(DownloadRequest(
            url=server.url_for("plain.torrent"), save_dir=tmp_path, torrent=False
        ))
        assert engine.wait_idle(timeout=30)
        assert engine.state(task_id) is TaskState.COMPLETED
    assert (tmp_path / "plain.torrent").read_bytes() == b"d4:infod4:name1:xee"


# ------------------------------------------------------------ with a swarm

lt = pytest.importorskip("libtorrent")

QUIET = {
    "listen_interfaces": "127.0.0.1:0",
    "enable_dht": False,
    "enable_lsd": False,
    "enable_upnp": False,
    "enable_natpmp": False,
    "allow_multiple_connections_per_ip": True,
    # Peers on 127.0.0.1 are "local"; do not throttle or skip them.
    "rate_limit_ip_overhead": False,
}


class Seed:
    """A libtorrent session seeding `files` from `root`."""

    def __init__(self, root: Path, name: str, files: dict[str, bytes]) -> None:
        base = root / "seed"
        base.mkdir()
        content = base / name
        if len(files) == 1 and name in files:
            content.write_bytes(files[name])
        else:
            for rel, data in files.items():
                path = content / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        storage = lt.file_storage()
        lt.add_files(storage, str(content))
        maker = lt.create_torrent(storage, 64 * 1024)
        lt.set_piece_hashes(maker, str(base))
        self.data = lt.bencode(maker.generate())
        self.info = lt.torrent_info(lt.bdecode(self.data))
        self.session = lt.session(dict(QUIET))
        params = lt.add_torrent_params()
        params.ti = self.info
        params.save_path = str(base)
        params.flags |= lt.torrent_flags.seed_mode
        self.handle = self.session.add_torrent(params)
        self.port = self.session.listen_port()

    @property
    def magnet(self) -> str:
        return lt.make_magnet_uri(self.info)

    def close(self) -> None:
        self.session.remove_torrent(self.handle)


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    torrent_mod.shutdown()
    monkeypatch.setattr(torrent_mod, "SETTINGS", {**torrent_mod.SETTINGS, **QUIET})
    seeds: list[Seed] = []

    def make(name: str, files: dict[str, bytes]) -> Seed:
        root = tmp_path / f"swarm{len(seeds)}"
        root.mkdir()
        seed = Seed(root, name, files)
        seeds.append(seed)
        monkeypatch.setattr(torrent_mod, "EXTRA_PEERS", [("127.0.0.1", seed.port)])
        return seed

    yield make
    for seed in seeds:
        seed.close()
    torrent_mod.shutdown()


def run(url, save_dir, **kwargs):
    runner = torrent_mod.TorrentTaskRunner(1, DownloadRequest(url=url, save_dir=save_dir, **kwargs))
    return runner, asyncio.run(asyncio.wait_for(runner.run(), timeout=60))


def test_a_magnet_link_downloads_a_file(swarm, tmp_path):
    data = make_payload(3 * MB, seed=61)
    seed = swarm("movie.mkv", {"movie.mkv": data})
    out = tmp_path / "out"
    runner, state = run(seed.magnet, out)
    assert state is TaskState.COMPLETED, runner.error
    assert sha256(out / "movie.mkv") == sha256(data)
    assert runner.dest_path == out / "movie.mkv"
    assert runner.size == len(data)
    assert not list(out.glob("*.boltdown-torrent"))


def test_a_torrent_file_from_the_web_downloads_a_folder(swarm, server, tmp_path):
    files = {"a.txt": make_payload(200_000, seed=62), "sub/b.bin": make_payload(900_000, seed=63)}
    seed = swarm("album", files)
    server.add_file("album.torrent", seed.data)
    out = tmp_path / "out"
    runner, state = run(server.url_for("album.torrent"), out)
    assert state is TaskState.COMPLETED, runner.error
    for rel, data in files.items():
        assert sha256(out / "album" / rel) == sha256(data)
    assert runner.filename == "album"


def test_a_page_that_is_not_a_torrent_file(server, tmp_path):
    server.add_file("fake.torrent", b"<html>login first</html>")
    runner, state = run(server.url_for("fake.torrent"), tmp_path)
    assert state is TaskState.ERROR and "torrent" in runner.error.lower()


def test_pause_saves_resume_data_and_resume_finishes(swarm, tmp_path):
    data = make_payload(4 * MB, seed=64)
    seed = swarm("big.bin", {"big.bin": data})
    out = tmp_path / "out"

    engine = Engine(max_concurrent=1)
    with engine:
        task_id = engine.submit(DownloadRequest(url=seed.magnet, save_dir=out, speed_limit=600_000))
        deadline = time.time() + 30
        while time.time() < deadline:
            snap = engine.snapshot(task_id)
            if snap and snap.downloaded > 500_000:
                break
            time.sleep(0.1)
        assert engine.snapshot(task_id).downloaded > 500_000
        engine.pause(task_id)
        assert engine.wait_idle(timeout=20)
        assert engine.state(task_id) is TaskState.PAUSED
        assert (out / "big.bin.boltdown-torrent").exists()

        engine._requests[task_id].speed_limit = None
        engine.resume(task_id)
        assert engine.wait_idle(timeout=60)
        assert engine.state(task_id) is TaskState.COMPLETED
    assert sha256(out / "big.bin") == sha256(data)
    assert not (out / "big.bin.boltdown-torrent").exists()


def test_cancel_removes_what_was_downloaded(swarm, tmp_path):
    data = make_payload(4 * MB, seed=65)
    seed = swarm("gone.bin", {"gone.bin": data})
    out = tmp_path / "out"
    with Engine() as engine:
        task_id = engine.submit(DownloadRequest(url=seed.magnet, save_dir=out, speed_limit=400_000))
        deadline = time.time() + 30
        while time.time() < deadline:
            snap = engine.snapshot(task_id)
            if snap and snap.downloaded > 200_000:
                break
            time.sleep(0.1)
        engine.cancel(task_id)
        assert engine.wait_idle(timeout=20)
        assert engine.state(task_id) is TaskState.CANCELLED
    time.sleep(0.5)  # libtorrent deletes on its own thread
    assert not (out / "gone.bin").exists()


def test_the_speed_limit_reaches_libtorrent(swarm, tmp_path):
    data = make_payload(2 * MB, seed=66)
    seed = swarm("slow.bin", {"slow.bin": data})
    started = time.monotonic()
    runner, state = run(seed.magnet, tmp_path / "out", speed_limit=700_000)
    assert state is TaskState.COMPLETED, runner.error
    assert time.monotonic() - started > 1.5


def test_the_engine_sends_magnets_to_the_torrent_runner(swarm, tmp_path):
    data = make_payload(500_000, seed=67)
    seed = swarm("small.bin", {"small.bin": data})
    events = []
    with Engine(on_event=lambda e: events.append(e)) as engine:
        task_id = engine.submit(DownloadRequest(url=seed.magnet, save_dir=tmp_path))
        assert engine.wait_idle(timeout=60)
        assert engine.state(task_id) is TaskState.COMPLETED
    done = [e for e in events if e.type == "completed"][-1].snapshot
    assert done.path == str(tmp_path / "small.bin") and done.size == len(data)
    assert sha256(tmp_path / "small.bin") == sha256(data)
