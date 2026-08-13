from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from app.cli import main
from app.core.resume import ResumeMeta

from .conftest import make_payload, sha256

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_cli_downloads_files(server, tmp_path):
    data = make_payload(900_000, seed=400)
    server.add_file("cli.bin", data)
    code = main([server.url_for("cli.bin"), "-o", str(tmp_path), "-n", "4", "-q"])
    assert code == 0
    assert sha256(tmp_path / "cli.bin") == sha256(data)


def test_cli_reports_failure(server, tmp_path):
    code = main([f"{server.base_url}/absent.bin", "-o", str(tmp_path), "-q"])
    assert code == 1


def test_cli_custom_name_and_headers(server, tmp_path):
    data = make_payload(120_000, seed=401)
    server.add_file("cli-hdr.bin", data)
    code = main([
        server.url_for("cli-hdr.bin"), "-o", str(tmp_path), "-O", "renamed.dat",
        "-H", "X-Test: 1", "--referer", "http://example.com/", "-q",
    ])
    assert code == 0
    assert sha256(tmp_path / "renamed.dat") == sha256(data)


def test_cli_downloads_a_playlist(server, tmp_path, monkeypatch):
    from app.media import ffmpeg as ffmpeg_mod
    from app.media import hls

    monkeypatch.setattr(ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(hls.ffmpeg_mod, "find_ffmpeg", lambda *a, **k: None)

    chunks = [make_payload(64 * 1024, seed=410 + i) for i in range(4)]
    for i, chunk in enumerate(chunks):
        server.add_file(f"cli-hls/seg{i}.ts", chunk)
    playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n" + "".join(
        f"#EXTINF:4.0,\nseg{i}.ts\n" for i in range(4)
    ) + "#EXT-X-ENDLIST\n"
    url = server.add_file("cli-hls/index.m3u8", playlist.encode("utf-8"))

    assert main([url, "-o", str(tmp_path), "-q"]) == 0
    # Named after the folder, and kept as raw TS because ffmpeg is "missing".
    assert sha256(tmp_path / "cli-hls.ts") == sha256(b"".join(chunks))


def test_video_flags_pick_the_media_pipeline():
    from app.cli import _media_kind, build_parser
    from app.media.detect import MediaKind

    args = build_parser().parse_args(["https://example.com/watch/1", "--video"])
    assert args.quality is None
    assert _media_kind("https://example.com/watch/1", args) is MediaKind.SITE
    # A playlist keeps its own pipeline even with --video.
    assert _media_kind("https://example.com/a.m3u8", args) is MediaKind.HLS

    plain = build_parser().parse_args(["https://example.com/a.zip"])
    assert _media_kind("https://example.com/a.zip", plain) is None
    audio = build_parser().parse_args(["https://x.com/w", "--audio-only", "--quality", "720"])
    assert audio.quality == 720 and audio.audio_only
    assert _media_kind("https://x.com/w", audio) is MediaKind.SITE


def test_list_formats_prints_a_table(monkeypatch, capsys):
    from app.media import ytdlp

    info = ytdlp.MediaInfo(
        title="Demo", webpage_url="https://x/1",
        tracks=[ytdlp.Track(url="https://x/v.mp4", format_id="137", height=1080,
                            vcodec="avc1", acodec="none", filesize=1048576)],
    )
    monkeypatch.setattr(ytdlp, "extract_sync", lambda url, options=None: info)
    assert main(["https://x/1", "--list-formats"]) == 0
    out = capsys.readouterr().out
    assert "Demo" in out and "137" in out and "1.0M" in out


def test_list_formats_reports_extraction_failures(monkeypatch, capsys):
    from app.media import ytdlp

    def boom(url, options=None):
        raise ytdlp.ExtractionError("unsupported URL")

    monkeypatch.setattr(ytdlp, "extract_sync", boom)
    assert main(["https://x/1", "--list-formats"]) == 1
    assert "unsupported URL" in capsys.readouterr().err


GRAB_PAGE = (
    b"<html><body>"
    b'<a href="one.jpg">1</a><a href="notes.txt">notes</a>'
    b'<img src="two.png">'
    b"</body></html>"
)


def test_cli_grab_lists_without_downloading(server, tmp_path, capsys):
    server.add_file("cli-grab/index.html", GRAB_PAGE)
    server.add_file("cli-grab/one.jpg", b"jpeg")
    server.add_file("cli-grab/two.png", b"png")
    server.add_file("cli-grab/notes.txt", b"text")

    code = main([
        server.url_for("cli-grab/index.html"), "--grab", "--depth", "0",
        "--dry-run", "-o", str(tmp_path), "-q",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "one.jpg" in out and "two.png" in out and "notes.txt" in out
    assert not list(tmp_path.iterdir()), "--dry-run downloaded something"


def test_cli_grab_downloads_the_filtered_files(server, tmp_path):
    server.add_file("cli-grab2/index.html", GRAB_PAGE)
    server.add_file("cli-grab2/one.jpg", b"jpeg-bytes")
    server.add_file("cli-grab2/two.png", b"png-bytes")
    server.add_file("cli-grab2/notes.txt", b"text")

    code = main([
        server.url_for("cli-grab2/index.html"), "--grab", "--depth", "0",
        "--filter", "jpg,png", "-o", str(tmp_path), "-q",
    ])
    assert code == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["one.jpg", "two.png"]


def test_cli_grab_reports_an_empty_result(server, tmp_path):
    server.add_file("cli-grab3/index.html", b"<html><body>nothing here</body></html>")
    code = main([
        server.url_for("cli-grab3/index.html"), "--grab", "-o", str(tmp_path), "-q",
    ])
    assert code == 1


def test_hard_kill_then_rerun_resumes(server, tmp_path):
    """The P1 acceptance test: kill the process, rerun, expect a byte-exact file.

    `Popen.kill()` maps to TerminateProcess on Windows - no cleanup handlers
    run, which is as close to a power cut as we can get in a test.
    """
    data = make_payload(4 * 1024 * 1024, seed=402)
    server.add_file("killed.bin", data)
    url = server.url_for("killed.bin")

    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "app", url, "-o", str(tmp_path), "-n", "4",
         "--limit", "400k", "-q"],
        cwd=str(PROJECT_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    part = tmp_path / "killed.bin.part"
    meta = tmp_path / "killed.bin.part.boltdown"
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if meta.exists() and part.exists() and meta.stat().st_size > 0:
                # Give it a moment to commit a few megabytes.
                time.sleep(2.0)
                break
            time.sleep(0.1)
        assert proc.poll() is None, "process finished before we could kill it"
    finally:
        proc.kill()
        proc.wait(timeout=30)

    assert part.exists(), "no partial file survived the kill"
    assert meta.exists(), "no resume metadata survived the kill"
    assert not (tmp_path / "killed.bin").exists()

    saved = ResumeMeta.load(meta)
    assert saved is not None and 0 < saved.downloaded < len(data)

    code = main([url, "-o", str(tmp_path), "-n", "4", "-q"])
    assert code == 0
    assert sha256(tmp_path / "killed.bin") == sha256(data)
    assert not part.exists()
    assert not meta.exists()
