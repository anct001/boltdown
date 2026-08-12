"""Deciding which pipeline a URL needs before anything is fetched."""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import unquote, urlsplit

#: Hosts we hand straight to yt-dlp: the URL is a *page*, not a file.
SITE_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv",
    "tiktok.com", "facebook.com", "fb.watch", "instagram.com", "twitter.com",
    "x.com", "bilibili.com", "soundcloud.com", "reddit.com", "nicovideo.jp",
    "ok.ru", "vk.com", "rumble.com", "odysee.com",
)

HLS_EXTENSIONS = (".m3u8", ".m3u")
DASH_EXTENSIONS = (".mpd",)

_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")

#: playlist file names that say nothing about the video behind them
GENERIC_STEMS = {
    "index", "master", "playlist", "manifest", "chunklist", "stream",
    "media", "video", "prog_index", "hls", "dash", "main", "out",
}


class MediaKind(str, Enum):
    DIRECT = "direct"   # ordinary file: the segmented engine handles it
    HLS = "hls"         # .m3u8 playlist
    DASH = "dash"       # .mpd manifest (extracted through yt-dlp)
    SITE = "site"       # a page yt-dlp knows how to read

    @property
    def is_media(self) -> bool:
        return self is not MediaKind.DIRECT


def url_path(url: str) -> str:
    return urlsplit(url).path.lower()


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_streaming_url(url: str) -> bool:
    path = url_path(url)
    return path.endswith(HLS_EXTENSIONS + DASH_EXTENSIONS)


def is_site_url(url: str) -> bool:
    """True for hosts whose links are pages we must ask yt-dlp about."""
    host = host_of(url)
    return any(host == site or host.endswith("." + site) for site in SITE_HOSTS)


def classify(url: str, *, content_type: str | None = None) -> MediaKind:
    """Pick a pipeline for `url`, optionally helped by a Content-Type."""
    path = url_path(url)
    if path.endswith(HLS_EXTENSIONS):
        return MediaKind.HLS
    if path.endswith(DASH_EXTENSIONS):
        return MediaKind.DASH
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        if ctype in ("application/vnd.apple.mpegurl", "application/x-mpegurl",
                     "audio/mpegurl", "audio/x-mpegurl"):
            return MediaKind.HLS
        if ctype == "application/dash+xml":
            return MediaKind.DASH
    if is_site_url(url) and not _FILE_EXT_RE.search(path):
        return MediaKind.SITE
    return MediaKind.DIRECT


def suggested_name(url: str, extension: str = "mp4") -> str:
    """A usable file name for a playlist URL.

    `.../videos/abc123/master.m3u8` should not become `master.mp4`, so walk
    back up the path until something that is not boilerplate turns up. The
    real title replaces this as soon as yt-dlp reports one.
    """
    parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    for part in reversed(parts):
        stem = part.rsplit(".", 1)[0] if "." in part else part
        if stem and stem.lower() not in GENERIC_STEMS:
            return f"{stem}.{extension}"
    host = host_of(url).split(".")[0]
    return f"{host or 'video'}.{extension}"
