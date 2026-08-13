"""End to end HLS downloads against the local test server."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from app.core.errors import CancelledByUser
from app.core.http_client import RequestSpec, build_client
from app.media import ffmpeg as ffmpeg_mod
from app.media import hls
from app.media.hls import HlsDownloader, UnsupportedStream, decrypt_aes128

from .conftest import make_payload

SEGMENT_SIZE = 96 * 1024


def encrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def publish_segments(
    server, prefix: str, count: int, *, size: int = SEGMENT_SIZE, seed: int = 7
) -> list[bytes]:
    chunks = [make_payload(size, seed=seed + i) for i in range(count)]
    for i, chunk in enumerate(chunks):
        server.add_file(f"{prefix}/seg{i}.ts", chunk)
    return chunks


def publish_playlist(server, prefix: str, lines: list[str]) -> str:
    text = "\n".join(["#EXTM3U", "#EXT-X-TARGETDURATION:4", *lines, "#EXT-X-ENDLIST"])
    return server.add_file(f"{prefix}/index.m3u8", text.encode("utf-8"))


def simple_playlist(server, prefix: str, count: int, query: str = "") -> str:
    lines = []
    for i in range(count):
        lines += ["#EXTINF:4.0,", f"seg{i}.ts{query}"]
    return publish_playlist(server, prefix, lines)


def make_downloader(url: str, tmp_path: Path, client: httpx.AsyncClient, **kwargs):
    return HlsDownloader(
        client=client,
        url=url,
        output=tmp_path / "video.mp4",
        work_dir=tmp_path / "work",
        connections=kwargs.pop("connections", 4),
        **kwargs,
    )


async def client_for(url: str) -> httpx.AsyncClient:
    return build_client(RequestSpec(url=url))


@pytest.fixture
def no_ffmpeg(monkeypatch):
    """Force the "ffmpeg is not installed" path: the raw stream is kept."""
    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(hls.ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)


async def test_downloads_every_segment_in_order(server, tmp_path, no_ffmpeg):
    chunks = publish_segments(server, "plain", 6)
    url = simple_playlist(server, "plain", 6)

    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        output = await downloader.run()

    assert output.name == "video.ts"  # no ffmpeg: kept as raw MPEG-TS
    assert output.read_bytes() == b"".join(chunks)
    assert downloader.status.completed_segments == 6
    assert downloader.status.downloaded == 6 * SEGMENT_SIZE
    assert downloader.status.estimated_size == 6 * SEGMENT_SIZE


async def test_master_playlist_picks_the_capped_variant(server, tmp_path, no_ffmpeg):
    for name, height in (("low", 360), ("high", 1080)):
        publish_segments(server, f"multi/{name}", 2, seed=height)
        simple_playlist(server, f"multi/{name}", 2)
    master = server.add_file(
        "multi/master.m3u8",
        (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360\n"
            "low/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
            "high/index.m3u8\n"
        ).encode("utf-8"),
    )

    async with await client_for(master) as client:
        downloader = make_downloader(master, tmp_path, client, max_height=720)
        output = await downloader.run()

    expected = b"".join(make_payload(SEGMENT_SIZE, seed=360 + i) for i in range(2))
    assert output.read_bytes() == expected
    assert downloader.master is not None


async def test_aes128_segments_are_decrypted(server, tmp_path, no_ffmpeg):
    key = bytes(range(16))
    iv = bytes(range(16, 32))
    plain = [make_payload(40 * 1024, seed=100 + i) for i in range(3)]
    for i, chunk in enumerate(plain):
        server.add_file(f"enc/seg{i}.ts", encrypt_aes128(chunk, key, iv))
    server.add_file("enc/secret.key", key)
    lines = [f'#EXT-X-KEY:METHOD=AES-128,URI="secret.key",IV=0x{iv.hex()}']
    for i in range(3):
        lines += ["#EXTINF:4.0,", f"seg{i}.ts"]
    url = publish_playlist(server, "enc", lines)

    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        output = await downloader.run()

    assert output.read_bytes() == b"".join(plain)


def test_decrypt_keeps_bytes_that_only_look_like_padding():
    key, iv = bytes(16), bytes(16)
    # Length is already a multiple of 16 and the tail is not valid padding.
    data = bytes([9]) * 32
    assert decrypt_aes128(encrypt_aes128(data, key, iv), key, iv) == data


async def test_unsupported_encryption_fails_with_a_reason(server, tmp_path, no_ffmpeg):
    publish_segments(server, "drm", 1)
    lines = ['#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k.key"', "#EXTINF:4.0,", "seg0.ts"]
    url = publish_playlist(server, "drm", lines)

    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        with pytest.raises(UnsupportedStream, match="SAMPLE-AES"):
            await downloader.run()


async def test_transient_segment_failures_are_retried(server, tmp_path, no_ffmpeg):
    chunks = publish_segments(server, "flaky", 3, size=32 * 1024, seed=55)
    lines = []
    for i in range(3):
        query = "?fail=1" if i == 1 else ""
        lines += ["#EXTINF:4.0,", f"seg{i}.ts{query}"]
    url = publish_playlist(server, "flaky", lines)

    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        output = await downloader.run()

    assert output.read_bytes() == b"".join(chunks)


async def test_stopping_keeps_finished_segments_and_resumes(server, tmp_path, no_ffmpeg):
    chunks = publish_segments(server, "resume", 8, size=48 * 1024, seed=200)
    url = simple_playlist(server, "resume", 8)
    stop = asyncio.Event()
    seen = 0

    def on_bytes(_n: int) -> None:
        nonlocal seen
        seen += 1
        if seen >= 2:
            stop.set()

    async with await client_for(url) as client:
        first = make_downloader(
            url, tmp_path, client, connections=1, stop_event=stop, on_bytes=on_bytes
        )
        with pytest.raises(CancelledByUser):
            await first.run()

    parts = sorted((tmp_path / "work").glob("*.bin"))
    assert 0 < len(parts) < 8
    assert not list((tmp_path / "work").glob("*.part"))  # nothing half written

    async with await client_for(url) as client:
        second = make_downloader(url, tmp_path, client)
        output = await second.run()

    assert output.read_bytes() == b"".join(chunks)
    # The resumed run counted the segments it found on disk.
    assert second.status.completed_segments == 8
    assert second.status.downloaded == 8 * 48 * 1024


async def test_empty_playlist_is_rejected(server, tmp_path, no_ffmpeg):
    url = publish_playlist(server, "empty", [])
    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        with pytest.raises(UnsupportedStream, match="no segments"):
            await downloader.run()


async def test_missing_playlist_reports_not_found(server, tmp_path, no_ffmpeg):
    url = server.url_for("nope/index.m3u8")
    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        with pytest.raises(Exception) as excinfo:
            await downloader.run()
    assert "404" in str(excinfo.value)


# --------------------------------------------------------------- with ffmpeg

ffmpeg_binary = ffmpeg_mod.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(ffmpeg_binary is None, reason="ffmpeg not installed")


def make_real_ts(path: Path) -> bytes:
    """A genuine MPEG-TS, so the remux step gets something it can parse."""
    subprocess.run(
        [
            str(ffmpeg_binary), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-f", "mpegts", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


@needs_ffmpeg
async def test_real_stream_is_remuxed_into_mp4(server, tmp_path):
    source = make_real_ts(tmp_path / "source.ts")
    # TS packets are 188 bytes; cutting on that boundary keeps every chunk valid.
    step = (len(source) // 4 // 188) * 188
    chunks = [source[i : i + step] for i in range(0, len(source), step)]
    for i, chunk in enumerate(chunks):
        server.add_file(f"real/seg{i}.ts", chunk)
    url = simple_playlist(server, "real", len(chunks))

    async with await client_for(url) as client:
        downloader = make_downloader(url, tmp_path, client)
        output = await downloader.run()

    assert output.suffix == ".mp4"
    assert output.stat().st_size > 0
    assert not (tmp_path / "work" / "stream.raw").exists()
    downloader.cleanup()
    assert not (tmp_path / "work").exists()


@needs_ffmpeg
async def test_ffmpeg_version_is_readable():
    assert "ffmpeg" in (await ffmpeg_mod.version()).lower()


@needs_ffmpeg
async def test_merge_tracks_produces_one_file(tmp_path):
    video = tmp_path / "v.mp4"
    audio = tmp_path / "a.m4a"
    subprocess.run(
        [str(ffmpeg_binary), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", str(video)],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(ffmpeg_binary), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "aac", str(audio)],
        check=True, capture_output=True,
    )

    output = tmp_path / "merged.mp4"
    await ffmpeg_mod.merge_tracks(video, audio, output)
    assert output.stat().st_size > video.stat().st_size


def test_missing_ffmpeg_raises_a_helpful_error(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)
    with pytest.raises(ffmpeg_mod.FfmpegMissing, match="BOLTDOWN_FFMPEG"):
        ffmpeg_mod.require_ffmpeg()
