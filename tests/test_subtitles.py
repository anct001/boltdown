"""Subtitles and cover art for videos from yt-dlp pages."""

from __future__ import annotations

import subprocess

import pytest

from app.core.task import DownloadRequest, TaskState
from app.media import ffmpeg as ffmpeg_mod
from app.media import ytdlp
from app.media.runner import MediaTaskRunner
from app.media.ytdlp import MediaInfo, Track, info_from_dict, pick_subtitles

from .test_hls import needs_ffmpeg

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:00.800\nXin chào\n"


def _info_dict(**extra) -> dict:
    base = {
        "title": "t", "webpage_url": "https://v.example/w",
        "formats": [{"url": "https://cdn/v.mp4", "ext": "mp4", "protocol": "https",
                     "vcodec": "avc1", "acodec": "mp4a"}],
    }
    base.update(extra)
    return base


def test_people_made_subtitles_beat_machine_captions():
    info = info_from_dict(_info_dict(
        subtitles={"vi": [{"ext": "json3", "url": "u0"}, {"ext": "vtt", "url": "u1"}]},
        automatic_captions={
            "vi": [{"ext": "vtt", "url": "auto-vi"}],
            "en": [{"ext": "srt", "url": "auto-en"}, {"ext": "vtt", "url": "auto-en-vtt"}],
            "live_chat": [{"ext": "json", "url": "x"}],
        },
    ))
    assert info.subtitles["vi"].url == "u1" and not info.subtitles["vi"].auto
    assert info.subtitles["en"].url == "auto-en" and info.subtitles["en"].auto
    assert "live_chat" not in info.subtitles


def test_picking_by_language():
    info = info_from_dict(_info_dict(
        subtitles={"en-US": [{"ext": "vtt", "url": "us"}], "vi": [{"ext": "vtt", "url": "vi"}]},
        automatic_captions={"fr": [{"ext": "vtt", "url": "fr"}]},
    ))
    assert [t.url for t in pick_subtitles(info, ["vi", "en"])] == ["vi", "us"]
    assert [t.url for t in pick_subtitles(info, ["de"])] == []
    # "all" means every people-made subtitle, not a hundred machine captions
    assert sorted(t.url for t in pick_subtitles(info, ["all"])) == ["us", "vi"]
    assert pick_subtitles(info, []) == []


def _make_media(tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(
        [ffmpeg_mod.find_ffmpeg(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)],
        check=True,
    )
    cover = tmp_path / "c.jpg"
    subprocess.run(
        [ffmpeg_mod.find_ffmpeg(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1", str(cover)],
        check=True,
    )
    subtitle = tmp_path / "s.vtt"
    subtitle.write_text(VTT, encoding="utf-8")
    return video, cover, subtitle


def _streams(path) -> str:
    probe = subprocess.run([ffmpeg_mod.find_ffmpeg(), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True)
    return probe.stderr


@needs_ffmpeg
@pytest.mark.parametrize("container", ["mp4", "mkv"])
async def test_extras_go_inside_the_file(tmp_path, container):
    video, cover, subtitle = _make_media(tmp_path)
    source = tmp_path / f"in.{container}"
    await ffmpeg_mod.remux(video, source)
    output = tmp_path / f"out.{container}"
    await ffmpeg_mod.embed_extras(source, output, subtitles=[(subtitle, "vi")], cover=cover)

    streams = _streams(output)
    assert "Subtitle:" in streams
    assert "(vie)" in streams or "language" in streams or container == "mkv"
    if container == "mp4":
        assert "attached pic" in streams
    else:
        assert "filename        : cover.jpg" in streams and "image/jpeg" in streams


async def _run_page(server, tmp_path, monkeypatch, payload, *, ext="mp4", **request):
    file_url = server.add_file(f"page/video.{ext}", payload)
    sub_url = server.add_file("page/video.vi.vtt", VTT.encode())
    cover_url = server.add_file("page/thumb.jpg", b"\xff\xd8 not really a jpeg")

    async def fake_extract(url, options=None):
        return MediaInfo(
            title="Clip", webpage_url=url, thumbnail=cover_url,
            tracks=[Track(url=file_url, format_id="18", ext=ext, height=120,
                          vcodec="avc1", acodec="mp4a", filesize=len(payload))],
            subtitles={"vi": ytdlp.SubtitleTrack("vi", sub_url, "vtt")},
        )

    monkeypatch.setattr(ytdlp, "extract", fake_extract)
    runner = MediaTaskRunner(1, DownloadRequest(
        url="https://www.youtube.com/watch?v=x", save_dir=tmp_path, connections=2, **request,
    ))
    assert await runner.run() is TaskState.COMPLETED
    return runner


async def test_without_ffmpeg_extras_land_beside_the_video(server, tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)
    payload = b"\0" * 200_000
    runner = await _run_page(server, tmp_path, monkeypatch, payload,
                             subtitle_langs=["vi"], embed_thumbnail=True)
    assert runner.dest_path.read_bytes() == payload, "the video itself is untouched"
    assert (tmp_path / "Clip.vi.vtt").read_text(encoding="utf-8") == VTT
    assert (tmp_path / "Clip.jpg").exists()


async def test_nothing_extra_unless_asked(server, tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)
    runner = await _run_page(server, tmp_path, monkeypatch, b"\0" * 100_000)
    assert sorted(p.name for p in tmp_path.iterdir()) == [runner.dest_path.name]


@needs_ffmpeg
async def test_with_ffmpeg_the_subtitle_is_embedded(server, tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    video, _cover, _subtitle = _make_media(source)
    runner = await _run_page(server, tmp_path, monkeypatch, video.read_bytes(),
                             subtitle_langs=["vi"])
    assert "Subtitle:" in _streams(runner.dest_path)
    assert not (tmp_path / "Clip.vi.vtt").exists()


@needs_ffmpeg
async def test_a_broken_cover_never_fails_the_download(server, tmp_path, monkeypatch):
    """The thumbnail in this test is not a real JPEG: converting it fails,
    and the video still finishes, with the extras saved beside it."""
    source = tmp_path / "src"
    source.mkdir()
    video, _cover, _subtitle = _make_media(source)
    runner = await _run_page(server, tmp_path, monkeypatch, video.read_bytes(),
                             subtitle_langs=["vi"], embed_thumbnail=True)
    assert runner.dest_path.exists()
    assert (tmp_path / "Clip.vi.vtt").exists() and (tmp_path / "Clip.jpg").exists()
