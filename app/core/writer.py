"""Positional file writing.

Each segment owns its own OS file descriptor, so writes never contend for a
shared file position and no lock is needed. `os.pwrite` is Unix-only, so on
Windows we emulate it with a per-descriptor `lseek` + `write` - safe precisely
because the descriptors are not shared between segments.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from .errors import DiskFullError

_O_BINARY = getattr(os, "O_BINARY", 0)
_HAS_PWRITE = hasattr(os, "pwrite")


def write_at(fd: int, data: bytes, offset: int) -> None:
    """Write all of `data` at `offset`, handling short writes."""
    try:
        if _HAS_PWRITE:
            written = 0
            while written < len(data):
                written += os.pwrite(fd, data[written:], offset + written)
        else:
            os.lseek(fd, offset, os.SEEK_SET)
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise DiskFullError(f"no space left while writing: {exc}") from exc
        raise


class TargetFile:
    """The `.part` file being filled in, plus its per-segment descriptors."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fds: list[int] = []

    def allocate(self, size: int | None) -> None:
        """Create the file and reserve `size` bytes up front.

        Preallocating means a full disk is reported immediately instead of at
        90% progress, and it keeps the file from fragmenting badly while eight
        segments write to it at once.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists()
        flags = os.O_RDWR | os.O_CREAT | _O_BINARY
        fd = os.open(self.path, flags, 0o644)
        try:
            if size is not None:
                current = os.fstat(fd).st_size
                if not exists or current != size:
                    try:
                        os.ftruncate(fd, size)
                    except OSError as exc:
                        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                            raise DiskFullError(
                                f"cannot reserve {size} bytes: {exc}"
                            ) from exc
                        raise
        finally:
            os.close(fd)

    def open_fd(self) -> int:
        """Open an independent descriptor for one segment."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o644)
        self._fds.append(fd)
        return fd

    def close_fd(self, fd: int) -> None:
        try:
            self._fds.remove(fd)
        except ValueError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def close(self) -> None:
        for fd in list(self._fds):
            self.close_fd(fd)

    def current_size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def truncate_to(self, size: int) -> None:
        """Trim trailing preallocated bytes (used when the size was unknown)."""
        fd = os.open(self.path, os.O_RDWR | _O_BINARY)
        try:
            os.ftruncate(fd, size)
        finally:
            os.close(fd)

    def __enter__(self) -> "TargetFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
