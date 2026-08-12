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

_BY_EXT: dict[str, str] = {
    ext: name for name, exts in CATEGORIES.items() for ext in exts
}

GENERAL = "General"


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
