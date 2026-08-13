"""P5 acceptance harness - queues, the scheduler and the site grabber.

    python scripts/verify_p5.py [--files 3] [--concurrent 2]

Three checks against a local server, so nothing depends on the network:

1. Missed schedule - a start time two minutes in the past still fires the
                     moment the app comes up, which is the "the machine was off
                     at 02:00" case.
2. Queue order     - a queue runs `max_concurrent` files at a time and reports
                     exactly one `queueFinished` when it drains.
3. Site grabber    - crawl a two-page site and download everything it matched.

Qt runs on the offscreen platform, so this is safe over a remote shell.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.schedule import PostAction  # noqa: E402
from app.core.task import TaskState  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.storage.settings import Settings  # noqa: E402
from app.ui.controller import Controller  # noqa: E402
from app.ui.scheduler import QueueScheduler  # noqa: E402
from tests.server import FileServer  # noqa: E402

PAGE = (
    b"<html><body>"
    b'<a href="one.bin">one</a><a href="page2.html">more</a>'
    b'<img src="two.bin">'
    b"</body></html>"
)
PAGE2 = b'<html><body><a href="three.bin">three</a></body></html>'


def wait_for(app: QApplication, predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    return predicate()


def report(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<10} {detail}")
    return ok


def check_schedule(app, controller, db, server, out: Path, files: int, concurrent: int) -> bool:
    queue_id = controller.create_queue("Night", max_concurrent=concurrent)
    for i in range(files):
        controller.add(server.url_for(f"night/file{i}.bin"), save_dir=out, queue_id=queue_id)

    start = (datetime.now() - timedelta(minutes=2)).strftime("%H:%M")
    db.save_schedule(queue_id, start_at=start, on_complete=PostAction.NONE.value)

    scheduler = QueueScheduler(controller, db)
    finished: list[int] = []
    controller.queueFinished.connect(finished.append)

    scheduler.tick()
    started = controller.is_queue_running(queue_id)
    running_now = sum(1 for i in controller.queue_items(queue_id) if i.is_live)
    ok_start = report(
        "schedule", started,
        f"start time {start} (2 min in the past) fired on the first tick",
    )
    ok_limit = report(
        "queue", running_now <= concurrent,
        f"{running_now} of {files} files running at once (limit {concurrent})",
    )

    wait_for(app, lambda: bool(finished), timeout=90)
    done = [i for i in controller.queue_items(queue_id) if i.state is TaskState.COMPLETED]
    ok_done = report(
        "drain", finished == [queue_id] and len(done) == files,
        f"{len(done)}/{files} completed, queueFinished fired {len(finished)}x",
    )
    scheduler.stop()
    return ok_start and ok_limit and ok_done


def check_grabber(app, controller, server, out: Path) -> bool:
    from app.core.http_client import RequestSpec, build_client
    from app.grabber.crawler import CrawlOptions, crawl

    async def job():
        url = server.url_for("site/index.html")
        async with build_client(RequestSpec(url=url)) as client:
            return await crawl(client, url, CrawlOptions(depth=1, extensions=("bin",)))

    result = controller.engine.run_coroutine(job()).result(timeout=60)
    for found in result.files:
        controller.add(found.url, save_dir=out / "grab", referer=found.referer)

    expected = len(result.files)
    wait_for(
        app,
        lambda: len(list((out / "grab").glob("*.bin"))) >= expected,
        timeout=90,
    )
    downloaded = sorted(p.name for p in (out / "grab").glob("*.bin"))
    return report(
        "grabber", expected == 3 and len(downloaded) == 3,
        f"found {expected} files on {result.pages_visited} pages, downloaded {downloaded}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=3)
    parser.add_argument("--concurrent", type=int, default=2)
    parser.add_argument("--size-kb", type=int, default=512)
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    out = Path(args.work_dir or tempfile.mkdtemp(prefix="boltdown-p5-"))
    (out / "grab").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BOLTDOWN_HOME", str(out / "home"))

    app = QApplication.instance() or QApplication([])
    server = FileServer().start()
    payload = os.urandom(args.size_kb * 1024)
    for i in range(args.files):
        server.add_file(f"night/file{i}.bin", payload)
    server.add_file("site/index.html", PAGE)
    server.add_file("site/page2.html", PAGE2)
    for name in ("site/one.bin", "site/two.bin", "site/three.bin"):
        server.add_file(name, payload)

    db = Database(out / "p5.db")
    settings = Settings(db)
    settings.set("download_dir", str(out))
    controller = Controller(db, settings)
    controller.start()

    try:
        ok = check_schedule(app, controller, db, server, out, args.files, args.concurrent)
        ok = check_grabber(app, controller, server, out) and ok
    finally:
        controller.shutdown()
        db.close()
        server.stop()
    print(f"\nworking directory: {out}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
