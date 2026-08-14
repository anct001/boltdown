"""GUI entry point (also the single-instance owner and IPC listener)."""

from __future__ import annotations

import logging
import sys
import time

from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .ipc import endpoint
from .ipc.protocol import TYPE_SHOW
from .core import categories
from .storage.db import Database
from .storage.settings import Settings
from .ui import i18n, icons, theme
from .ui.controller import Controller
from .ui.ipc_bridge import IpcBridge
from .ui.main_window import MainWindow
from .ui.tray import TrayIcon
from .util.log import get_logger, setup_logging

log = get_logger(__name__)


#: how patient to be with an instance that is alive but slow to answer
BUSY_TIMEOUT = 3.0
BUSY_RETRIES = (0.4, 1.0)


def _forward_to_running_instance(urls: list[str]) -> bool:
    """Hand our arguments to an app that is already up, then bow out.

    A failed ping used to mean "start a second copy". It does not: the probe
    times out after two seconds, which Windows can spend enumerating a phone
    that was just plugged in, and the user then gets a second window - then a
    third, because each new instance republishes the endpoint. Reported as
    "lots of windows open when I connect my phone by cable".

    So a silent instance is asked again, and if it is demonstrably alive we
    refuse to open another window at all. A missed hand-over is a smaller
    failure than a duplicate application.
    """
    message = {"type": TYPE_SHOW, "urls": urls}
    reply = endpoint.send(message)
    if reply is not None and reply.get("ok", False):
        return True
    if not endpoint.owner_alive():
        return False

    for delay in BUSY_RETRIES:
        time.sleep(delay)
        reply = endpoint.send(message, timeout=BUSY_TIMEOUT)
        if reply is not None and reply.get("ok", False):
            return True
        if not endpoint.owner_alive():
            return False       # it exited while we waited; we are the app now

    log.warning(
        "an instance is running but did not answer; not opening a second window"
    )
    return True


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
    app.setApplicationName("Boltdown")
    app.setOrganizationName("Boltdown")
    app.setWindowIcon(icons.app_icon())
    # The tray icon keeps the app alive after the window is closed.
    app.setQuitOnLastWindowClosed(False)

    db = Database()
    settings = Settings(db)
    i18n.set_language(settings.language)
    # The user's own extension table, if they wrote one.
    categories.set_categories(categories.parse_categories(settings.get("categories")))
    theme.apply(app, settings.get("theme"))
    # "Follow Windows" has to keep following it: repaint when the user flips
    # their system between light and dark while the app is open.
    def follow_system(_scheme) -> None:
        theme.apply(app, settings.get("theme"))
        window.refresh_icons()

    app.styleHints().colorSchemeChanged.connect(follow_system)

    controller = Controller(db, settings)
    controller.start()

    window = MainWindow(controller, settings)
    if QSystemTrayIcon.isSystemTrayAvailable():
        window.tray = TrayIcon(window)
        window.tray.show()

    bridge = IpcBridge()
    bridge.downloadRequested.connect(window.handle_ipc_download)
    bridge.showRequested.connect(window.handle_ipc_show)
    # Read-only view and control, for `boltdown-cli --remote-*`. The bridge
    # runs both on the GUI thread and waits for the answer.
    bridge.snapshot = window.remote_snapshot
    bridge.control = window.remote_control
    server = endpoint.IpcServer(bridge.handle)
    try:
        server.start()
    except OSError as exc:
        log.error("could not start the IPC listener: %s", exc)

    # A reinstall takes the native-messaging registration with it, and the
    # browser then quietly stops handing downloads over. Put it back.
    if settings.get("extension_id"):
        from .ipc import register

        register.repair([settings.get("extension_id"), register.DEFAULT_GECKO_ID])

    if start_hidden and window.tray is not None:
        log.info("started minimised to the tray")
    else:
        window.show()
    for url in urls:
        controller.add(url)
    if settings.get("resume_on_start"):
        # Everything that was mid-flight when the app last closed.
        controller.resume_all()

    try:
        return app.exec()
    finally:
        server.stop()
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
