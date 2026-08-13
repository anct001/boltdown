"""P6 acceptance harness - exercise the *packaged* build, not the checkout.

    python scripts/build.py --no-installer
    python scripts/verify_p6.py [--dist dist/Boltdown]

Four checks, all against `dist/Boltdown`:

1. Layout    - the three executables exist and none of them shadow another
               (Windows file names are case-insensitive).
2. CLI       - `boltdown-cli.exe` downloads a file from a local server, which
               also proves the frozen build found its CA bundle and Qt-free
               console path.
3. Host      - `boltdown-host.exe` speaks native messaging, starts the windowed
               application when nothing is running, and answers a ping.
4. Handover  - a `download` message sent the way Chrome sends it ends with the
               file on disk.

The application it starts is stopped again at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.db import Database  # noqa: E402
from app.storage.settings import Settings  # noqa: E402
from tests.server import FileServer  # noqa: E402

GUI_EXE = "Boltdown.exe" if os.name == "nt" else "Boltdown"
CLI_EXE = "boltdown-cli.exe" if os.name == "nt" else "boltdown-cli"
HOST_EXE = "boltdown-host.exe" if os.name == "nt" else "boltdown-host"


def report(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<9} {detail}")
    return ok


def ask_host(host: Path, message: dict, timeout: float = 120) -> dict:
    """One native-messaging exchange, framed exactly like Chrome frames it."""
    payload = json.dumps(message).encode("utf-8")
    process = subprocess.Popen(
        [str(host)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ},
    )
    out, err = process.communicate(struct.pack("<I", len(payload)) + payload, timeout=timeout)
    if len(out) < 4:
        return {"ok": False, "error": err.decode("utf-8", "replace")[-300:]}
    (length,) = struct.unpack("<I", out[:4])
    return json.loads(out[4 : 4 + length])


def stop_app() -> None:
    if os.name != "nt":  # pragma: no cover - Windows is the target
        return
    subprocess.run(
        ["taskkill", "/IM", GUI_EXE, "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def check_layout(dist: Path) -> bool:
    names = [p.name for p in dist.glob("*") if p.suffix.lower() == ".exe"]
    missing = [n for n in (GUI_EXE, CLI_EXE, HOST_EXE) if n not in names]
    lowered = [n.lower() for n in names]
    unique = len(set(lowered)) == len(lowered)
    size = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file()) / 1048576
    return report(
        "layout", not missing and unique,
        f"{len(names)} executables, {size:.0f} MB"
        + (f", missing {missing}" if missing else ""),
    )


def check_cli(dist: Path, server: FileServer, out: Path, payload: bytes) -> bool:
    url = server.url_for("packaged.bin")
    start = time.monotonic()
    result = subprocess.run(
        [str(dist / CLI_EXE), url, "-o", str(out), "-n", "4", "-q"],
        capture_output=True, text=True, timeout=180,
    )
    target = out / "packaged.bin"
    ok = result.returncode == 0 and target.exists() and target.read_bytes() == payload
    return report(
        "cli", ok,
        f"downloaded {len(payload) // 1024} KB in {time.monotonic() - start:.1f}s"
        + ("" if ok else f" (exit {result.returncode}: {result.stderr[-200:]})"),
    )


def check_host(dist: Path) -> bool:
    start = time.monotonic()
    reply = ask_host(dist / HOST_EXE, {"type": "ping"})
    return report(
        "host", bool(reply.get("ok")),
        f"ping answered in {time.monotonic() - start:.1f}s: {reply}",
    )


def check_handover(dist: Path, server: FileServer, out: Path, payload: bytes) -> bool:
    url = server.url_for("from-browser.bin")
    reply = ask_host(dist / HOST_EXE, {
        "type": "download", "url": url,
        "referer": "https://example.com/", "user_agent": "TestBrowser/1.0",
    })
    target = out / "from-browser.bin"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not target.exists():
        time.sleep(0.25)
    ok = bool(reply.get("ok")) and target.exists() and target.read_bytes() == payload
    return report(
        "handover", ok,
        f"{target.name} " + ("landed in the download folder" if ok else f"failed: {reply}"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default=str(PROJECT_ROOT / "dist" / "Boltdown"))
    parser.add_argument("--size-kb", type=int, default=3072)
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    dist = Path(args.dist).resolve()
    if not (dist / CLI_EXE).exists():
        print(f"no build in {dist} - run scripts/build.py first", file=sys.stderr)
        return 2

    work = Path(args.work_dir) if args.work_dir else dist.parent / "verify-p6"
    downloads = work / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    # Keep the packaged app out of the real profile, and let it download
    # without asking - it has no visible window in this harness.
    os.environ["BOLTDOWN_HOME"] = str(work / "home")
    Path(os.environ["BOLTDOWN_HOME"]).mkdir(parents=True, exist_ok=True)
    db = Database(Path(os.environ["BOLTDOWN_HOME"]) / "boltdown.db")
    Settings(db).update({
        "ask_before_download": False,
        "download_dir": str(downloads),
        "use_categories": False,
    })
    db.close()

    server = FileServer().start()
    payload = os.urandom(args.size_kb * 1024)
    server.add_file("packaged.bin", payload)
    server.add_file("from-browser.bin", payload)

    try:
        ok = check_layout(dist)
        ok = check_cli(dist, server, downloads, payload) and ok
        ok = check_host(dist) and ok
        ok = check_handover(dist, server, downloads, payload) and ok
    finally:
        stop_app()
        server.stop()
    print(f"\nworking directory: {work}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
