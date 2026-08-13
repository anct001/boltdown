"""Turns saved schedules into queue start/stop calls.

The clock lives here and nowhere else: `tick()` takes `now` as an argument so
the tests drive it directly, and a `QTimer` supplies the real one every few
seconds. Everything it decides comes from `app.core.schedule`, which has no
idea Qt exists.

A schedule's `on_complete` action fires when *its* queue drains, not when the
whole application goes idle - two queues can then have different endings.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from ..core.schedule import PostAction, Schedule
from ..storage.db import Database
from ..util.log import get_logger
from .controller import Controller

log = get_logger(__name__)

TICK_MS = 15_000


def schedule_from_row(row) -> Schedule:
    return Schedule(
        id=row["id"],
        queue_id=row["queue_id"],
        enabled=bool(row["enabled"]),
        start_at=row["start_at"],
        stop_at=row["stop_at"],
        days_mask=row["days_mask"],
        on_complete=row["on_complete"] or PostAction.NONE.value,
        last_run=row["last_run"],
    )


class QueueScheduler(QObject):
    """Watches the clock and the queues; asks the window to act."""

    #: a queue finished and its schedule asks for shutdown / hibernate / exit
    actionRequested = Signal(str, int)  # action value, queue id
    started = Signal(int)
    stopped = Signal(int)

    def __init__(
        self, controller: Controller, db: Database, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.db = db
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._applied_limit: int | None = -1  # -1 = nothing applied yet
        controller.queueFinished.connect(self._on_queue_finished)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.tick()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    # ------------------------------------------------------------------ logic

    def schedules(self) -> list[Schedule]:
        return [schedule_from_row(row) for row in self.db.list_schedules()]

    def bandwidth_schedule(self) -> tuple[Schedule, int | None] | None:
        """The saved "slow down between these hours" rule, if any."""
        saved = self.controller.settings.get("bandwidth_schedule")
        if not isinstance(saved, dict) or not saved.get("start"):
            return None
        schedule = Schedule(
            enabled=bool(saved.get("enabled", True)),
            start_at=saved.get("start"),
            stop_at=saved.get("stop"),
            days_mask=int(saved.get("days", 127)),
        )
        limit = saved.get("limit")
        return schedule, int(limit) if limit else None

    def apply_bandwidth(self, now: datetime) -> int | None:
        """Set the global speed limit for this moment; returns what was applied.

        Outside the window the user's normal limit comes back, so the rule is
        a temporary override rather than a second setting to keep in sync.
        """
        pair = self.bandwidth_schedule()
        if pair is None:
            return None
        schedule, limit = pair
        wanted = limit if schedule.covers(now) else self.controller.settings.speed_limit
        if wanted != self._applied_limit:
            log.info("bandwidth schedule: limit is now %s", wanted or "unlimited")
            self.controller.engine.set_speed_limit(wanted)
            self._applied_limit = wanted
        return wanted

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self.apply_bandwidth(now)
        for schedule in self.schedules():
            if schedule.queue_id is None:
                continue
            occurrence = schedule.due_start(now)
            if occurrence is not None:
                log.info(
                    "schedule %s starts queue %d (%s)",
                    schedule.id, schedule.queue_id, occurrence.isoformat(timespec="minutes"),
                )
                self.db.mark_schedule_run(schedule.id, occurrence.timestamp())
                self.controller.start_queue(schedule.queue_id)
                self.started.emit(schedule.queue_id)
                continue
            if schedule.due_stop(now) and self.controller.is_queue_running(
                schedule.queue_id
            ):
                log.info("schedule %s stops queue %d", schedule.id, schedule.queue_id)
                self.controller.stop_queue(schedule.queue_id)
                self.stopped.emit(schedule.queue_id)

    def _on_queue_finished(self, queue_id: int) -> None:
        row = self.db.get_schedule(queue_id)
        if row is None:
            return
        schedule = schedule_from_row(row)
        if schedule.action is PostAction.NONE:
            return
        log.info("queue %d finished, requesting %s", queue_id, schedule.action.value)
        self.actionRequested.emit(schedule.action.value, queue_id)
