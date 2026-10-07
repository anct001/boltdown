"""FTP downloads: segmented, resumable, and polite to servers with limits."""

from __future__ import annotations

import time

import pytest

from app.core.engine import Engine
from app.core.errors import FatalError
from app.core.ftp import FtpTarget, FtpTaskRunner, is_ftp
from app.core.task import DownloadRequest, TaskState

from .conftest import make_payload, sha256
from .ftp_server import FtpServer

MB = 1024 * 1024


@pytest.fixture
def ftp():
    server = FtpServer()
    yield server
    server.close()


def run(url, tmp_path, **kwargs):
    import asyncio

    runner = FtpTaskRunner(1, DownloadRequest(url=url, save_dir=tmp_path, **kwargs))
    return runner, asyncio.run(runner.run())


# ------------------------------------------------------------------ addresses


def test_ftp_addresses_are_recognised():
    assert is_ftp("ftp://example.com/a.iso")
    assert is_ftp("FTPS://example.com/a.iso")
    assert not is_ftp("https://example.com/a.iso")


def test_an_address_is_taken_apart():
    target = FtpTarget.from_url("ftp://bob:s%40cret@example.com:2121/pub/linux/a%20b.iso;type=i")
    assert (target.host, target.port, target.user, target.password) == (
        "example.com", 2121, "bob", "s@cret"
    )
    assert target.dirs == ["pub", "linux"]
    assert target.name == "a b.iso"
    assert target.path == "/pub/linux/a b.iso"
    assert not target.tls


def test_anonymous_unless_told_otherwise():
    target = FtpTarget.from_url("ftps://example.com/file.bin")
    assert target.user == "anonymous" and target.tls and target.port == 21
    ruled = FtpTarget.from_url("ftp://example.com/file.bin", auth=("ann", "pw"))
    assert (ruled.user, ruled.password) == ("ann", "pw")
    # The address wins over a site rule: it was typed for this file.
    typed = FtpTarget.from_url("ftp://zed:1@example.com/file.bin", auth=("ann", "pw"))
    assert typed.user == "zed"


def test_a_folder_is_not_a_file():
    with pytest.raises(FatalError):
        FtpTarget.from_url("ftp://example.com/pub/")


# ------------------------------------------------------------------ downloads


def test_a_file_arrives_over_several_connections(ftp, tmp_path):
    data = make_payload(3 * MB, seed=41)
    url = ftp.add_file("/pub/big.bin", data)
    runner, state = run(url, tmp_path, connections=4)
    assert state is TaskState.COMPLETED, runner.error
    assert sha256(tmp_path / "big.bin") == sha256(data)
    assert ftp.state.peak_clients >= 2
    offsets = sorted(rest for _, rest in ftp.state.retr)
    assert offsets[0] == 0 and len(set(offsets)) >= 2


def test_logging_in(ftp, tmp_path):
    ftp.state.users = {"bob": "pw"}
    data = make_payload(200_000, seed=42)
    url = ftp.add_file("/secret.bin", data)

    runner, state = run(url, tmp_path)
    assert state is TaskState.ERROR and "login" in (runner.error or "").lower()

    with_login = url.replace("ftp://", "ftp://bob:pw@")
    runner, state = run(with_login, tmp_path)
    assert state is TaskState.COMPLETED, runner.error

    (tmp_path / "secret.bin").unlink()
    runner, state = run(url, tmp_path, auth=("bob", "pw"))
    assert state is TaskState.COMPLETED, runner.error
    assert sha256(tmp_path / "secret.bin") == sha256(data)


def test_a_missing_file_is_an_error_not_a_retry(ftp, tmp_path):
    url = f"ftp://127.0.0.1:{ftp.port}/nothing-here.bin"
    started = time.monotonic()
    runner, state = run(url, tmp_path)
    assert state is TaskState.ERROR
    assert time.monotonic() - started < 5


def test_one_connection_allowed_still_gets_every_segment(ftp, tmp_path):
    ftp.state.max_clients = 1
    data = make_payload(3 * MB, seed=43)
    url = ftp.add_file("/one.bin", data)
    runner, state = run(url, tmp_path, connections=4)
    assert state is TaskState.COMPLETED, runner.error
    assert sha256(tmp_path / "one.bin") == sha256(data)
    assert ftp.state.peak_clients == 1


def test_two_connections_allowed(ftp, tmp_path):
    ftp.state.max_clients = 2
    data = make_payload(4 * MB, seed=44)
    url = ftp.add_file("/two.bin", data)
    runner, state = run(url, tmp_path, connections=6)
    assert state is TaskState.COMPLETED, runner.error
    assert sha256(tmp_path / "two.bin") == sha256(data)


def test_a_server_without_rest_gets_one_connection(ftp, tmp_path):
    ftp.state.no_rest = True
    data = make_payload(2 * MB, seed=45)
    url = ftp.add_file("/norest.bin", data)
    runner, state = run(url, tmp_path, connections=4)
    assert state is TaskState.COMPLETED, runner.error
    assert not runner.resumable
    assert ftp.state.retr == [("/norest.bin", 0)]
    assert sha256(tmp_path / "norest.bin") == sha256(data)


def test_a_cut_data_connection_carries_on_from_where_it_stopped(ftp, tmp_path):
    ftp.state.cut_after = 300_000
    ftp.state.cuts = 2
    data = make_payload(2 * MB, seed=46)
    url = ftp.add_file("/cut.bin", data)
    runner, state = run(url, tmp_path, connections=2, max_retries=5)
    assert state is TaskState.COMPLETED, runner.error
    assert sha256(tmp_path / "cut.bin") == sha256(data)
    # The retries asked for the rest, not the whole segment again.
    assert any(rest not in (0, 1 * MB) for _, rest in ftp.state.retr), ftp.state.retr


def test_pause_and_resume_keep_the_bytes(ftp, tmp_path):
    ftp.state.rate = 400_000
    data = make_payload(2 * MB, seed=47)
    url = ftp.add_file("/pause.bin", data)

    with Engine(max_concurrent=1) as engine:
        task_id = engine.submit(DownloadRequest(url=url, save_dir=tmp_path, connections=2))
        deadline = time.time() + 20
        while time.time() < deadline:
            snap = engine.snapshot(task_id)
            if snap and snap.downloaded > 300_000:
                break
            time.sleep(0.05)
        engine.pause(task_id)
        assert engine.wait_idle(timeout=20)
        assert engine.state(task_id) is TaskState.PAUSED
        part = next(tmp_path.glob("pause.bin*.part"), None) or tmp_path / "pause.bin.part"
        assert part.exists()

        ftp.state.retr.clear()
        ftp.state.rate = 0
        engine.resume(task_id)
        assert engine.wait_idle(timeout=30)
        assert engine.state(task_id) is TaskState.COMPLETED
    assert sha256(tmp_path / "pause.bin") == sha256(data)
    assert all(rest > 0 for _, rest in ftp.state.retr), ftp.state.retr


def test_the_speed_limit_holds(ftp, tmp_path):
    data = make_payload(1 * MB, seed=48)
    url = ftp.add_file("/slow.bin", data)
    started = time.monotonic()
    runner, state = run(url, tmp_path, connections=2, speed_limit=400_000)
    elapsed = time.monotonic() - started
    assert state is TaskState.COMPLETED, runner.error
    # 1 MB at 400 KB/s, less the bucket's first second of burst.
    assert elapsed > 1.2


def test_cancel_removes_the_part_file(ftp, tmp_path):
    ftp.state.rate = 200_000
    data = make_payload(2 * MB, seed=49)
    url = ftp.add_file("/cancel.bin", data)
    with Engine() as engine:
        task_id = engine.submit(DownloadRequest(url=url, save_dir=tmp_path))
        deadline = time.time() + 20
        while time.time() < deadline:
            snap = engine.snapshot(task_id)
            if snap and snap.downloaded > 100_000:
                break
            time.sleep(0.05)
        engine.cancel(task_id)
        assert engine.wait_idle(timeout=20)
        assert engine.state(task_id) is TaskState.CANCELLED
    assert not list(tmp_path.glob("cancel.bin*"))


def test_a_server_that_will_not_say_the_size(ftp, tmp_path):
    ftp.state.no_size = True
    data = make_payload(700_000, seed=50)
    url = ftp.add_file("/nosize.bin", data)
    runner, state = run(url, tmp_path, connections=4)
    assert state is TaskState.COMPLETED, runner.error
    assert runner.size == len(data)
    assert sha256(tmp_path / "nosize.bin") == sha256(data)


@pytest.mark.parametrize("text, ok", [
    ("https://example.com/a.zip", True),
    ("ftp://example.com/a.iso", True),
    ("FTPS://example.com/a.iso", True),
    ("magnet:?xt=urn:btih:0123456789abcdef&dn=x", True),
    ("magnet:?dn=only-a-name", False),
    ("ftp://", False),
    ("file:///etc/passwd", False),
    ("javascript:alert(1)", False),
    (None, False),
])
def test_which_links_are_downloads(text, ok):
    from app.util.links import is_download_link

    assert is_download_link(text) is ok
