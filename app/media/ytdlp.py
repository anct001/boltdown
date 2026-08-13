"""yt-dlp used as an *extractor only*.

yt-dlp knows how to turn a page URL into direct media URLs for a thousand
sites; it is not, however, a fast downloader - it fetches with a single
connection. So we ask it what to download and then hand the URLs to our own
segmented engine, which is the whole point of this application.

The import is deliberately lazy: yt-dlp costs about a second to import and
most downloads never touch it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import FatalError
from ..util.log import get_logger

log = get_logger(__name__)

#: protocols we can drive ourselves; anything else has to be left alone
DIRECT_PROTOCOLS = ("https", "http")
HLS_PROTOCOLS = ("m3u8", "m3u8_native")


class ExtractionError(FatalError):
    """yt-dlp could not read the page (site changed, geo block, private...)."""


class YtDlpMissing(FatalError):
    def __init__(self) -> None:
        super().__init__(
            "yt-dlp is not installed - run: pip install yt-dlp"
        )


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def version() -> str | None:
    try:
        import yt_dlp
    except ImportError:
        return None
    return getattr(yt_dlp.version, "__version__", None)


@dataclass(slots=True)
class Track:
    """One downloadable stream out of a format list."""

    url: str
    format_id: str = ""
    ext: str = "mp4"
    protocol: str = "https"
    height: int | None = None
    width: int | None = None
    tbr: float | None = None
    abr: float | None = None
    filesize: int | None = None
    vcodec: str = "none"
    acodec: str = "none"
    fps: float | None = None
    note: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec) and self.vcodec != "none"

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec) and self.acodec != "none"

    @property
    def is_hls(self) -> bool:
        return self.protocol in HLS_PROTOCOLS

    @property
    def is_usable(self) -> bool:
        return self.protocol in DIRECT_PROTOCOLS + HLS_PROTOCOLS

    @property
    def label(self) -> str:
        if self.height:
            return f"{self.height}p" + (f"{self.fps:.0f}" if self.fps and self.fps > 30 else "")
        if self.has_audio and not self.has_video:
            return f"{int(self.abr or self.tbr or 0)} kbps audio"
        return self.format_id or self.ext


@dataclass(slots=True)
class MediaInfo:
    title: str
    webpage_url: str
    duration: float | None = None
    ext: str = "mp4"
    tracks: list[Track] = field(default_factory=list)
    thumbnail: str | None = None

    def videos(self) -> list[Track]:
        return [t for t in self.tracks if t.has_video and t.is_usable]

    def audios(self) -> list[Track]:
        return [t for t in self.tracks if t.has_audio and not t.has_video and t.is_usable]


@dataclass(slots=True)
class PlaylistEntry:
    """One video inside a playlist, before anything is extracted in full."""

    url: str
    title: str
    index: int = 0
    duration: float | None = None
    uploader: str = ""

    @property
    def label(self) -> str:
        if self.duration:
            minutes, seconds = divmod(int(self.duration), 60)
            return f"{self.title}  ({minutes}:{seconds:02d})"
        return self.title


@dataclass(slots=True)
class Playlist:
    title: str
    url: str
    entries: list[PlaylistEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(slots=True)
class DownloadPlan:
    """What the media runner should actually fetch."""

    title: str
    video: Track | None
    audio: Track | None = None
    container: str = "mp4"

    @property
    def needs_merge(self) -> bool:
        return self.video is not None and self.audio is not None

    @property
    def tracks(self) -> list[Track]:
        return [t for t in (self.video, self.audio) if t is not None]


def _track_from(fmt: dict[str, Any]) -> Track | None:
    url = fmt.get("url")
    if not url:
        return None
    return Track(
        url=url,
        format_id=str(fmt.get("format_id") or ""),
        ext=str(fmt.get("ext") or "mp4"),
        protocol=str(fmt.get("protocol") or "https"),
        height=fmt.get("height"),
        width=fmt.get("width"),
        tbr=fmt.get("tbr"),
        abr=fmt.get("abr"),
        filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
        vcodec=str(fmt.get("vcodec") or "none"),
        acodec=str(fmt.get("acodec") or "none"),
        fps=fmt.get("fps"),
        note=str(fmt.get("format_note") or ""),
        headers=dict(fmt.get("http_headers") or {}),
    )


def playlist_from_dict(data: dict[str, Any]) -> Playlist | None:
    """A playlist listing, or None when the URL was a single video."""
    if data.get("_type") != "playlist":
        return None
    entries = []
    for position, entry in enumerate(data.get("entries") or [], start=1):
        if not entry:
            continue
        url = entry.get("url") or entry.get("webpage_url") or entry.get("original_url")
        if not url:
            continue
        entries.append(PlaylistEntry(
            url=url,
            title=str(entry.get("title") or f"#{position}"),
            index=position,
            duration=entry.get("duration"),
            uploader=str(entry.get("uploader") or ""),
        ))
    return Playlist(
        title=str(data.get("title") or "playlist"),
        url=str(data.get("webpage_url") or data.get("original_url") or ""),
        entries=entries,
    )


def extract_playlist_sync(url: str, options: dict[str, Any] | None = None) -> Playlist | None:
    """List a playlist without resolving every video's formats.

    `extract_flat` is the difference between one request and one request *per
    video* - a 200-video channel would otherwise take minutes before the user
    sees anything to pick from.
    """
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError as YtDownloadError
    except ImportError as exc:
        raise YtDlpMissing() from exc

    opts = dict(options or build_options())
    opts.update({"noplaylist": False, "extract_flat": "in_playlist", "skip_download": True})
    try:
        with YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except YtDownloadError as exc:
        raise ExtractionError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - extractors raise anything
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc
    if not data:
        raise ExtractionError("yt-dlp returned no information")
    return playlist_from_dict(data)


async def extract_playlist(url: str, options: dict[str, Any] | None = None) -> Playlist | None:
    return await asyncio.to_thread(extract_playlist_sync, url, options)


def info_from_dict(data: dict[str, Any]) -> MediaInfo:
    """Convert a yt-dlp info dict into our own model."""
    if data.get("_type") == "playlist":
        entries = [e for e in (data.get("entries") or []) if e]
        if not entries:
            raise ExtractionError("playlist is empty")
        log.info("playlist detected, taking the first entry of %d", len(entries))
        data = entries[0]

    formats = data.get("formats")
    if not formats:
        # Some extractors return a single ready-made URL instead of a list.
        formats = [data] if data.get("url") else []
    tracks = [t for t in (_track_from(f) for f in formats) if t is not None]
    if not tracks:
        raise ExtractionError("no downloadable formats were found")

    return MediaInfo(
        title=str(data.get("title") or "video"),
        webpage_url=str(data.get("webpage_url") or data.get("original_url") or ""),
        duration=data.get("duration"),
        ext=str(data.get("ext") or "mp4"),
        tracks=tracks,
        thumbnail=data.get("thumbnail"),
    )


def build_options(
    *,
    proxy: str | None = None,
    cookie: str | None = None,
    referer: str | None = None,
    user_agent: str | None = None,
    verify_tls: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    if user_agent:
        headers["User-Agent"] = user_agent
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "nocheckcertificate": not verify_tls,
        "extract_flat": False,
        "socket_timeout": 20,
    }
    if proxy:
        options["proxy"] = proxy
    if headers:
        options["http_headers"] = headers
    if extra:
        options.update(extra)
    return options


def extract_sync(url: str, options: dict[str, Any] | None = None) -> MediaInfo:
    """Blocking extraction - call it through `extract` from async code."""
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError as YtDownloadError
    except ImportError as exc:
        raise YtDlpMissing() from exc

    opts = options if options is not None else build_options()
    try:
        with YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except YtDownloadError as exc:
        raise ExtractionError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - extractors raise anything
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc
    if not data:
        raise ExtractionError("yt-dlp returned no information")
    return info_from_dict(data)


async def extract(url: str, options: dict[str, Any] | None = None) -> MediaInfo:
    """Read `url` in a worker thread so the event loop keeps running."""
    return await asyncio.to_thread(extract_sync, url, options)


def _score(track: Track) -> tuple:
    return (track.height or 0, track.tbr or 0, track.filesize or 0)


def select(
    info: MediaInfo, *, max_height: int | None = None, audio_only: bool = False
) -> DownloadPlan:
    """Choose the tracks to download.

    Sites like YouTube only offer low resolutions with audio muxed in; the
    good renditions are video-only and need a separate audio track. So we
    compare the best muxed stream against the best video+audio pair and take
    whichever is actually higher quality.
    """
    audios = sorted(info.audios(), key=lambda t: (t.abr or t.tbr or 0, t.filesize or 0))
    best_audio = audios[-1] if audios else None

    if audio_only:
        if best_audio is None:
            raise ExtractionError("this media has no separate audio track")
        return DownloadPlan(title=info.title, video=None, audio=best_audio,
                            container=best_audio.ext or "m4a")

    videos = info.videos()
    if max_height is not None:
        limited = [t for t in videos if (t.height or 0) <= max_height]
        videos = limited or sorted(videos, key=_score)[:1]
    if not videos:
        if best_audio is not None:
            return DownloadPlan(title=info.title, video=None, audio=best_audio,
                                container=best_audio.ext or "m4a")
        raise ExtractionError("no usable video format")

    muxed = sorted([t for t in videos if t.has_audio], key=_score)
    video_only = sorted([t for t in videos if not t.has_audio], key=_score)

    best_muxed = muxed[-1] if muxed else None
    best_video = video_only[-1] if video_only else None

    if best_video is not None and best_audio is not None:
        if best_muxed is None or _score(best_video) > _score(best_muxed):
            container = "mp4" if best_video.ext in ("mp4", "m4v") else "mkv"
            return DownloadPlan(
                title=info.title, video=best_video, audio=best_audio, container=container
            )
    if best_muxed is not None:
        return DownloadPlan(title=info.title, video=best_muxed, audio=None,
                            container=best_muxed.ext or "mp4")
    if best_video is not None:
        # Video with no audio anywhere: still worth downloading.
        return DownloadPlan(title=info.title, video=best_video, audio=None,
                            container=best_video.ext or "mp4")
    raise ExtractionError("no usable format combination")


def format_table(info: MediaInfo) -> list[str]:
    """Human readable format list for `--list-formats`."""
    rows = [f"{'ID':<14} {'EXT':<5} {'RES':<10} {'CODECS':<22} {'SIZE':>10}  NOTE"]
    for track in sorted(info.tracks, key=_score):
        res = f"{track.width}x{track.height}" if track.height else (
            "audio only" if track.has_audio else "?"
        )
        codecs = f"{track.vcodec.split('.')[0]}+{track.acodec.split('.')[0]}"
        size = f"{track.filesize/1048576:.1f}M" if track.filesize else "-"
        rows.append(
            f"{track.format_id:<14} {track.ext:<5} {res:<10} {codecs:<22} "
            f"{size:>10}  {track.note}"
        )
    return rows
