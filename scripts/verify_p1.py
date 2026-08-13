"""P1 acceptance harness - run it to see the engine's claims demonstrated.

    python scripts/verify_p1.py [--size-mb 128] [--connections 8]

Three checks, all against a local server so results are reproducible:

1. Integrity     - multi-segment download matches the source SHA-256.
2. Speedup       - against a server that throttles *per connection* (what a
                   real CDN does), N segments beat 1 by roughly N times.
3. Crash resume  - hard-kill the CLI mid-download, rerun, expect a byte-exact
                   file and no leftover .part / .boltdown.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.cli import main  # noqa: E402
from app.core.resume import ResumeMeta  # noqa: E402
from tests.server import FileServer  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_payload(size: int) -> bytes:
    # os.urandom is fast and incompressible, which keeps the test honest.
    return os.urandom(size)


def run_cli(args: list[str]) -> tuple[int, float]:
    start = time.monotonic()
    code = main(args + ["-q"])
    return code, time.monotonic() - start


def main_verify(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=64)
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--slow-ms", type=int, default=10,
                        help="per-connection delay every 64 KiB (simulated CDN). "
                             "Too small and loopback/disk cost dominates, which "
                             "flatters a single connection.")
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    work = Path(args.work_dir or (PROJECT_ROOT / ".verify"))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    size = args.size_mb * 1024 * 1024
    print(f"generating {args.size_mb} MB payload ...", flush=True)
    data = make_payload(size)
    expected = hashlib.sha256(data).hexdigest()

    failures = 0
    with FileServer() as server:
        server.add_file("payload.bin", data)

        # -- 1. integrity ------------------------------------------------
        out = work / "integrity"
        code, elapsed = run_cli(
            [server.url_for("payload.bin"), "-o", str(out), "-n", str(args.connections)]
        )
        actual = sha256_file(out / "payload.bin")
        ok = code == 0 and actual == expected
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] integrity  {args.size_mb} MB in "
              f"{elapsed:.2f}s ({size / elapsed / 1e6:.1f} MB/s), sha256 "
              f"{'matches' if actual == expected else 'MISMATCH'}")

        # -- 2. speedup --------------------------------------------------
        throttled = server.url_for("payload.bin", slow=args.slow_ms)
        _, single = run_cli([throttled, "-o", str(work / "one"), "-n", "1"])
        _, multi = run_cli([throttled, "-o", str(work / "many"),
                            "-n", str(args.connections)])
        speedup = single / multi if multi else 0
        ok = speedup >= 2.0
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] speedup    1 conn {single:.2f}s vs "
              f"{args.connections} conn {multi:.2f}s -> {speedup:.1f}x")

        # -- 3. crash resume ---------------------------------------------
        out = work / "resume"
        out.mkdir()
        part = out / "payload.bin.part"
        meta = out / "payload.bin.part.boltdown"
        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "app", server.url_for("payload.bin"),
             "-o", str(out), "-n", str(args.connections), "--limit", "8M", "-q"],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            saved = ResumeMeta.load(meta) if meta.exists() else None
            if saved and saved.downloaded > size * 0.25:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        killed_at = ResumeMeta.load(meta).downloaded if meta.exists() else 0
        proc.kill()
        proc.wait(timeout=30)

        code, elapsed = run_cli(
            [server.url_for("payload.bin"), "-o", str(out),
             "-n", str(args.connections)]
        )
        actual = sha256_file(out / "payload.bin")
        ok = (
            code == 0 and actual == expected
            and 0 < killed_at < size
            and not part.exists() and not meta.exists()
        )
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] resume     killed at "
              f"{killed_at / 1e6:.1f} MB, finished in {elapsed:.2f}s, sha256 "
              f"{'matches' if actual == expected else 'MISMATCH'}")

    shutil.rmtree(work, ignore_errors=True)
    print("\nall checks passed" if not failures else f"\n{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main_verify())
