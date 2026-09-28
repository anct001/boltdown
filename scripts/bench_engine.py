"""Benchmark the download engine against what IDM is known for.

    python scripts/bench_engine.py [--only NAME ...] [--json out.json]

A local web server, in its own process so the numbers below are the
downloader's alone, plays the kinds of servers that make IDM worth having:

  capped      every connection is held to 2 MB/s - the usual reason a
              multi-connection downloader is faster at all
  latency     200 ms before every response, as on a far-away server: what
              the probe and each new connection cost before any byte flows
              (its ideal allows the two round trips no downloader avoids)
  slow_link   one connection crawls at 128 KB/s while the others run at
              2 MB/s - the tail that "dynamic segmentation" exists to fix
  flaky       connections are cut after a few megabytes at random
  unlimited   no limits at all: how fast the engine can go, and how much CPU
              each gigabyte costs
  small_files a hundred 256 KB files, four at a time, 50 ms away: what each
              download costs before and after its bytes (probe, requests,
              setting up) - the batch of pictures or a page of attachments

For each it reports the wall time, the ideal time for that server (what a
downloader that kept every allowed connection busy from the first
millisecond to the last would take) and the ratio, `efficiency`. IDM gets
close to 1.0 on the first four; that is the bar.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MB = 1024 * 1024
BLOCK = 1 << 20
_PATTERN = random.Random(7).randbytes(BLOCK + 7)  # 7: blocks never line up


def payload(offset: int, length: int) -> bytes:
    """Bytes `offset..offset+length` of the (virtual, endless) test file."""
    out = bytearray()
    while length > 0:
        block, within = divmod(offset, BLOCK)
        shift = block % 7
        take = min(length, BLOCK - within)
        out += _PATTERN[shift + within: shift + within + take]
        offset += take
        length -= take
    return bytes(out)


def expected_digest(size: int) -> str:
    digest = hashlib.sha256()
    for offset in range(0, size, BLOCK):
        digest.update(payload(offset, min(BLOCK, size - offset)))
    return digest.hexdigest()


# ------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, as real servers do

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        server = self.server
        with server.lock:
            server.connections += 1
            self.number = server.connections
        self.flaky_budget = None

    def do_HEAD(self):
        self._serve(body=False)

    def do_GET(self):
        if self.path.startswith("/__stats"):
            body = json.dumps(self.server.stats).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/__reset"):
            with self.server.lock:
                self.server.stats = {"requests": 0, "first_byte": None}
                self.server.connections = 0
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._serve(body=True)

    def _serve(self, body: bool):
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        size = int(q.get("size", 64 * MB))
        rate = float(q.get("rate", 0)) * MB  # per connection, 0 = unlimited
        slow = int(q.get("slow", 0))         # this connection number crawls
        slow_rate = float(q.get("slow_rate", 0.125)) * MB
        latency = float(q.get("latency", 0)) / 1000
        cut = float(q.get("cut", 0)) * MB    # drop connections after ~this much
        with self.server.lock:
            self.server.stats["requests"] += 1
        if latency:
            time.sleep(latency)

        start, end, partial = 0, size - 1, False
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            first, _, last = header[6:].split(",")[0].partition("-")
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
            partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"bench"')
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return

        speed = slow_rate if slow and self.number == slow else rate
        if cut and self.flaky_budget is None:
            self.flaky_budget = int(cut * random.uniform(0.5, 1.5))
        began = time.monotonic()
        sent = 0
        chunk = 256 * 1024 if not speed else 64 * 1024
        try:
            while sent < length:
                piece = payload(start + sent, min(chunk, length - sent))
                if self.server.stats["first_byte"] is None:
                    with self.server.lock:
                        if self.server.stats["first_byte"] is None:
                            self.server.stats["first_byte"] = time.time()
                self.wfile.write(piece)
                sent += len(piece)
                if self.flaky_budget is not None:
                    self.flaky_budget -= len(piece)
                    if self.flaky_budget <= 0:
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                if speed:
                    ahead = sent / speed - (time.monotonic() - began)
                    if ahead > 0:
                        time.sleep(ahead)
        except OSError:
            self.close_connection = True


def serve() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.lock = threading.Lock()
    server.connections = 0
    server.stats = {"requests": 0, "first_byte": None}
    print(server.server_address[1], flush=True)
    server.serve_forever()


# ------------------------------------------------------------------ scenarios

SCENARIOS = {
    # name: (query, size, ideal seconds)
    "capped": ("rate=2", 64 * MB, 64 / (8 * 2)),
    # No downloader can split a file before it knows its size: the first
    # connection starts after one round trip, the other seven after two.
    # (32 MB/4 MB/s + 0.2 s + 7 x 0.4 s) / 8 connections.
    "latency": ("rate=4&latency=200", 32 * MB, (32 / 4 + 0.2 + 7 * 0.4) / 8),
    "slow_link": ("rate=2&slow=2&slow_rate=0.125", 64 * MB, 64 / (7 * 2 + 0.125)),
    "flaky": ("rate=4&cut=4", 64 * MB, 64 / (8 * 4)),
    "unlimited": ("", 512 * MB, None),
    "small_files": ("latency=50", 256 * 1024, None),
}
SMALL_FILES = 100
SMALL_PARALLEL = 4


async def download(url: str, target: Path, connections: int):
    from app.core.task import DownloadRequest, TaskRunner

    runner = TaskRunner(1, DownloadRequest(url=url, save_dir=target, connections=connections))
    state = await runner.run()
    return runner, state


def get_json(base: str, path: str):
    import urllib.request

    with urllib.request.urlopen(base + path, timeout=10) as reply:
        body = reply.read()
    return json.loads(body) if body else None


async def download_many(urls: list[str], target: Path, connections: int, parallel: int):
    from app.core.task import DownloadRequest, TaskRunner

    gate = asyncio.Semaphore(parallel)

    async def one(i: int, url: str):
        async with gate:
            runner = TaskRunner(
                i, DownloadRequest(url=url, save_dir=target, connections=connections)
            )
            return runner, await runner.run()

    return await asyncio.gather(*(one(i, url) for i, url in enumerate(urls)))


def run_small_files(base: str, connections: int) -> dict:
    query, size, _ = SCENARIOS["small_files"]
    get_json(base, "/__reset")
    urls = [f"{base}/small{i}.bin?size={size}&{query}" for i in range(SMALL_FILES)]
    with tempfile.TemporaryDirectory(prefix="boltdown-bench-") as tmp:
        before = time.process_time()
        started = time.time()
        results = asyncio.run(download_many(urls, Path(tmp), connections, SMALL_PARALLEL))
        elapsed = time.time() - started
        after = time.process_time()
        stats = get_json(base, "/__stats")
        want = expected_digest(size)
        intact = all(
            state.value == "completed"
            and hashlib.sha256(runner.dest_path.read_bytes()).hexdigest() == want
            for runner, state in results
        )
    cpu = after - before  # this process only: the server runs in another
    return {
        "state": "completed" if intact else "failed",
        "intact": intact,
        "seconds": round(elapsed, 2),
        "files_s": round(SMALL_FILES / elapsed, 1),
        "requests_per_file": round(stats["requests"] / SMALL_FILES, 2),
        "cpu_ms_per_file": round(cpu * 1000 / SMALL_FILES, 1),
    }


def run_scenario(base: str, name: str, connections: int) -> dict:
    if name == "small_files":
        return run_small_files(base, connections)
    query, size, ideal = SCENARIOS[name]
    get_json(base, "/__reset")
    url = f"{base}/{name}.bin?size={size}" + (f"&{query}" if query else "")
    with tempfile.TemporaryDirectory(prefix="boltdown-bench-") as tmp:
        before = time.process_time()
        started = time.time()
        runner, state = asyncio.run(download(url, Path(tmp), connections))
        elapsed = time.time() - started
        after = time.process_time()
        stats = get_json(base, "/__stats")
        ok = state.value == "completed" and runner.dest_path is not None
        digest_ok = False
        if ok:
            digest = hashlib.sha256()
            with open(runner.dest_path, "rb") as handle:
                for block in iter(lambda: handle.read(4 * MB), b""):
                    digest.update(block)
            digest_ok = digest.hexdigest() == expected_digest(size)
    cpu = after - before  # this process only: the server runs in another
    result = {
        "state": state.value,
        "intact": digest_ok,
        "seconds": round(elapsed, 2),
        "first_byte_ms": round((stats["first_byte"] - started) * 1000) if stats["first_byte"] else None,
        "requests": stats["requests"],
        "segments": len(runner.segments),
        "mb_s": round(size / MB / elapsed, 1),
        "cpu_s_per_gb": round(cpu / (size / (1024 * MB)), 2),
    }
    if ideal:
        result["ideal_seconds"] = round(ideal, 2)
        result["efficiency"] = round(ideal / elapsed, 2)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS))
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    if args.serve:
        serve()
        return 0

    # Imported before any clock starts: module loading is not download time.
    from app.core.task import DownloadRequest, TaskRunner  # noqa: F401

    server = subprocess.Popen(
        [sys.executable, __file__, "--serve"], stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(server.stdout.readline())
        base = f"http://127.0.0.1:{port}"
        results = {}
        for name in args.only or list(SCENARIOS):
            results[name] = run_scenario(base, name, args.connections)
            print(f"{name:10} {json.dumps(results[name])}", flush=True)
    finally:
        server.kill()
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(r["intact"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
