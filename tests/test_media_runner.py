"""Format selection, and the media runner behind the ordinary engine API."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.core.engine import Engine
from app.core.task import DownloadRequest, TaskState
from app.media import ffmpeg as ffmpeg_mod
from app.media import hls, ytdlp
from app.media.detect import MediaKind
from app.media.runner import MediaTaskRunner
from app.media.ytdlp import ExtractionError, MediaInfo, Track, info_from_dict, select

from .conftest import make_payload
from .test_hls import needs_ffmpeg, simple_playlist, publish_segments

SEGMENT_SIZE = 96 * 1024


@pytest.fixture
def no_ffmpeg(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(hls.ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)


# ------------------------------------------------------------ format picking

YT_INFO = {
    "title": "Bản tin 18:00",
    "webpage_url": "https://www.youtube.com/watch?v=abc",
    "duration": 61.0,
    "ext": "mp4",
    "formats": [
        {"format_id": "18", "url": "https://x/muxed360.mp4", "ext": "mp4",
         "protocol": "https", "height": 360, "vcodec": "avc1.42001E",
         "acodec": "mp4a.40.2", "filesize": 5_000_000},
        {"format_id": "137", "url": "https://x/video1080.mp4", "ext": "mp4",
         "protocol": "https", "height": 1080, "vcodec": "avc1.640028",
         "acodec": "none", "filesize": 40_000_000, "fps": 30},
        {"format_id": "136", "url": "https://x/video720.mp4", "ext": "mp4",
         "protocol": "https", "height": 720, "vcodec": "avc1.4d401f",
         "acodec": "none", "filesize": 20_000_000},
        {"format_id": "140", "url": "https://x/audio.m4a", "ext": "m4a",
         "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 128.0, "filesize": 1_000_000},
        {"format_id": "dash-seg", "url": "https://x/frag", "ext": "mp4",
         "protocol": "http_dash_segments", "height": 2160, "vcodec": "vp9",
         "acodec": "none"},
    ],
}


def test_info_from_dict_keeps_only_what_we_can_fetch():
    info = info_from_dict(YT_INFO)
    assert info.title == "Bản tin 18:00"
    assert len(info.tracks) == 5
    # The fragmented DASH format is parsed but never offered as usable.
    assert [t.format_id for t in info.videos()] == ["18", "137", "136"]
    assert [t.format_id for t in info.audios()] == ["140"]


def test_select_prefers_video_plus_audio_over_the_muxed_stream():
    plan = select(info_from_dict(YT_INFO))
    assert plan.video is not None and plan.video.format_id == "137"
    assert plan.audio is not None and plan.audio.format_id == "140"
    assert plan.needs_merge and plan.container == "mp4"


def test_select_honours_a_height_cap():
    plan = select(info_from_dict(YT_INFO), max_height=720)
    assert plan.video is not None and plan.video.format_id == "136"


def test_without_an_audio_track_a_lower_muxed_stream_wins():
    """A 360p clip with sound beats a silent 1080p one."""
    data = {**YT_INFO, "formats": [f for f in YT_INFO["formats"] if f["format_id"] != "140"]}
    plan = select(info_from_dict(data))
    assert plan.video is not None and plan.video.format_id == "18"
    assert plan.audio is None and not plan.needs_merge


def test_select_audio_only():
    plan = select(info_from_dict(YT_INFO), audio_only=True)
    assert plan.video is None
    assert plan.audio is not None and plan.audio.format_id == "140"
    assert plan.container == "m4a"


def test_a_cap_below_every_rendition_keeps_the_smallest():
    plan = select(info_from_dict(YT_INFO), max_height=144)
    assert plan.video is not None and plan.video.height == 360


def test_playlists_use_their_first_entry():
    info = info_from_dict({"_type": "playlist", "entries": [YT_INFO]})
    assert info.title == "Bản tin 18:00"


def test_a_page_without_formats_is_an_error():
    with pytest.raises(ExtractionError):
        info_from_dict({"title": "x", "formats": []})


def test_format_table_lists_every_format():
    rows = ytdlp.format_table(info_from_dict(YT_INFO))
    assert rows[0].startswith("ID")
    assert any("140" in row and "audio only" in row for row in rows[1:])


def test_hls_formats_are_marked_for_the_playlist_pipeline():
    info = info_from_dict(
        {"title": "live", "formats": [
            {"format_id": "hls-1", "url": "https://x/master.m3u8", "ext": "mp4",
             "protocol": "m3u8_native", "height": 720, "vcodec": "avc1", "acodec": "mp4a"},
        ]}
    )
    plan = select(info)
    assert plan.video is not None and plan.video.is_hls


@pytest.mark.skipif(not ytdlp.available(), reason="yt-dlp not installed")
def test_yt_dlp_options_carry_the_browser_headers():
    options = ytdlp.build_options(cookie="a=b", referer="https://r/", proxy="http://p:1")
    assert options["http_headers"]["Cookie"] == "a=b"
    assert options["http_headers"]["Referer"] == "https://r/"
    assert options["proxy"] == "http://p:1"
    assert options["skip_download"] is True


# --------------------------------------------------------------- media tasks


def hls_request(url: str, save_dir: Path, **kwargs) -> DownloadRequest:
    kwargs.setdefault("connections", 4)
    return DownloadRequest(url=url, save_dir=save_dir, **kwargs)


def test_a_playlist_url_is_routed_to_the_media_pipeline(tmp_path):
    request = DownloadRequest(url="https://x/v/master.m3u8", save_dir=tmp_path)
    assert request.media_kind is MediaKind.HLS and request.is_media
    plain = DownloadRequest(url="https://x/v/file.zip", save_dir=tmp_path)
    assert plain.media_kind is MediaKind.DIRECT and not plain.is_media


async def test_media_runner_downloads_a_playlist(server, tmp_path, no_ffmpeg):
    chunks = publish_segments(server, "runner", 5, seed=300)
    url = simple_playlist(server, "runner", 5)
    events: list[str] = []

    runner = MediaTaskRunner(
        1, hls_request(url, tmp_path), on_event=lambda name, _s: events.append(name)
    )
    state = await runner.run()

    assert state is TaskState.COMPLETED
    assert runner.dest_path is not None
    assert runner.dest_path.read_bytes() == b"".join(chunks)
    # `.../runner/index.m3u8` is named after the folder, not the playlist file.
    assert runner.filename == "runner.ts"
    assert runner.snapshot().percent == 100.0
    assert "probing" in events and "completed" in events
    # The scratch directory is gone once the file is in place.
    assert not list(tmp_path.glob("*.idmedia"))


async def test_pausing_a_media_task_keeps_the_segments(server, tmp_path, no_ffmpeg):
    chunks = publish_segments(server, "pause", 10, size=48 * 1024, seed=400)
    url = simple_playlist(server, "pause", 10, query="?slow=20")
    request = hls_request(url, tmp_path, connections=2)

    runner = MediaTaskRunner(1, request)
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        await asyncio.sleep(0.05)
        if runner.downloaded > 0:
            break
    runner.request_pause()
    assert await task is TaskState.PAUSED
    work = tmp_path / "pause.idmedia"
    assert list((work / "hls").glob("*.bin"))

    resumed = MediaTaskRunner(2, hls_request(url, tmp_path, connections=4))
    assert await resumed.run() is TaskState.COMPLETED
    assert resumed.dest_path is not None
    assert resumed.dest_path.read_bytes() == b"".join(chunks)


async def test_cancelling_a_media_task_removes_the_work_directory(server, tmp_path, no_ffmpeg):
    publish_segments(server, "cancel", 10, size=48 * 1024, seed=500)
    url = simple_playlist(server, "cancel", 10, query="?slow=20")

    runner = MediaTaskRunner(1, hls_request(url, tmp_path, connections=2))
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        await asyncio.sleep(0.05)
        if runner.downloaded > 0:
            break
    runner.request_cancel()
    assert await task is TaskState.CANCELLED
    assert not (tmp_path / "cancel.idmedia").exists()


async def test_extracted_page_downloads_through_the_segmented_engine(
    server, tmp_path, monkeypatch, no_ffmpeg
):
    payload = make_payload(512 * 1024, seed=900)
    file_url = server.add_file("site/movie.mp4", payload)

    async def fake_extract(url, options=None):
        assert url == "https://www.youtube.com/watch?v=abc"
        return MediaInfo(
            title="Phim: hay/tuyệt",
            webpage_url=url,
            tracks=[Track(url=file_url, format_id="18", ext="mp4", height=360,
                          vcodec="avc1", acodec="mp4a", filesize=len(payload))],
        )

    monkeypatch.setattr(ytdlp, "extract", fake_extract)

    request = DownloadRequest(
        url="https://www.youtube.com/watch?v=abc", save_dir=tmp_path, connections=4
    )
    assert request.media_kind is MediaKind.SITE

    runner = MediaTaskRunner(1, request)
    assert await runner.run() is TaskState.COMPLETED
    assert runner.dest_path is not None
    # The title is sanitised, keeping the file name Windows-safe.
    assert runner.dest_path.name == "Phim_ hay_tuyệt.mp4"
    assert runner.dest_path.read_bytes() == payload
    assert runner.size == len(payload)


@needs_ffmpeg
async def test_separate_video_and_audio_tracks_are_merged(server, tmp_path, monkeypatch):
    binary = ffmpeg_mod.find_ffmpeg()
    video = tmp_path / "src_video.mp4"
    audio = tmp_path / "src_audio.m4a"
    subprocess.run(
        [str(binary), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", str(video)],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(binary), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "aac", str(audio)],
        check=True, capture_output=True,
    )
    video_url = server.add_file("merge/v.mp4", video.read_bytes())
    audio_url = server.add_file("merge/a.m4a", audio.read_bytes())

    async def fake_extract(url, options=None):
        return MediaInfo(
            title="Clip", webpage_url=url,
            tracks=[
                Track(url=video_url, format_id="137", ext="mp4", height=720,
                      vcodec="avc1", acodec="none", filesize=video.stat().st_size),
                Track(url=audio_url, format_id="140", ext="m4a",
                      vcodec="none", acodec="mp4a", filesize=audio.stat().st_size),
            ],
        )

    monkeypatch.setattr(ytdlp, "extract", fake_extract)
    request = DownloadRequest(
        url="https://vimeo.com/12345", save_dir=tmp_path,
        media_kind=MediaKind.SITE, connections=4,
    )
    runner = MediaTaskRunner(1, request)
    assert await runner.run() is TaskState.COMPLETED

    output = runner.dest_path
    assert output is not None and output.name == "Clip.mp4"
    assert output.stat().st_size > video.stat().st_size
    assert not (tmp_path / "Clip.idmedia").exists()


def test_engine_runs_a_playlist_task(server, tmp_path, no_ffmpeg):
    chunks = publish_segments(server, "engine-hls", 4, seed=600)
    url = simple_playlist(server, "engine-hls", 4)

    with Engine(max_concurrent=2) as engine:
        task_id = engine.submit(hls_request(url, tmp_path))
        assert engine.wait_idle(timeout=60)
        assert engine.state(task_id) is TaskState.COMPLETED
        snap = engine.snapshot(task_id)

    assert snap is not None and snap.path is not None
    assert Path(snap.path).read_bytes() == b"".join(chunks)
