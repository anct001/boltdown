from __future__ import annotations

import hashlib
import os
import random
import tempfile
from pathlib import Path

import pytest

# Keep logs / database out of the real user profile during tests.
os.environ.setdefault(
    "BOLTDOWN_HOME", str(Path(tempfile.gettempdir()) / "boltdown-tests")
)
# Qt widgets must render without a display; set before PySide6 is imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from .server import FileServer  # noqa: E402


@pytest.fixture(autouse=True)
def _no_audible_tests(monkeypatch):
    """A test run must not make noise on the machine running it.

    The player is replaced, not the setting: the code that decides *whether*
    to play still runs, so the tests still exercise it - they just never reach
    the speaker. A test that wants to observe playback passes its own player.
    """
    monkeypatch.setattr("app.ui.sounds._winsound_player", lambda path: None)


@pytest.fixture(scope="session")
def server():
    srv = FileServer().start()
    yield srv
    srv.stop()


@pytest.fixture
def payload() -> bytes:
    return make_payload(3 * 1024 * 1024 + 12345)


def make_payload(size: int, seed: int = 1234) -> bytes:
    return random.Random(seed).randbytes(size)


def sha256(data: bytes | Path) -> str:
    if isinstance(data, Path):
        digest = hashlib.sha256()
        with open(data, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    return hashlib.sha256(data).hexdigest()
