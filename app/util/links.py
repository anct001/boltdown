"""Which addresses are downloads: the web, FTP, and torrents' magnet links."""

from __future__ import annotations

from urllib.parse import urlsplit

WEB = ("http://", "https://")
FTP = ("ftp://", "ftps://")
MAGNET = "magnet:?"


def is_download_link(text: object) -> bool:
    """An address Boltdown can fetch - not just anything with a colon."""
    if not isinstance(text, str):
        return False
    value = text.strip()
    lowered = value.lower()
    if lowered.startswith(MAGNET):
        return "xt=urn:" in lowered
    if lowered.startswith(WEB + FTP):
        return bool(urlsplit(value).netloc)
    return False


def is_magnet(text: str) -> bool:
    return (text or "").strip().lower().startswith(MAGNET)
