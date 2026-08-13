"""Command line front end - P1's way to drive the engine without a GUI."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .core.engine import Engine, EngineEvent
from .core.task import DownloadRequest, TaskSnapshot, TaskState
from .util.fmt import human_size, human_speed, human_duration, parse_size
from .util.log import setup_logging
from .util.paths import default_download_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boltdown",
        description="Multi-segment download manager (IDM-like) - CLI front end.",
    )
    parser.add_argument(
        "urls", nargs="*", help="one or more URLs to download"
    )
    parser.add_argument(
        "-o", "--output-dir", default=None,
        help="destination directory (default: your Downloads folder)",
    )
    parser.add_argument("-O", "--name", default=None, help="override the file name")
    parser.add_argument(
        "-n", "--connections", type=int, default=8,
        help="segments per download, 1-32 (default: 8)",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=3, help="downloads running at once (default: 3)"
    )
    parser.add_argument(
        "-l", "--limit", default=None,
        help="global speed limit, e.g. 500k or 2M (default: unlimited)",
    )
    parser.add_argument("--referer", default=None)
    parser.add_argument("--cookie", default=None, help="raw Cookie header value")
    parser.add_argument("--user-agent", default=None)
    parser.add_argument("--proxy", default=None, help="e.g. http://127.0.0.1:8080")
    parser.add_argument(
        "-H", "--header", action="append", default=[], metavar="K:V",
        help="extra request header (repeatable)",
    )
    parser.add_argument(
        "--user", default=None, metavar="USER:PASS", help="HTTP basic auth"
    )
    parser.add_argument(
        "--insecure", action="store_true", help="do not verify TLS certificates"
    )
    parser.add_argument(
        "--categories", action="store_true",
        help="sort finished files into Video/Music/... subfolders",
    )
    media = parser.add_argument_group("video / streaming")
    media.add_argument(
        "--video", action="store_true",
        help="treat the URL as a video page and let yt-dlp find the streams "
             "(.m3u8 and .mpd links are detected on their own)",
    )
    media.add_argument(
        "--quality", type=int, default=None, metavar="HEIGHT",
        help="cap the video height, e.g. 1080 or 720 (default: best available)",
    )
    media.add_argument(
        "--audio-only", action="store_true", help="download the audio track only"
    )
    media.add_argument(
        "--list-formats", action="store_true",
        help="print the available formats and exit",
    )
    media.add_argument(
        "--playlist", action="store_true",
        help="treat the URL as a playlist and download every video in it",
    )
    media.add_argument(
        "--ffmpeg", default=None, metavar="PATH",
        help="ffmpeg binary to use for merging and remuxing",
    )
    grab = parser.add_argument_group("site grabber")
    grab.add_argument(
        "--grab", action="store_true",
        help="treat the URL as a page to crawl and download what it links to",
    )
    grab.add_argument(
        "--depth", type=int, default=1, metavar="N",
        help="how many link hops to follow while grabbing (default: 1)",
    )
    grab.add_argument(
        "--filter", default=None, metavar="EXT",
        help="extensions to keep, e.g. jpg,png,mp4 (default: every file)",
    )
    grab.add_argument("--match", default=None, help="only URLs matching this regex")
    grab.add_argument("--exclude", default=None, help="skip URLs matching this regex")
    grab.add_argument(
        "--max-pages", type=int, default=50, help="page limit while grabbing"
    )
    grab.add_argument(
        "--dry-run", action="store_true", help="list what --grab found, download nothing"
    )
    browser = parser.add_argument_group("browser integration")
    browser.add_argument(
        "--register-host", nargs="+", default=None, metavar="EXT_ID",
        help="register the native messaging host for these extension ids",
    )
    browser.add_argument(
        "--unregister-host", action="store_true",
        help="remove the native messaging host registration",
    )
    browser.add_argument(
        "--host-status", action="store_true",
        help="show which browsers know about the native messaging host",
    )
    remote = parser.add_argument_group("remote control (talks to the running app)")
    remote.add_argument(
        "--remote-add", nargs="+", default=None, metavar="URL",
        help="hand URLs to the running application instead of downloading here",
    )
    remote.add_argument(
        "--remote-list", action="store_true", help="show what it is downloading",
    )
    remote.add_argument(
        "--remote-pause", nargs="?", const="all", default=None, metavar="ID",
        help="pause one download by id, or everything",
    )
    remote.add_argument(
        "--remote-resume", nargs="?", const="all", default=None, metavar="ID",
        help="resume one download by id, or everything",
    )
    remote.add_argument(
        "--check-update", action="store_true",
        help="ask GitHub whether a newer release exists",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"boltdown {__version__}")
    return parser


def _parse_headers(items: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition(":")
        if not sep:
            raise SystemExit(f"invalid header {item!r}, expected 'Key: Value'")
        headers[key.strip()] = value.strip()
    return headers


def _media_kind(url: str, args: argparse.Namespace):
    """None lets the request classify itself; `--video` forces yt-dlp."""
    from .media.detect import MediaKind, classify

    if not (args.video or args.audio_only or args.playlist):
        return None
    kind = classify(url)
    return MediaKind.SITE if kind is MediaKind.DIRECT else kind


def _grab(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Crawl every URL and return the (url, referer) pairs found."""
    import asyncio

    from .core.http_client import RequestSpec, build_client
    from .grabber.crawler import CrawlOptions, crawl

    options = CrawlOptions(
        depth=max(0, args.depth),
        max_pages=max(1, args.max_pages),
        extensions=tuple(
            e.strip().lstrip(".") for e in (args.filter or "").split(",") if e.strip()
        ),
        pattern=args.match,
        exclude=args.exclude,
    )

    async def run() -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for url in args.urls:
            spec = RequestSpec(
                url=url,
                cookie=args.cookie,
                referer=args.referer,
                user_agent=args.user_agent,
                proxy=args.proxy,
                verify_tls=not args.insecure,
            )
            async with build_client(spec) as client:
                result = await crawl(client, url, options)
            print(
                f"{url}: {len(result.files)} files on {result.pages_visited} pages"
                + (" (limit reached)" if result.stopped_early else ""),
                file=sys.stderr,
            )
            found.extend((f.url, f.referer) for f in result.files)
        return found

    return asyncio.run(run())


def _browser_integration(args: argparse.Namespace) -> int:
    """`--register-host` and friends: the installed build has no `python -m`."""
    from .ipc import register

    if args.host_status:
        for browser, value in register.status().items():
            print(f"{browser:9} {value or '-'}")
        return 0
    if args.unregister_host:
        removed = register.uninstall()
        print("removed:", ", ".join(removed) if removed else "nothing")
        return 0
    try:
        results = register.install(list(args.register_host))
    except (ValueError, OSError) as exc:
        print(f"registration failed: {exc}", file=sys.stderr)
        return 2
    for browser, value in results.items():
        print(f"{browser:9} {value}")
    print("\nNow reload the extension in the browser.")
    return 0


def _check_update() -> int:
    """`--check-update`: report, never install."""
    from .util import updates

    newer = updates.check(__version__)
    if newer is None:
        print(f"boltdown {__version__} is the newest release I can see")
        return 0
    print(f"{newer.name} is available (you have {__version__})")
    if newer.url:
        print(newer.url)
    if newer.has_installer:
        size = newer.asset_size / 1048576
        print(f"installer: {newer.asset_name} ({size:.1f} MB)")
        print(f"  boltdown-cli \"{newer.asset_url}\"   # download it with this app")
    return 0


def _remote(args: argparse.Namespace) -> int:
    """Drive the running GUI over the local IPC socket."""
    from .ipc import endpoint
    from .ipc.protocol import TYPE_DOWNLOAD, TYPE_LIST, TYPE_PAUSE, TYPE_RESUME

    def send(message: dict) -> dict | None:
        reply = endpoint.send(message)
        if reply is None:
            print("Boltdown is not running", file=sys.stderr)
        return reply

    if args.remote_list:
        reply = send({"type": TYPE_LIST})
        if reply is None:
            return 1
        if not reply.get("ok"):
            print(reply.get("error", "refused"), file=sys.stderr)
            return 1
        rows = reply.get("downloads") or []
        if not rows:
            print("nothing in the list")
            return 0
        print(f"{'ID':>4}  {'STATE':<11} {'PROGRESS':>18}  {'SPEED':>11}  NAME")
        for row in rows:
            size = row.get("size") or 0
            done = row.get("downloaded") or 0
            progress = f"{human_size(done)}/{human_size(size)}" if size else human_size(done)
            print(
                f"{row['id']:>4}  {row['state']:<11} {progress:>18}  "
                f"{human_speed(row.get('speed') or 0):>11}  {row['name']}"
            )
        return 0

    if args.remote_add:
        failed = False
        for url in args.remote_add:
            reply = send({"type": TYPE_DOWNLOAD, "url": url})
            if reply is None:
                return 1
            print(f"{url}: {'accepted' if reply.get('ok') else reply.get('error')}")
            failed = failed or not reply.get("ok")
        return 1 if failed else 0

    for value, kind in ((args.remote_pause, TYPE_PAUSE), (args.remote_resume, TYPE_RESUME)):
        if value is None:
            continue
        message = {"type": kind}
        if value != "all":
            try:
                message["id"] = int(value)
            except ValueError:
                print(f"not a download id: {value}", file=sys.stderr)
                return 2
        reply = send(message)
        if reply is None:
            return 1
        print("ok" if reply.get("ok") else reply.get("error", "refused"))
        return 0 if reply.get("ok") else 1
    return 0


def _expand_playlists(args: argparse.Namespace) -> list[str]:
    """Replace playlist URLs with the URLs of the videos inside them."""
    from .media import ytdlp

    options = ytdlp.build_options(
        proxy=args.proxy,
        cookie=args.cookie,
        referer=args.referer,
        user_agent=args.user_agent,
        verify_tls=not args.insecure,
    )
    urls: list[str] = []
    for url in args.urls:
        try:
            playlist = ytdlp.extract_playlist_sync(url, options)
        except Exception as exc:  # noqa: BLE001 - the message is the output
            print(f"{url}: {exc}", file=sys.stderr)
            continue
        if playlist is None or not playlist.entries:
            print(f"{url}: not a playlist", file=sys.stderr)
            urls.append(url)
            continue
        print(f"{playlist.title}: {len(playlist.entries)} videos", file=sys.stderr)
        urls.extend(entry.url for entry in playlist.entries)
    return urls


def _list_formats(args: argparse.Namespace) -> int:
    from .media import ytdlp

    options = ytdlp.build_options(
        proxy=args.proxy,
        cookie=args.cookie,
        referer=args.referer,
        user_agent=args.user_agent,
        verify_tls=not args.insecure,
    )
    failed = False
    for url in args.urls:
        try:
            info = ytdlp.extract_sync(url, options)
        except Exception as exc:  # noqa: BLE001 - the message is the output
            print(f"{url}: {exc}", file=sys.stderr)
            failed = True
            continue
        print(f"\n{info.title}  ({url})")
        for row in ytdlp.format_table(info):
            print(row)
    return 1 if failed else 0


class ProgressPrinter:
    """Redraws one line per download in place."""

    def __init__(self, count: int, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self.count = count
        self.lines: dict[int, str] = {}
        self.order: list[int] = []
        self._drawn = 0
        self._last = 0.0

    def update(self, snap: TaskSnapshot, force: bool = False) -> None:
        if snap.id not in self.lines:
            self.order.append(snap.id)
        self.lines[snap.id] = self._format(snap)
        now = time.monotonic()
        if not force and now - self._last < 0.2:
            return
        self._last = now
        self.draw()

    def _format(self, snap: TaskSnapshot) -> str:
        name = snap.filename
        if len(name) > 34:
            name = name[:31] + "..."
        if snap.state in (TaskState.COMPLETED, TaskState.ERROR, TaskState.CANCELLED,
                          TaskState.PAUSED):
            tail = snap.error or snap.state.value
            return f"{name:<34} {human_size(snap.downloaded):>10}  {tail}"
        if snap.size:
            pct = snap.percent
            filled = int(pct / 5)
            bar = "#" * filled + "-" * (20 - filled)
            return (
                f"{name:<34} [{bar}] {pct:5.1f}%  "
                f"{human_speed(snap.speed):>11}  ETA {human_duration(snap.eta)}  "
                f"x{snap.connections}"
            )
        return (
            f"{name:<34} {human_size(snap.downloaded):>10}  "
            f"{human_speed(snap.speed):>11}  (size unknown)"
        )

    def draw(self) -> None:
        if not self.enabled:
            return
        out = sys.stderr
        if self._drawn:
            out.write(f"\x1b[{self._drawn}A")
        for task_id in self.order:
            out.write("\x1b[2K" + self.lines[task_id] + "\n")
        self._drawn = len(self.order)
        out.flush()

    def finish(self) -> None:
        self.draw()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        console=not args.quiet,
    )

    if args.register_host or args.unregister_host or args.host_status:
        return _browser_integration(args)
    if args.check_update:
        return _check_update()
    if (args.remote_add or args.remote_list or args.remote_pause
            or args.remote_resume):
        return _remote(args)
    if not args.urls:
        parser.error("no URLs given")

    save_dir = Path(args.output_dir) if args.output_dir else default_download_dir()
    save_dir.mkdir(parents=True, exist_ok=True)

    limit = parse_size(args.limit) if args.limit else None
    auth = None
    if args.user:
        user, _, password = args.user.partition(":")
        auth = (user, password)

    if args.list_formats:
        return _list_formats(args)

    if args.playlist:
        expanded = _expand_playlists(args)
        if not expanded:
            return 1
        args.urls = expanded

    targets: list[tuple[str, str | None]] = [(url, args.referer) for url in args.urls]
    if args.grab:
        found = _grab(args)
        for url, _referer in found:
            print(url)
        if args.dry_run:
            return 0 if found else 1
        if not found:
            return 1
        targets = list(found)

    printer = ProgressPrinter(len(targets), enabled=not args.quiet)
    results: dict[int, TaskSnapshot] = {}

    def on_event(event: EngineEvent) -> None:
        if event.snapshot is None:
            return
        results[event.task_id] = event.snapshot
        printer.update(event.snapshot, force=event.type != "progress")

    engine = Engine(max_concurrent=max(1, args.jobs), speed_limit=limit, on_event=on_event)
    engine.start()

    connections = max(1, min(32, args.connections))
    for url, referer in targets:
        engine.submit(
            DownloadRequest(
                url=url,
                save_dir=save_dir,
                filename=args.name if len(targets) == 1 else None,
                connections=connections,
                use_categories=args.categories,
                headers=_parse_headers(args.header),
                cookie=args.cookie,
                referer=referer,
                user_agent=args.user_agent,
                proxy=args.proxy,
                auth=auth,
                verify_tls=not args.insecure,
                media_kind=_media_kind(url, args),
                max_height=args.quality,
                audio_only=args.audio_only,
                ffmpeg_path=args.ffmpeg,
            )
        )

    interrupted = False
    try:
        while not engine.wait_idle(timeout=0.25):
            printer.draw()
    except KeyboardInterrupt:
        interrupted = True
        print("\nPausing... (progress is saved, rerun the same command to resume)",
              file=sys.stderr)
        engine.pause_all()
        engine.wait_idle(timeout=30)
    finally:
        engine.stop(timeout=30)
        printer.finish()

    failed = [s for s in results.values() if s.state == TaskState.ERROR]
    done = [s for s in results.values() if s.state == TaskState.COMPLETED]
    if not args.quiet:
        for snap in done:
            print(f"saved: {snap.path}", file=sys.stderr)
        for snap in failed:
            print(f"failed: {snap.url} ({snap.error})", file=sys.stderr)

    if interrupted:
        return 130
    return 1 if failed or not done else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
