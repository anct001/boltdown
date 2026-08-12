"""Locating and driving ffmpeg.

ffmpeg is only ever used to *remux* - copy already downloaded streams into a
container. No re-encoding, so the calls are I/O bound and finish in seconds
even for a feature length video. The binary is optional: without it HLS
downloads still produce a playable `.ts` and separate video/audio tracks are
left side by side rather than merged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..core.errors import FatalError
from ..util.log import get_logger
from ..util.paths import data_dir

log = get_logger(__name__)

EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
ENV_VAR = "IDMCLONE_FFMPEG"
#: keep the last few stderr lines - ffmpeg puts the real reason at the end
ERROR_TAIL = 12


class FfmpegError(FatalError):
    """ffmpeg is missing, or exited non-zero."""


class FfmpegMissing(FfmpegError):
    def __init__(self) -> None:
        super().__init__(
            "ffmpeg not found - install it, put ffmpeg.exe next to the app, "
            f"or set the {ENV_VAR} environment variable"
        )


def _candidates(explicit: str | os.PathLike[str] | None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))
    env = os.environ.get(ENV_VAR)
    if env:
        paths.append(Path(env))
    app_dir = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None
    for base in (app_dir, Path(sys.executable).parent, data_dir() / "bin", data_dir()):
        if base is not None:
            paths.append(Path(base) / EXE)
    return paths


def find_ffmpeg(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolve an ffmpeg binary, or None. Setting/env first, then PATH."""
    for candidate in _candidates(explicit):
        if candidate.is_dir():
            candidate = candidate / EXE
        if candidate.is_file():
            return candidate
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def require_ffmpeg(explicit: str | os.PathLike[str] | None = None) -> Path:
    binary = find_ffmpeg(explicit)
    if binary is None:
        raise FfmpegMissing()
    return binary


async def run(args: list[str], *, ffmpeg: str | os.PathLike[str] | None = None) -> str:
    """Run ffmpeg with `args`; return stderr. Raises `FfmpegError` on failure."""
    binary = require_ffmpeg(ffmpeg)
    cmd = [str(binary), "-hide_banner", "-nostdin", "-loglevel", "warning", *args]
    log.debug("ffmpeg %s", " ".join(args))
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _out, err = await process.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        raise
    stderr = err.decode("utf-8", "replace")
    if process.returncode != 0:
        tail = "\n".join(stderr.strip().splitlines()[-ERROR_TAIL:])
        raise FfmpegError(f"ffmpeg exited {process.returncode}: {tail}")
    return stderr


def _concat_list(parts: list[Path], list_path: Path) -> None:
    """Write a concat demuxer script. Single quotes are the only escape."""
    lines = []
    for part in parts:
        text = str(part.resolve()).replace("'", r"'\''")
        lines.append(f"file '{text}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def concat(
    parts: list[Path],
    output: Path,
    *,
    ffmpeg: str | os.PathLike[str] | None = None,
    work_dir: Path | None = None,
) -> None:
    """Join already-contiguous media parts into `output` without re-encoding."""
    if not parts:
        raise FfmpegError("nothing to concatenate")
    work_dir = work_dir or output.parent
    list_path = work_dir / (output.stem + ".concat.txt")
    _concat_list(parts, list_path)
    base = ["-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy"]
    try:
        # MPEG-TS carries AAC in ADTS frames; MP4 needs them re-headed. The
        # filter is a no-op error for non-AAC streams, hence the retry.
        try:
            await run([*base, "-bsf:a", "aac_adtstoasc", str(output)], ffmpeg=ffmpeg)
        except FfmpegError:
            await run([*base, str(output)], ffmpeg=ffmpeg)
    finally:
        list_path.unlink(missing_ok=True)


async def merge_tracks(
    video: Path,
    audio: Path,
    output: Path,
    *,
    ffmpeg: str | os.PathLike[str] | None = None,
) -> None:
    """Mux a separate video and audio track into one file (stream copy)."""
    await run(
        [
            "-y", "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy", "-shortest", str(output),
        ],
        ffmpeg=ffmpeg,
    )


async def remux(source: Path, output: Path, *, ffmpeg: str | os.PathLike[str] | None = None) -> None:
    """Rewrap one file into another container, copying the streams."""
    await run(["-y", "-i", str(source), "-c", "copy", str(output)], ffmpeg=ffmpeg)


async def version(ffmpeg: str | os.PathLike[str] | None = None) -> str:
    binary = require_ffmpeg(ffmpeg)
    process = await asyncio.create_subprocess_exec(
        str(binary), "-version",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    first = out.decode("utf-8", "replace").splitlines()
    return first[0] if first else ""
