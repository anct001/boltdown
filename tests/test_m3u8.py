"""Playlist parsing and pipeline detection."""

from __future__ import annotations

import pytest

from app.media.detect import MediaKind, classify, is_site_url
from app.media.m3u8 import (
    MasterPlaylist,
    MediaPlaylist,
    parse_attributes,
    parse_playlist,
    pick_variant,
)

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English, US",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="French",URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.42c01e,mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.4d401f",AUDIO="aud"
mid/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028",AUDIO="aud"
high/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:7
#EXTINF:9.009,
seg0.ts
#EXTINF:9.009,
seg1.ts
#EXTINF:3.003,
http://cdn.example.net/seg2.ts
#EXT-X-ENDLIST
"""

ENCRYPTED = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="secret.key",IV=0x000102030405060708090a0b0c0d0e0f
#EXTINF:4.0,
a.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:4.0,
b.ts
#EXT-X-ENDLIST
"""

FMP4 = """#EXTM3U
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
#EXT-X-BYTERANGE:1000@2000
chunk.m4s
#EXTINF:4.0,
#EXT-X-BYTERANGE:500
chunk.m4s
#EXT-X-ENDLIST
"""


def test_attributes_keep_commas_inside_quotes():
    attrs = parse_attributes('BANDWIDTH=800000,CODECS="avc1.42c01e,mp4a.40.2",NAME="A,B"')
    assert attrs["BANDWIDTH"] == "800000"
    assert attrs["CODECS"] == "avc1.42c01e,mp4a.40.2"
    assert attrs["NAME"] == "A,B"


def test_master_playlist_variants_and_audio_groups():
    playlist = parse_playlist(MASTER, "https://cdn.example.com/v/master.m3u8")
    assert isinstance(playlist, MasterPlaylist)
    assert [v.height for v in playlist.variants] == [360, 720, 1080]
    assert playlist.variants[0].url == "https://cdn.example.com/v/low/index.m3u8"

    best = pick_variant(playlist)
    assert best is not None and best.height == 1080
    audio = playlist.audio_for(best)
    assert audio is not None
    assert audio.name == "English, US"  # DEFAULT=YES wins over playlist order
    assert audio.uri == "https://cdn.example.com/v/audio/en.m3u8"


@pytest.mark.parametrize(
    "cap, expected",
    [(None, 1080), (1080, 1080), (720, 720), (900, 720), (240, 360)],
)
def test_pick_variant_respects_the_height_cap(cap, expected):
    playlist = parse_playlist(MASTER, "https://cdn.example.com/v/master.m3u8")
    variant = pick_variant(playlist, cap)
    assert variant is not None and variant.height == expected


def test_media_playlist_segments_and_sequence():
    playlist = parse_playlist(MEDIA, "https://cdn.example.com/v/index.m3u8")
    assert isinstance(playlist, MediaPlaylist)
    assert not playlist.is_live
    assert playlist.target_duration == 10
    assert [s.sequence for s in playlist.segments] == [7, 8, 9]
    assert playlist.segments[0].url == "https://cdn.example.com/v/seg0.ts"
    assert playlist.segments[2].url == "http://cdn.example.net/seg2.ts"
    assert round(playlist.duration, 3) == 21.021


def test_live_playlist_has_no_endlist():
    playlist = parse_playlist(MEDIA.replace("#EXT-X-ENDLIST\n", ""), "https://x/i.m3u8")
    assert playlist.is_live


def test_key_applies_until_it_is_replaced():
    playlist = parse_playlist(ENCRYPTED, "https://cdn.example.com/v/index.m3u8")
    first, second = playlist.segments
    assert playlist.encrypted
    assert first.key is not None and first.key.method == "AES-128"
    assert first.key.uri == "https://cdn.example.com/v/secret.key"
    assert first.iv == bytes(range(16))
    assert second.key is None  # METHOD=NONE turns decryption back off
    assert playlist.unsupported_key is None


def test_sample_aes_is_reported_as_unsupported():
    text = ENCRYPTED.replace("METHOD=AES-128", "METHOD=SAMPLE-AES")
    playlist = parse_playlist(text, "https://cdn.example.com/v/index.m3u8")
    bad = playlist.unsupported_key
    assert bad is not None and bad.method == "SAMPLE-AES"


def test_default_iv_comes_from_the_media_sequence():
    playlist = parse_playlist(
        ENCRYPTED.replace(",IV=0x000102030405060708090a0b0c0d0e0f", ""),
        "https://cdn.example.com/v/index.m3u8",
    )
    assert playlist.segments[0].iv == (0).to_bytes(16, "big")


def test_fmp4_map_and_byteranges():
    playlist = parse_playlist(FMP4, "https://cdn.example.com/v/index.m3u8")
    init, first, second = playlist.segments
    assert init.is_map and init.url.endswith("init.mp4")
    assert first.byterange == (2000, 1000)
    # A bare length continues where the previous range ended.
    assert second.byterange == (3000, 500)


def test_rejects_text_that_is_not_a_playlist():
    with pytest.raises(ValueError):
        parse_playlist("<html>404</html>", "https://x/y.m3u8")


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://x.com/a/file.zip", MediaKind.DIRECT),
        ("https://cdn.x.com/v/master.m3u8?token=1", MediaKind.HLS),
        ("https://cdn.x.com/v/manifest.mpd", MediaKind.DASH),
        ("https://www.youtube.com/watch?v=abc", MediaKind.SITE),
        ("https://youtu.be/abc123", MediaKind.SITE),
        ("https://example.com/video.mp4", MediaKind.DIRECT),
    ],
)
def test_classify(url, kind):
    assert classify(url) is kind


def test_content_type_can_reveal_a_playlist():
    assert classify(
        "https://cdn.example.com/stream", content_type="application/x-mpegURL; charset=utf-8"
    ) is MediaKind.HLS


def test_site_detection_is_host_based():
    assert is_site_url("https://m.youtube.com/watch?v=x")
    assert not is_site_url("https://youtube.com.evil.example/watch?v=x")
