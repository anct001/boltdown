"""The engine: one asyncio event loop on a background thread.

The GUI (P2) will live on the Qt main thread and must never block, so every
public method here is safe to call from another thread and returns quickly.
Work is handed to the loop with `run_coroutine_threadsafe` / `call_soon_threadsafe`,
and progress comes back through the `on_event` callback (invoked *on the loop
thread* - listeners are responsible for marshalling to their own thread).
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..util.log import get_logger
from .ratelimit import TokenBucket
from .task import DownloadRequest, TaskRunner, TaskSnapshot, TaskState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..media.runner import MediaTaskRunner

log = get_logger(__name__)

EventListener = Callable[["EngineEvent"], None]


@dataclass(slots=True)
class EngineEvent:
    type: str
    task_id: int
    snapshot: TaskSnapshot | None = None


class Engine:
    def __init__(
        self,
        *,
        max_concurrent: int = 3,
        speed_limit: int | None = None,
        on_event: EventListener | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self._on_event = on_event
        self._global_bucket = TokenBucket(speed_limit)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._idle = threading.Event()
        self._idle.set()

        self._ids = itertools.count(1)
        self._requests: dict[int, DownloadRequest] = {}
        #: TaskRunner or MediaTaskRunner - both expose the same four methods
        self._runners: dict[int, "TaskRunner | MediaTaskRunner"] = {}
        self._states: dict[int, TaskState] = {}
        self._supervisors: dict[int, asyncio.Task] = {}
        self._active: set[int] = set()
        self._sem: asyncio.Semaphore | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="idmclone-engine", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def stop(self, timeout: float = 15.0) -> None:
        """Pause everything, flush resume metadata, then shut the loop down."""
        if self._loop is None or self._thread is None:
            return
        self.pause_all()
        self.wait_idle(timeout=timeout)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self._ready.clear()

    def __enter__(self) -> "Engine":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ----------------------------------------------------------------- public

    def submit(self, request: DownloadRequest) -> int:
        """Queue a download and return its id. Non-blocking."""
        task_id = next(self._ids)
        with self._lock:
            self._requests[task_id] = request
            self._states[task_id] = TaskState.QUEUED
            self._active.add(task_id)
            self._idle.clear()
        self._emit(EngineEvent("added", task_id))
        self._schedule(task_id)
        return task_id

    def pause(self, task_id: int) -> None:
        runner = self._runners.get(task_id)
        if runner is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(runner.request_pause)

    def cancel(self, task_id: int) -> None:
        runner = self._runners.get(task_id)
        if runner is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(runner.request_cancel)

    def pause_all(self) -> None:
        for task_id in list(self._runners):
            self.pause(task_id)

    def resume(self, task_id: int) -> None:
        """Restart a paused/failed task; on-disk metadata does the rest."""
        with self._lock:
            state = self._states.get(task_id)
            if task_id not in self._requests:
                raise KeyError(task_id)
            if state in (TaskState.DOWNLOADING, TaskState.PROBING, TaskState.QUEUED):
                return
            self._states[task_id] = TaskState.QUEUED
            self._active.add(task_id)
            self._idle.clear()
        self._schedule(task_id)

    def state(self, task_id: int) -> TaskState | None:
        return self._states.get(task_id)

    def snapshot(self, task_id: int) -> TaskSnapshot | None:
        runner = self._runners.get(task_id)
        return runner.snapshot() if runner else None

    def snapshots(self) -> list[TaskSnapshot]:
        return [r.snapshot() for r in list(self._runners.values())]

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no task is running. Returns False on timeout."""
        return self._idle.wait(timeout=timeout)

    def run_coroutine(self, coro):
        """Run `coro` on the engine loop; returns a `concurrent.futures.Future`.

        Lets other parts of the app (the site grabber, for one) borrow the
        event loop instead of starting a second one just to make HTTP calls.
        """
        if self._loop is None:
            raise RuntimeError("engine is not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def set_event_listener(self, listener: EventListener | None) -> None:
        """Detach before tearing down a GUI: the loop thread may still emit."""
        self._on_event = listener

    def set_speed_limit(self, limit: int | None) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._global_bucket.set_rate, limit)
        else:
            self._global_bucket.set_rate(limit)

    # --------------------------------------------------------------- internals

    def _schedule(self, task_id: int) -> None:
        if self._loop is None:
            raise RuntimeError("engine is not started")
        asyncio.run_coroutine_threadsafe(self._spawn(task_id), self._loop)

    async def _spawn(self, task_id: int) -> None:
        supervisor = asyncio.create_task(
            self._supervise(task_id), name=f"supervise-{task_id}"
        )
        self._supervisors[task_id] = supervisor

    def _make_runner(self, task_id: int, request: DownloadRequest):
        """Plain files go to `TaskRunner`; playlists and pages to the media one."""
        if request.is_media:
            from ..media.runner import MediaTaskRunner

            factory = MediaTaskRunner
        else:
            factory = TaskRunner
        return factory(
            task_id,
            request,
            global_bucket=self._global_bucket,
            on_event=self._on_task_event,
        )

    async def _supervise(self, task_id: int) -> None:
        assert self._sem is not None
        request = self._requests[task_id]
        runner = self._make_runner(task_id, request)
        self._runners[task_id] = runner
        try:
            async with self._sem:
                state = await runner.run()
        except asyncio.CancelledError:
            self._states[task_id] = TaskState.PAUSED
            raise
        except Exception as exc:  # pragma: no cover - runner already traps its own
            log.exception("supervisor for task %d failed", task_id)
            self._states[task_id] = TaskState.ERROR
            self._emit(EngineEvent("error", task_id, runner.snapshot()))
            _ = exc
        else:
            self._states[task_id] = state
        finally:
            self._supervisors.pop(task_id, None)
            self._mark_inactive(task_id)

    def _mark_inactive(self, task_id: int) -> None:
        with self._lock:
            self._active.discard(task_id)
            if not self._active:
                self._idle.set()

    def _on_task_event(self, event: str, snapshot: TaskSnapshot) -> None:
        self._states[snapshot.id] = snapshot.state
        self._emit(EngineEvent(event, snapshot.id, snapshot))

    def _emit(self, event: EngineEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # pragma: no cover - listener bugs must not kill the loop
            log.exception("engine event listener raised")
