"""M3U8 (HLS) playlist parsing.

Pure text in, dataclasses out - no I/O lives here so every branch is cheap to
test. Two playlist shapes exist and a client cannot know which it fetched
until it has read the tags:

* **master** - a list of `#EXT-X-STREAM-INF` variants (bitrate ladder) plus
  `#EXT-X-MEDIA` renditions (separate audio/subtitle tracks);
* **media** - the actual `#EXTINF` segment list.

Only what the downloader needs is modelled. Unknown tags are ignored on
purpose: playlists in the wild carry plenty of them and refusing to parse
would break real streams.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

#: `KEY=VALUE` pairs, where VALUE may be quoted and contain commas.
_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)=("[^"]*"|[^,]*)')


def parse_attributes(value: str) -> dict[str, str]:
    """Parse an HLS attribute list, stripping quotes from quoted values."""
    return {
        key.upper(): raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        for key, raw in _ATTR_RE.findall(value)
    }


@dataclass(slots=True)
class Key:
    """An `#EXT-X-KEY` line. `method == "NONE"` means "stop decrypting"."""

    method: str
    uri: str | None = None
    iv: bytes | None = None
    key_format: str = "identity"

    @property
    def is_encrypted(self) -> bool:
        return self.method.upper() not in ("", "NONE")

    @property
    def is_supported(self) -> bool:
        """We implement plain AES-128; SAMPLE-AES and DRM need a player."""
        if not self.is_encrypted:
            return True
        return self.method.upper() == "AES-128" and self.key_format == "identity"


@dataclass(slots=True)
class MediaSegment:
    index: int
    url: str
    duration: float = 0.0
    key: Key | None = None
    #: media sequence number - the default IV when `#EXT-X-KEY` has none
    sequence: int = 0
    byterange: tuple[int, int] | None = None  # (offset, length)
    is_map: bool = False

    @property
    def iv(self) -> bytes:
        if self.key is not None and self.key.iv is not None:
            return self.key.iv
        return self.sequence.to_bytes(16, "big")


@dataclass(slots=True)
class Variant:
    url: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    resolution: tuple[int, int] | None = None
    codecs: str = ""
    audio_group: str | None = None

    @property
    def height(self) -> int | None:
        return self.resolution[1] if self.resolution else None

    @property
    def label(self) -> str:
        if self.resolution:
            return f"{self.resolution[1]}p"
        if self.bandwidth:
            return f"{self.bandwidth // 1000} kbps"
        return "unknown"


@dataclass(slots=True)
class Rendition:
    type: str
    group_id: str
    name: str = ""
    language: str = ""
    uri: str | None = None
    default: bool = False
    autoselect: bool = False


@dataclass(slots=True)
class MasterPlaylist:
    url: str
    variants: list[Variant] = field(default_factory=list)
    renditions: list[Rendition] = field(default_factory=list)

    is_master = True

    def audio_for(self, variant: Variant) -> Rendition | None:
        """The audio track a variant references, if audio is not muxed in."""
        if not variant.audio_group:
            return None
        candidates = [
            r
            for r in self.renditions
            if r.type == "AUDIO" and r.group_id == variant.audio_group and r.uri
        ]
        if not candidates:
            return None
        for rendition in candidates:
            if rendition.default:
                return rendition
        return candidates[0]


@dataclass(slots=True)
class MediaPlaylist:
    url: str
    segments: list[MediaSegment] = field(default_factory=list)
    target_duration: float = 0.0
    media_sequence: int = 0
    is_live: bool = False
    encrypted: bool = False

    is_master = False

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments if not s.is_map)

    @property
    def unsupported_key(self) -> Key | None:
        """First key we cannot handle, so the caller can fail with a reason."""
        for segment in self.segments:
            if segment.key is not None and not segment.key.is_supported:
                return segment.key
        return None


Playlist = MasterPlaylist | MediaPlaylist


def _parse_iv(raw: str) -> bytes | None:
    text = raw.strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    if not text:
        return None
    return bytes.fromhex(text.rjust(32, "0"))


def _parse_resolution(raw: str) -> tuple[int, int] | None:
    width, sep, height = raw.lower().partition("x")
    if not sep:
        return None
    try:
        return int(width), int(height)
    except ValueError:
        return None


def _parse_byterange(raw: str, previous_end: int) -> tuple[int, int]:
    length, sep, offset = raw.partition("@")
    size = int(length)
    start = int(offset) if sep and offset else previous_end
    return start, size


def parse_playlist(text: str, base_url: str = "") -> Playlist:
    """Parse `text` into a master or media playlist, resolving relative URIs."""
    lines = [line.strip() for line in text.splitlines()]
    if not any(line.startswith("#EXTM3U") for line in lines[:3]):
        raise ValueError("not an M3U8 playlist (missing #EXTM3U)")

    variants: list[Variant] = []
    renditions: list[Rendition] = []
    segments: list[MediaSegment] = []

    pending_stream: dict[str, str] | None = None
    duration = 0.0
    key: Key | None = None
    byterange: tuple[int, int] | None = None
    previous_end = 0
    target_duration = 0.0
    media_sequence = 0
    endlist = False
    encrypted = False
    index = 0
    sequence_offset = 0

    for line in lines:
        if not line:
            continue
        if line.startswith("#"):
            tag, _, value = line.partition(":")
            if tag == "#EXT-X-STREAM-INF":
                pending_stream = parse_attributes(value)
            elif tag == "#EXT-X-MEDIA":
                attrs = parse_attributes(value)
                uri = attrs.get("URI")
                renditions.append(
                    Rendition(
                        type=attrs.get("TYPE", "").upper(),
                        group_id=attrs.get("GROUP-ID", ""),
                        name=attrs.get("NAME", ""),
                        language=attrs.get("LANGUAGE", ""),
                        uri=urljoin(base_url, uri) if uri else None,
                        default=attrs.get("DEFAULT", "").upper() == "YES",
                        autoselect=attrs.get("AUTOSELECT", "").upper() == "YES",
                    )
                )
            elif tag == "#EXTINF":
                duration = float(value.split(",")[0] or 0)
            elif tag == "#EXT-X-KEY":
                attrs = parse_attributes(value)
                uri = attrs.get("URI")
                raw_iv = attrs.get("IV")
                key = Key(
                    method=attrs.get("METHOD", "NONE").upper(),
                    uri=urljoin(base_url, uri) if uri else None,
                    iv=_parse_iv(raw_iv) if raw_iv else None,
                    key_format=attrs.get("KEYFORMAT", "identity"),
                )
                if not key.is_encrypted:
                    key = None
                else:
                    encrypted = True
            elif tag == "#EXT-X-MAP":
                attrs = parse_attributes(value)
                uri = attrs.get("URI")
                if uri:
                    rng = attrs.get("BYTERANGE")
                    segments.append(
                        MediaSegment(
                            index=index,
                            url=urljoin(base_url, uri),
                            key=key,
                            sequence=media_sequence,
                            byterange=_parse_byterange(rng, 0) if rng else None,
                            is_map=True,
                        )
                    )
                    index += 1
            elif tag == "#EXT-X-BYTERANGE":
                byterange = _parse_byterange(value, previous_end)
            elif tag == "#EXT-X-TARGETDURATION":
                target_duration = float(value or 0)
            elif tag == "#EXT-X-MEDIA-SEQUENCE":
                media_sequence = int(value or 0)
            elif tag == "#EXT-X-ENDLIST":
                endlist = True
            continue

        url = urljoin(base_url, line)
        if pending_stream is not None:
            attrs = pending_stream
            pending_stream = None
            variants.append(
                Variant(
                    url=url,
                    bandwidth=int(attrs.get("BANDWIDTH") or 0),
                    average_bandwidth=int(attrs.get("AVERAGE-BANDWIDTH") or 0),
                    resolution=_parse_resolution(attrs.get("RESOLUTION", "")),
                    codecs=attrs.get("CODECS", ""),
                    audio_group=attrs.get("AUDIO") or None,
                )
            )
            continue

        segments.append(
            MediaSegment(
                index=index,
                url=url,
                duration=duration,
                key=key,
                sequence=media_sequence + sequence_offset,
                byterange=byterange,
            )
        )
        sequence_offset += 1
        if byterange is not None:
            previous_end = byterange[0] + byterange[1]
            byterange = None
        duration = 0.0
        index += 1

    if variants:
        return MasterPlaylist(url=base_url, variants=variants, renditions=renditions)
    return MediaPlaylist(
        url=base_url,
        segments=segments,
        target_duration=target_duration,
        media_sequence=media_sequence,
        is_live=not endlist,
        encrypted=encrypted,
    )


def pick_variant(
    master: MasterPlaylist, max_height: int | None = None
) -> Variant | None:
    """Best variant at or below `max_height` (best overall when unset).

    Bandwidth breaks ties because two 1080p renditions can differ wildly in
    quality, and it is the only signal audio-only variants carry.
    """
    if not master.variants:
        return None
    ranked = sorted(
        master.variants,
        key=lambda v: (v.height or 0, v.average_bandwidth or v.bandwidth),
    )
    if max_height is not None:
        allowed = [v for v in ranked if (v.height or 0) <= max_height]
        if allowed:
            return allowed[-1]
        # Every rendition is bigger than asked for: take the smallest one.
        return ranked[0]
    return ranked[-1]
