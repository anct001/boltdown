"""Extension -> category mapping, mirroring IDM's default folders."""

from __future__ import annotations

from pathlib import Path

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Video": (
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
        "ts", "m2ts", "3gp", "ogv",
    ),
    "Music": ("mp3", "flac", "wav", "aac", "m4a", "ogg", "opus", "wma", "alac"),
    "Compressed": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "cab", "zst"),
    "Documents": (
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub", "mobi",
        "csv", "rtf", "odt",
    ),
    "Programs": ("exe", "msi", "msix", "apk", "deb", "rpm", "dmg", "appx"),
}

GENERAL = "General"

#: The mapping in force. Starts as the defaults above and can be replaced by
#: the user's own list - `set_categories` is the only way to change it, so
#: everything that classifies a file sees the same table.
_ACTIVE: dict[str, tuple[str, ...]] = dict(CATEGORIES)
_BY_EXT: dict[str, str] = {
    ext: name for name, exts in CATEGORIES.items() for ext in exts
}


def parse_categories(text: str | None) -> dict[str, tuple[str, ...]]:
    """Read the user's table: one `Name = ext, ext, ext` per line."""
    if not text:
        return {}
    parsed: dict[str, tuple[str, ...]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, extensions = line.partition("=")
        name = name.strip()
        exts = tuple(
            e.strip().lstrip(".").lower()
            for e in extensions.replace(";", ",").split(",")
            if e.strip()
        )
        if name and exts:
            parsed[name] = exts
    return parsed


def format_categories(mapping: dict[str, tuple[str, ...]] | None = None) -> str:
    mapping = mapping if mapping is not None else _ACTIVE
    return "\n".join(f"{name} = {', '.join(exts)}" for name, exts in mapping.items())


def set_categories(mapping: dict[str, tuple[str, ...]] | None) -> None:
    """Swap the table. `None` restores the built-in one."""
    global _ACTIVE, _BY_EXT
    _ACTIVE = dict(mapping) if mapping else dict(CATEGORIES)
    _BY_EXT = {ext: name for name, exts in _ACTIVE.items() for ext in exts}


def categories() -> dict[str, tuple[str, ...]]:
    return dict(_ACTIVE)


def category_for(filename: str) -> str:
    ext = Path(filename).suffix.lstrip(".").lower()
    return _BY_EXT.get(ext, GENERAL)


def target_dir(base: Path, filename: str, use_categories: bool = True) -> Path:
    if not use_categories:
        return Path(base)
    category = category_for(filename)
    if category == GENERAL:
        return Path(base)
    return Path(base) / category
