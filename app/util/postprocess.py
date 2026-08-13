"""What happens to a file after the last byte lands.

Two optional steps, both off by default because both touch the user's disk in
ways they did not explicitly ask for at download time:

* unpack an archive next to itself;
* hand the file to Microsoft Defender for a scan.

Everything here runs on a worker thread - unpacking a 2 GB archive on the GUI
thread would freeze the window - so the functions take no Qt types and return
plain results the caller can display.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

#: what Python can unpack without another program
ARCHIVES = (".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz")
DEFENDER = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
SCAN_TIMEOUT = 600


@dataclass(slots=True)
class Result:
    ok: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(ARCHIVES)


def extract_dir_for(path: Path) -> Path:
    """`downloads/pack.zip` -> `downloads/pack`, without clobbering anything."""
    base = path.parent / path.name.split(".")[0]
    candidate = base
    index = 1
    while candidate.exists():
        candidate = base.parent / f"{base.name} ({index})"
        index += 1
    return candidate


def is_within(root: Path, target: Path) -> bool:
    """Guard against `../..` entries in a malicious archive (zip slip)."""
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def extract(path: Path, destination: Path | None = None) -> Result:
    """Unpack an archive beside itself. Refuses paths that escape the folder."""
    path = Path(path)
    if not path.is_file():
        return Result(False, "file is gone")
    if not is_archive(path):
        return Result(False, "not an archive we can open")
    target = Path(destination) if destination else extract_dir_for(path)
    target.mkdir(parents=True, exist_ok=True)
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if not is_within(target, target / member):
                        return Result(False, f"unsafe path in archive: {member}")
                archive.extractall(target)
        else:
            shutil.unpack_archive(str(path), str(target))
    except (OSError, zipfile.BadZipFile, shutil.ReadError) as exc:
        log.info("could not unpack %s: %s", path.name, exc)
        return Result(False, str(exc))
    count = sum(1 for _ in target.rglob("*"))
    log.info("unpacked %s into %s (%d entries)", path.name, target, count)
    return Result(True, str(target))


def defender_available() -> bool:
    return sys.platform == "win32" and DEFENDER.is_file()


def scan(path: Path, runner=None) -> Result:
    """Scan one file with Microsoft Defender.

    Exit code 0 means clean, 2 means something was found; anything else is
    Defender itself having a problem, which is not the download's fault.
    """
    path = Path(path)
    if not defender_available():
        return Result(False, "Microsoft Defender is not available")
    if not path.is_file():
        return Result(False, "file is gone")
    argv = [str(DEFENDER), "-Scan", "-ScanType", "3", "-File", str(path)]
    try:
        completed = (runner or _run)(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(False, str(exc))
    code = getattr(completed, "returncode", 1)
    if code == 0:
        return Result(True, "clean")
    if code == 2:
        return Result(False, "threat found")
    return Result(False, f"scanner exited {code}")


def _run(argv: list[str]):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=SCAN_TIMEOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
