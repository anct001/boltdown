"""Filename derivation and Windows-safe sanitising."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_STEM = 180


def sanitize(name: str, fallback: str = "download") -> str:
    """Make `name` safe to use as a Windows file name."""
    name = _INVALID.sub("_", name).strip()
    # Windows silently strips trailing dots/spaces; do it explicitly so the
    # name we record matches the name on disk.
    name = name.rstrip(". ")
    if not name:
        return fallback
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM]
    return f"{stem}.{ext}" if ext else stem


def from_content_disposition(header: str | None) -> str | None:
    """Extract a filename from a Content-Disposition header.

    Handles both `filename="x"` and the RFC 5987 `filename*=UTF-8''x` form,
    preferring the latter because it carries an explicit charset.
    """
    if not header:
        return None

    ext = re.search(
        r"filename\*\s*=\s*([\w\-]+)'([\w\-]*)'([^;]+)", header, re.IGNORECASE
    )
    if ext:
        charset, _lang, value = ext.groups()
        try:
            decoded = unquote(value.strip(), encoding=charset or "utf-8", errors="replace")
        except LookupError:
            decoded = unquote(value.strip())
        decoded = decoded.strip('"').strip()
        if decoded:
            return sanitize(Path(decoded).name)

    plain = re.search(r'filename\s*=\s*"([^"]+)"', header, re.IGNORECASE)
    if not plain:
        plain = re.search(r"filename\s*=\s*([^;]+)", header, re.IGNORECASE)
    if plain:
        value = plain.group(1).strip().strip('"').strip()
        if value:
            return sanitize(Path(value).name)
    return None


def from_url(url: str) -> str | None:
    path = urlsplit(url).path
    if not path:
        return None
    name = unquote(path.rstrip("/").rpartition("/")[2])
    if not name:
        return None
    return sanitize(name)


def variants(name: str, limit: int = 10000):
    """Yield `report.pdf`, `report (1).pdf`, `report (2).pdf`, ...

    The naming a download manager needs in two places: picking a free file
    name before writing, and picking a free one again at the finish line.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    yield name
    for i in range(1, limit):
        yield f"{stem} ({i}){suffix}"


def unique_path(path: Path) -> Path:
    """Return `path`, or `name (n).ext` if something is already there."""
    parent = path.parent
    for candidate in variants(path.name):
        target = parent / candidate
        if not target.exists():
            return target
    raise FileExistsError(f"cannot find a free name near {path}")
