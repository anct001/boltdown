"""The remote-control server's calls, carried to the GUI thread.

The web server answers on threads of its own, while the controller and its
Qt objects belong to the GUI thread. Every call is posted there and the
server thread waits for the answer (`BlockingQueuedConnection`); a modal
dialog does not get in the way, since it runs the event loop too.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from .. import __version__
from ..core.task import TaskState
from ..remote.server import RemoteServer, new_token
from ..util.log import get_logger
from .i18n import language, tr

log = get_logger(__name__)


class _Job:
    __slots__ = ("work", "result", "error")

    def __init__(self, work: Callable[[], Any]) -> None:
        self.work = work
        self.result: Any = None
        self.error: BaseException | None = None


class ControllerApi(QObject):
    """`RemoteApi` for the real window."""

    _posted = Signal(object)

    def __init__(self, controller, add: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.controller = controller
        #: how a URL is added - the window's way, so the usual rules apply
        self._add = add or (lambda url: controller.add(url))
        self._posted.connect(self._run, Qt.ConnectionType.BlockingQueuedConnection)

    @Slot(object)
    def _run(self, job: _Job) -> None:
        try:
            job.result = job.work()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            job.error = exc

    def _on_gui(self, work: Callable[[], Any]) -> Any:
        if QThread.currentThread() is self.thread():
            return work()
        job = _Job(work)
        self._posted.emit(job)
        if job.error is not None:
            raise job.error
        return job.result

    # ------------------------------------------------------------ RemoteApi

    def status(self) -> dict[str, Any]:
        def work():
            return {
                "app": "Boltdown",
                "version": __version__,
                "speed": self.controller.total_speed(),
                "active": self.controller.active_count(),
                "lang": language(),
                "words": _words(),
            }
        return self._on_gui(work)

    def items(self) -> list[dict[str, Any]]:
        def work():
            return [
                {
                    "id": item.db_id,
                    "filename": item.filename,
                    "state": item.state.value,
                    "size": item.size,
                    "downloaded": item.downloaded,
                    "percent": round(item.percent, 1),
                    "speed": item.speed,
                    "eta": item.eta,
                    "error": item.error,
                    "added_at": item.added_at,
                }
                for item in sorted(
                    self.controller.items(), key=lambda i: i.added_at, reverse=True
                )
            ]
        return self._on_gui(work)

    def add(self, url: str) -> None:
        self._on_gui(lambda: self._add(url))

    def act(self, item_id: int, action: str) -> bool:
        def work():
            item = self.controller.item(item_id)
            if item is None:
                return False
            if action == "pause":
                self.controller.pause_item(item_id)
            elif action == "resume":
                if item.state is not TaskState.COMPLETED:
                    self.controller.start_item(item_id)
            elif action == "remove":
                # Off the list only: deleting files is not a phone's job.
                self.controller.remove(item_id, delete_file=False)
            return True
        return self._on_gui(work)

    def act_all(self, action: str) -> None:
        if action == "pause":
            self._on_gui(self.controller.pause_all)
        elif action == "resume":
            self._on_gui(self.controller.resume_all)


def _words() -> dict[str, str]:
    return {
        "pause": tr("Pause"),
        "resume": tr("Resume"),
        "remove": tr("Remove"),
        "pause_all": tr("Pause all"),
        "resume_all": tr("Resume all"),
        "empty": tr("No downloads"),
        "no_token": tr("Open the link shown in Boltdown's settings."),
        **{f"state_{state.value}": tr(state.value) for state in TaskState},
    }


class RemoteControl:
    """Starts and stops the server to match the settings."""

    def __init__(self, settings, controller, add: Callable[[str], None] | None = None) -> None:
        self.settings = settings
        self.api = ControllerApi(controller, add)
        self.server: RemoteServer | None = None
        self.error: str | None = None

    def token(self) -> str:
        token = str(self.settings.get("remote_token") or "")
        if not token:
            token = new_token()
            self.settings.set("remote_token", token)
        return token

    def apply(self) -> None:
        """On, off, or restarted on a new port or token."""
        wanted = bool(self.settings.get("remote_enabled"))
        port = int(self.settings.get("remote_port") or 0)
        token = self.token() if wanted else ""
        current = self.server
        if current is not None and (
            not wanted or current.token != token or (port and current.port != port)
        ):
            current.stop()
            self.server = None
        if wanted and self.server is None:
            server = RemoteServer(self.api, token, port=port)
            try:
                server.start()
            except OSError as exc:
                self.error = str(exc)
                log.warning("remote control could not start on port %s: %s", port, exc)
                return
            self.server = server
        self.error = None

    def links(self) -> list[str]:
        return self.server.links() if self.server is not None else []

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
