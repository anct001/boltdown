from __future__ import annotations

import os
import threading

from app.core.writer import TargetFile, write_at


def test_allocate_reserves_size(tmp_path):
    target = TargetFile(tmp_path / "big.bin")
    target.allocate(5_000_000)
    assert target.path.stat().st_size == 5_000_000
    target.close()


def test_allocate_without_size_creates_empty_file(tmp_path):
    target = TargetFile(tmp_path / "unknown.bin")
    target.allocate(None)
    assert target.path.exists()
    assert target.path.stat().st_size == 0
    target.close()


def test_parallel_positional_writes_do_not_interleave(tmp_path):
    """Each thread gets its own fd, so offsets must stay independent."""
    size = 8 * 4096
    target = TargetFile(tmp_path / "parallel.bin")
    target.allocate(size)

    blocks = {i: bytes([65 + i]) * 4096 for i in range(8)}
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        fd = target.open_fd()
        barrier.wait()
        for _ in range(20):  # rewrite repeatedly to provoke position races
            write_at(fd, blocks[index], index * 4096)
        target.close_fd(fd)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = target.path.read_bytes()
    target.close()
    for i in range(8):
        assert data[i * 4096 : (i + 1) * 4096] == blocks[i]


def test_write_at_handles_offset_beyond_current_size(tmp_path):
    path = tmp_path / "sparse.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        write_at(fd, b"tail", 1000)
    finally:
        os.close(fd)
    assert path.stat().st_size == 1004
    assert path.read_bytes()[1000:] == b"tail"


def test_truncate_to(tmp_path):
    target = TargetFile(tmp_path / "trim.bin")
    target.allocate(1000)
    target.truncate_to(400)
    assert target.path.stat().st_size == 400
    target.close()
