"""GUI entry point (also the single-instance owner and IPC listener)."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .ipc import endpoint
from .ipc.protocol import TYPE_SHOW
from .storage.db import Database
from .storage.settings import Settings
from .ui import i18n, icons
from .ui.controller import Controller
from .ui.ipc_bridge import IpcBridge
from .ui.main_window import MainWindow
from .ui.tray import TrayIcon
from .util.log import get_logger, setup_logging

log = get_logger(__name__)


def _forward_to_running_instance(urls: list[str]) -> bool:
    """Hand our arguments to an app that is already up, then bow out."""
    reply = endpoint.send({"type": TYPE_SHOW, "urls": urls})
    return reply is not None and reply.get("ok", False)


#: started by the Run key at login - come up in the tray, not in the way
TRAY_FLAGS = ("--tray", "--minimized")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging(level=logging.WARNING, console=False)

    start_hidden = any(flag in argv for flag in TRAY_FLAGS)
    urls = [a for a in argv if a.startswith(("http://", "https://"))]
    if _forward_to_running_instance(urls):
        log.info("another instance is running; handed over %d URL(s)", len(urls))
        return 0

    app = QApplication(sys.argv[:1])
    app.setApplicationName("IDMClone")
    app.setOrganizationName("IDMClone")
    app.setWindowIcon(icons.app_icon())
    # The tray icon keeps the app alive after the window is closed.
    app.setQuitOnLastWindowClosed(False)

    db = Database()
    settings = Settings(db)
    i18n.set_language(settings.language)

    controller = Controller(db, settings)
    controller.start()

    window = MainWindow(controller, settings)
    if QSystemTrayIcon.isSystemTrayAvailable():
        window.tray = TrayIcon(window)
        window.tray.show()

    bridge = IpcBridge()
    bridge.downloadRequested.connect(window.handle_ipc_download)
    bridge.showRequested.connect(window.handle_ipc_show)
    server = endpoint.IpcServer(bridge.handle)
    try:
        server.start()
    except OSError as exc:
        log.error("could not start the IPC listener: %s", exc)

    if start_hidden and window.tray is not None:
        log.info("started minimised to the tray")
    else:
        window.show()
    for url in urls:
        controller.add(url)

    try:
        return app.exec()
    finally:
        server.stop()
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
