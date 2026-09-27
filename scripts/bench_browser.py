"""End-to-end benchmark of the browser integration, in a real Chromium.

    python scripts/bench_browser.py [--runs 5] [--json out.json]

Needs node, the `playwright` npm package (NODE_PATH pointing at it, or
installed next to this script) and a Chromium build Playwright can drive.
Linux only: it registers the native host inside a throwaway browser profile,
which is where Chromium looks for it there.

Everything real except the application window: the built extension, the
generated native-host launcher, the loopback IPC endpoint with its token. The
endpoint just writes down what arrived and when. A local web server plays
the websites, and counts how many bytes of each file the browser pulled
before it gave the file up.

What it measures, and what an IDM-class integration should manage:

  takeover_ms      browser requests the file -> the app has its URL
  wasted_kb        bytes the browser fetched before letting go
  batch_links_s    "download every link on this page", links handed over/s
  hls_playlist     the playlist survives a page that fetches 60 segments
  typed_media      media found by Content-Type when the URL has no extension
  video_button     a download button sits on the <video> itself
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

BIG_FILE = 200 * 1024 * 1024
#: how fast the "website" hands out a big file: a good home connection
RATE = 12 * 1024 * 1024
CHUNK = 64 * 1024
SEGMENTS = 60

PAGES = {
    "/": """<!doctype html><title>files</title>
        <a id="big" href="/files/big.bin">big</a>""",
    "/links": "<!doctype html><title>links</title>" + "".join(
        f'<a href="/files/f{i}.zip">f{i}</a> ' for i in range(50)
    ) + '<a href="/about.html">about</a><a href="/news">news</a>',
    "/hls": """<!doctype html><title>hls</title><script>
        (async () => {
          await fetch("/v/master.m3u8");
          for (let i = 0; i < %d; i++) await fetch(`/v/seg${i}.ts`);
          document.title = "done";
        })();
        </script>""" % SEGMENTS,
    "/typed": """<!doctype html><title>typed</title><script>
        fetch("/stream?id=42").then(() => { document.title = "done"; });
        </script>""",
    # Clicks its own download links, one every few seconds, so the download
    # phase needs no automation attached to the browser at all.
    "/auto": """<!doctype html><title>auto</title><script>
        const runs = Number(new URLSearchParams(location.search).get("runs") || 5);
        let i = 0;
        const next = () => {
          if (i >= runs) { document.title = "done"; return; }
          const a = document.createElement("a");
          a.href = `/files/big${i++}.bin`;
          document.body.appendChild(a);
          a.click();
          setTimeout(next, 3000);
        };
        setTimeout(next, 2500);
        </script>""",
    "/video": """<!doctype html><title>video</title>
        <div style="margin:120px"><video id="v" width="480" height="270"
        src="/media/clip.mp4" controls></video></div>""",
}


class Site(BaseHTTPRequestHandler):
    served: dict  # path -> bytes written before the client went away
    started: dict  # path -> when the browser asked for it (ms)
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _count(self, n: int) -> None:
        with self.lock:
            self.served[self.path] = self.served.get(self.path, 0) + n

    def do_GET(self):
        path = self.path.split("?")[0]
        self.started.setdefault(path, time.time() * 1000)
        if path in PAGES:
            body = PAGES[path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/files/"):
            self._slow_file(path.rsplit("/", 1)[1])
            return
        kinds = {
            ".m3u8": "application/vnd.apple.mpegurl",
            ".ts": "video/mp2t",
            ".mp4": "video/mp4",
        }
        if path == "/stream":
            ctype, size = "video/mp4", 3 * 1024 * 1024
        else:
            ext = "." + path.rsplit(".", 1)[-1]
            ctype, size = kinds.get(ext, "application/octet-stream"), 200 * 1024
        if path.endswith(".m3u8"):
            body = ("#EXTM3U\n" + "".join(
                f"#EXTINF:4,\nseg{i}.ts\n" for i in range(SEGMENTS)
            ) + "#EXT-X-ENDLIST\n").encode()
        else:
            body = b"\0" * size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _slow_file(self, name: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(BIG_FILE))
        self.end_headers()
        chunk = b"\0" * CHUNK
        start = time.monotonic()
        sent = 0
        try:
            while sent < BIG_FILE:
                self.wfile.write(chunk)
                sent += CHUNK
                self._count(CHUNK)
                ahead = sent / RATE - (time.monotonic() - start)
                if ahead > 0:
                    time.sleep(ahead)
        except OSError:
            pass


def find_node_modules() -> str | None:
    for candidate in (os.environ.get("NODE_PATH"), str(Path(__file__).parent / "node_modules")):
        if candidate and (Path(candidate) / "playwright").is_dir():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json")
    parser.add_argument("--chromium", default=os.environ.get(
        "BOLTDOWN_CHROMIUM", "/opt/pw-browsers/chromium"))
    args = parser.parse_args(argv)

    node = shutil.which("node")
    modules = find_node_modules()
    if not node or not modules or sys.platform != "linux":
        print("needs Linux, node and the playwright npm package (NODE_PATH)")
        return 2

    work = Path(tempfile.mkdtemp(prefix="boltdown-bench-"))
    home = work / "home"
    home.mkdir()
    os.environ["BOLTDOWN_HOME"] = str(home)

    from app.ipc import endpoint, register

    sys.path.insert(0, str(Path(__file__).parent))
    import build_extension

    built = {name: folder for name, folder, _ in build_extension.build(work / "ext")}
    ext_dir = built["chrome"]
    arrived: list[tuple[float, dict]] = []

    def handler(message):
        arrived.append((time.time() * 1000, message))
        if message.get("type") == "ping":
            return {"ok": True, "app": "Boltdown", "version": "bench"}
        return {"ok": True, "accepted": message.get("url")}

    served: dict = {}
    started: dict = {}
    site = ThreadingHTTPServer(
        ("127.0.0.1", 0), type("S", (Site,), {"served": served, "started": started})
    )
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_address[1]}"

    profile = work / "profile"
    (profile / "NativeMessagingHosts").mkdir(parents=True)
    # The test page starts several downloads by itself; allow that the way a
    # user answering Chrome's "download multiple files?" prompt would.
    (profile / "Default").mkdir()
    (profile / "Default" / "Preferences").write_text(json.dumps({
        "profile": {"default_content_setting_values": {"automatic_downloads": 1}},
        "download": {"prompt_for_download": False, "default_directory": str(work / "dl")},
    }), encoding="utf-8")
    launcher = register.write_launcher(work)

    config = {
        "base": base,
        "extension": str(ext_dir),
        "chromium": args.chromium,
        "profile": str(profile),
        "hostDir": str(profile / "NativeMessagingHosts"),
        "launcher": str(launcher),
        "runs": args.runs,
    }
    with endpoint.IpcServer(handler):
        env = {**os.environ, "NODE_PATH": modules, "BOLTDOWN_HOME": str(home)}
        proc = subprocess.run(
            [node, str(Path(__file__).with_name("bench_browser.js")), json.dumps(config)],
            env=env, capture_output=True, text=True, timeout=600,
        )
    site.shutdown()
    if proc.returncode != 0 or not proc.stdout.strip():
        print(proc.stdout[-2000:], proc.stderr[-4000:])
        return 1
    browser = json.loads(proc.stdout.strip().splitlines()[-1])

    # ---------------------------------------------------------------- report
    # Takeover: from the browser asking the site for the file to the app
    # having its URL - both ends timed here, on one clock.
    downloads = [(t, m) for t, m in arrived if m.get("type") == "download"]
    takeovers = []
    for i in range(args.runs):
        name = f"big{i}.bin"
        asked = started.get(f"/files/{name}")
        hit = next((t for t, m in downloads if m.get("url", "").endswith(name)), None)
        takeovers.append({
            "file": name,
            "ms": round(hit - asked) if hit and asked else None,
            "wasted_kb": round(served.get(f"/files/{name}", 0) / 1024),
        })
    batch = browser["batch"]
    batch_urls = [m for t, m in arrived if t >= batch["start"] and t <= batch["end"] + 50
                  and m.get("type") in ("download", "batch")]
    handed = sum(len(m.get("items", [])) or 1 for m in batch_urls)
    seconds = max((batch["end"] - batch["start"]) / 1000, 1e-3)
    ms = [t["ms"] for t in takeovers if t["ms"] is not None]
    result = {
        "takeover": takeovers,
        "takeover_ms_first": ms[0] if ms else None,
        "takeover_ms_median_after_first": sorted(ms[1:])[len(ms[1:]) // 2] if len(ms) > 1 else None,
        "wasted_kb_median": sorted(t["wasted_kb"] for t in takeovers)[len(takeovers) // 2],
        "batch_links_handed": handed,
        "batch_messages": len(batch_urls),
        "batch_seconds": round(seconds, 2),
        "batch_links_s": round(handed / seconds, 1),
        "hls_entries": browser["hls"]["count"],
        "hls_playlist": browser["hls"]["playlist"],
        "typed_media": browser["typed"],
        "video_button": browser["videoButton"],
        "worker_errors": browser["errors"],
    }
    print(json.dumps(result, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
