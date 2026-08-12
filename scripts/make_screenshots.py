"""Render the screenshots in docs/ from the real widgets.

    python scripts/make_screenshots.py [--theme dark|light] [--out docs/screenshots]

Downloads really are running while the shots are taken - the progress bars,
speed graph and segment map show live data from a local server, not mock-ups.
Windows are parked off-screen instead of using the `offscreen` Qt platform,
which has no fonts and would render every label as boxes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import Future
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OFFSCREEN = (-4000, -4000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", default="dark", choices=("dark", "light"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "docs" / "screenshots"))
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    work = Path(args.work_dir) if args.work_dir else Path(
        os.environ.get("TEMP", ".")
    ) / "idmclone-shots"
    downloads = work / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    os.environ["IDMCLONE_HOME"] = str(work / "home")

    from PySide6.QtWidgets import QApplication, QTabWidget

    from app.grabber.crawler import CrawlResult, FoundFile
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui import i18n, theme
    from app.ui.add_url_dialog import AddUrlDialog
    from app.ui.batch_dialog import BatchDialog
    from app.ui.controller import Controller
    from app.ui.grabber_dialog import GrabberDialog
    from app.ui.history_dialog import HistoryDialog
    from app.ui.main_window import MainWindow
    from app.ui.progress_dialog import ProgressDialog
    from app.ui.queue_dialog import SchedulerDialog
    from app.core.schedule import PostAction, WEEKDAYS
    from tests.server import FileServer

    app = QApplication.instance() or QApplication([])
    i18n.set_language("vi")
    theme.apply(app, args.theme)

    server = FileServer().start()
    server.add_file("demo/BigBuckBunny-1080p.mkv", os.urandom(120 * 1024 * 1024))
    server.add_file("demo/ban-ke-hoach.pdf", os.urandom(8 * 1024 * 1024))
    base = server.base_url

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db = Database(work / "home" / "shots.db")
    settings = Settings(db)
    settings.update({
        "download_dir": str(downloads),
        "use_categories": False,
        "speed_limit": 900 * 1024,   # keep the bars visibly moving
        "max_concurrent": 3,
        "connections": 8,
        "theme": args.theme,
    })
    controller = Controller(db, settings)
    controller.start()

    window = MainWindow(controller, settings)
    window.resize(1100, 640)
    window.move(*OFFSCREEN)
    window.show()

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)

    def shot(widget, name: str) -> None:
        app.processEvents()
        path = out / f"{name}.png"
        widget.grab().save(str(path))
        print(f"  {path.name:<24} {path.stat().st_size // 1024} KB")

    live = controller.add(f"{base}/demo/BigBuckBunny-1080p.mkv?slow=4")
    controller.add(f"{base}/demo/ban-ke-hoach.pdf?slow=2")
    controller.add(f"{base}/demo/ban-ke-hoach.pdf", filename="báo cáo quý 4.pdf")
    controller.add(f"{base}/demo/phim-tap-2.mkv", start_now=False)
    pump(9)

    print("screenshots:")
    shot(window, "main-window")

    progress = ProgressDialog(controller, controller.item(live.db_id), window)
    progress.resize(560, 520)
    progress.move(*OFFSCREEN)
    progress.show()
    pump(4)
    shot(progress, "progress-dialog")
    progress.close()

    add = AddUrlDialog(settings, url=f"{base}/demo/BigBuckBunny-1080p.mkv", parent=window)
    add.move(*OFFSCREEN)
    add.show()
    pump(0.5)
    shot(add, "add-url")
    add.close()

    batch = BatchDialog(controller, settings, window)
    batch.text.setPlainText(
        "https://example.com/ban-tin/tap[001-024].mp4\n"
        "https://example.com/tai-lieu/huong-dan.pdf"
    )
    batch.move(*OFFSCREEN)
    batch.show()
    pump(0.6)
    shot(batch, "batch")
    batch.close()

    night = controller.create_queue("Tải ban đêm", max_concurrent=2)
    controller.create_queue("Phim bộ")
    db.save_schedule(night, start_at="02:00", stop_at="06:00", days_mask=WEEKDAYS,
                     on_complete=PostAction.SHUTDOWN.value)
    scheduler = SchedulerDialog(controller, db, window)
    scheduler.move(*OFFSCREEN)
    scheduler.show()
    pump(0.6)
    shot(scheduler, "scheduler")
    scheduler.close()

    grabber = GrabberDialog(controller, settings, window)
    grabber.url.setText("https://example.com/thu-vien-anh/")
    grabber.preset.setCurrentIndex(1)
    grabber.depth.setValue(2)
    future: Future = Future()
    future.set_result(CrawlResult(
        files=[
            FoundFile(url=f"https://example.com/anh/{name}", name=name,
                      referer="https://example.com/thu-vien-anh/",
                      extension=name.rsplit(".", 1)[1], depth=1)
            for name in ("bien-2026.jpg", "nui-doi.jpg", "pho-co.png",
                         "hoang-hon.jpg", "ca-phe.png", "cau-vang.jpg")
        ],
        pages_visited=4,
    ))
    grabber._on_finished(future)
    grabber.move(*OFFSCREEN)
    grabber.show()
    pump(0.6)
    shot(grabber, "site-grabber")
    grabber.close()

    pump(10)  # let a download or two finish so the history has rows
    history = HistoryDialog(controller, db, window)
    history.move(*OFFSCREEN)
    history.show()
    pump(0.6)
    shot(history, "history")
    history.close()

    from app.ui.settings_dialog import SettingsDialog

    options = SettingsDialog(settings, window)
    options.move(*OFFSCREEN)
    options.show()
    pump(0.5)
    tabs = options.findChild(QTabWidget)
    tabs.setCurrentIndex(3)  # Clipboard
    pump(0.3)
    shot(options, "settings")
    options.close()

    controller.shutdown()
    db.close()
    server.stop()
    print(f"\nwrote {args.theme} screenshots to {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
