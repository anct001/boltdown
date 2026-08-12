"""Queue orchestration, the scheduler timer, and post-download actions."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.schedule import PostAction, WEEKDAYS  # noqa: E402
from app.core.task import TaskState  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.storage.settings import Settings  # noqa: E402
from app.ui.controller import Controller, DownloadItem  # noqa: E402
from app.ui.scheduler import QueueScheduler  # noqa: E402
from app.util import power  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def stack(qapp, tmp_path):
    db = Database(tmp_path / "queues.db")
    settings = Settings(db)
    settings.set("download_dir", str(tmp_path / "dl"))
    controller = Controller(db, settings)
    yield controller, settings, db
    db.close()


class FakeEngine:
    """Records submissions instead of downloading; the queue logic is the test."""

    def __init__(self) -> None:
        self.submitted: list[int] = []
        self.paused: list[int] = []
        self._next = 100

    def submit(self, request) -> int:
        self._next += 1
        self.submitted.append(self._next)
        return self._next

    def pause(self, engine_id: int) -> None:
        self.paused.append(engine_id)

    def cancel(self, engine_id: int) -> None:
        self.paused.append(engine_id)

    def state(self, engine_id: int):
        return None

    def set_speed_limit(self, limit) -> None:
        pass


@pytest.fixture
def queued(stack):
    """A queue holding three paused downloads and a controller that fakes the engine."""
    controller, _settings, db = stack
    controller.engine = FakeEngine()
    queue_id = controller.create_queue("Night", max_concurrent=2)
    items = [
        controller.add(f"https://example.com/f{i}.zip", queue_id=queue_id)
        for i in range(3)
    ]
    return controller, db, queue_id, items


def finish(controller: Controller, item: DownloadItem, state=TaskState.COMPLETED) -> None:
    """Pretend the engine finished this item and let the queue react."""
    item.state = state
    item.speed = 0.0
    controller.itemChanged.emit(item)
    controller.pump_queues()


def test_adding_to_a_queue_does_not_start_it(queued):
    controller, _db, queue_id, items = queued
    assert [i.state for i in items] == [TaskState.PAUSED] * 3
    assert controller.engine.submitted == []
    assert not controller.is_queue_running(queue_id)
    assert [i.db_id for i in controller.queue_items(queue_id)] == [i.db_id for i in items]


def test_a_running_queue_respects_its_concurrency(queued):
    controller, _db, queue_id, items = queued
    controller.start_queue(queue_id)
    assert controller.is_queue_running(queue_id)
    # Two at a time, in the order they were added.
    assert [i.state for i in items] == [TaskState.QUEUED, TaskState.QUEUED, TaskState.PAUSED]

    finish(controller, items[0])
    assert items[2].state is TaskState.QUEUED
    assert len(controller.engine.submitted) == 3


def test_a_queue_that_drains_reports_that_it_finished(queued):
    controller, _db, queue_id, items = queued
    finished: list[int] = []
    controller.queueFinished.connect(finished.append)

    controller.start_queue(queue_id)
    for item in items:
        finish(controller, item)

    assert finished == [queue_id]
    assert not controller.is_queue_running(queue_id)


def test_a_failed_download_does_not_spin_the_queue(queued):
    controller, _db, queue_id, items = queued
    controller.start_queue(queue_id)
    finish(controller, items[0], TaskState.ERROR)
    submitted = len(controller.engine.submitted)
    controller.pump_queues()
    controller.pump_queues()
    assert len(controller.engine.submitted) == submitted, "an error was retried forever"


def test_stopping_a_queue_pauses_its_downloads_but_keeps_them_pending(queued):
    controller, _db, queue_id, items = queued
    controller.start_queue(queue_id)
    live = [i for i in items if i.state is TaskState.QUEUED]
    for item in live:
        item.state = TaskState.DOWNLOADING

    controller.stop_queue(queue_id)
    assert not controller.is_queue_running(queue_id)
    assert controller.engine.paused, "the running downloads were not paused"
    for item in live:
        item.state = TaskState.PAUSED
        assert item.is_pending

    controller.start_queue(queue_id)
    assert items[0].state is TaskState.QUEUED


def test_a_manual_pause_survives_the_queue_moving_on(queued):
    controller, _db, queue_id, items = queued
    controller.start_queue(queue_id)
    items[1].state = TaskState.DOWNLOADING
    controller.pause_item(items[1].db_id)
    items[1].state = TaskState.PAUSED
    assert items[1].manual_pause and not items[1].is_pending

    finish(controller, items[0])
    assert items[1].state is TaskState.PAUSED, "a hand-paused file was restarted"
    assert items[2].state is TaskState.QUEUED


def test_an_empty_queue_never_claims_to_have_finished(stack):
    controller, _settings, _db = stack
    controller.engine = FakeEngine()
    finished: list[int] = []
    controller.queueFinished.connect(finished.append)
    queue_id = controller.create_queue("Empty")
    controller.start_queue(queue_id)
    assert finished == []


def test_moving_a_download_between_queues(queued):
    controller, db, queue_id, items = queued
    other = controller.create_queue("Day")
    controller.assign_queue(items[2].db_id, other)
    assert [i.db_id for i in controller.queue_items(queue_id)] == [
        items[0].db_id, items[1].db_id
    ]
    assert db.get_download(items[2].db_id)["queue_id"] == other

    controller.assign_queue(items[2].db_id, None)
    assert controller.queue_items(other) == []


def test_deleting_a_queue_keeps_the_downloads(queued):
    controller, db, queue_id, items = queued
    controller.delete_queue(queue_id)
    assert controller.queues() == []
    assert all(i.queue_id is None for i in items)
    assert db.get_download(items[0].db_id) is not None


# ----------------------------------------------------------------- scheduler


def test_scheduler_starts_and_stops_a_queue_on_time(queued):
    controller, db, queue_id, _items = queued
    schedule_id = db.save_schedule(queue_id, start_at="02:00", stop_at="04:00")
    scheduler = QueueScheduler(controller, db)

    scheduler.tick(datetime(2026, 8, 12, 1, 59))
    assert not controller.is_queue_running(queue_id)

    scheduler.tick(datetime(2026, 8, 12, 2, 0))
    assert controller.is_queue_running(queue_id)
    assert db.get_schedule(queue_id)["last_run"] is not None

    # A second tick inside the window must not restart anything.
    controller.stop_queue(queue_id)
    scheduler.tick(datetime(2026, 8, 12, 3, 0))
    assert not controller.is_queue_running(queue_id)

    controller.start_queue(queue_id)
    scheduler.tick(datetime(2026, 8, 12, 4, 1))
    assert not controller.is_queue_running(queue_id)
    assert schedule_id == db.get_schedule(queue_id)["id"]


def test_scheduler_skips_days_outside_the_mask(queued):
    controller, db, queue_id, _items = queued
    db.save_schedule(queue_id, start_at="08:00", days_mask=WEEKDAYS)
    scheduler = QueueScheduler(controller, db)
    scheduler.tick(datetime(2026, 8, 15, 9, 0))  # Saturday
    assert not controller.is_queue_running(queue_id)
    scheduler.tick(datetime(2026, 8, 17, 9, 0))  # Monday
    assert controller.is_queue_running(queue_id)


def test_scheduler_asks_for_the_post_action_when_the_queue_drains(queued):
    controller, db, queue_id, items = queued
    db.save_schedule(queue_id, start_at="02:00", on_complete="shutdown")
    scheduler = QueueScheduler(controller, db)
    requested: list[tuple[str, int]] = []
    scheduler.actionRequested.connect(lambda action, qid: requested.append((action, qid)))

    scheduler.tick(datetime(2026, 8, 12, 2, 30))
    for item in items:
        finish(controller, item)

    assert requested == [("shutdown", queue_id)]


def test_no_action_is_requested_without_a_schedule(queued):
    controller, db, queue_id, items = queued
    scheduler = QueueScheduler(controller, db)
    requested: list[tuple[str, int]] = []
    scheduler.actionRequested.connect(lambda action, qid: requested.append((action, qid)))
    controller.start_queue(queue_id)
    for item in items:
        finish(controller, item)
    assert requested == []


# --------------------------------------------------------------------- power


def test_power_commands_are_platform_appropriate():
    argv = power.command_for(PostAction.SHUTDOWN, delay=120)
    assert argv is not None
    if os.name == "nt":
        assert argv[:2] == ["shutdown", "/s"] and argv[-1] == "120"
        assert power.command_for(PostAction.HIBERNATE) == ["shutdown", "/h"]
        assert "powrprof" in " ".join(power.command_for(PostAction.SLEEP))
    assert power.command_for(PostAction.NONE) is None
    assert power.command_for(PostAction.EXIT) is None


def test_apply_runs_the_command_through_the_injected_runner():
    calls: list[list[str]] = []
    assert power.apply(PostAction.SHUTDOWN, delay=30, runner=lambda argv: calls.append(list(argv)))
    assert calls and "shutdown" in calls[0][0]

    assert power.cancel(runner=lambda argv: calls.append(list(argv)))
    assert calls[-1][-1] in ("/a", "-c")


def test_exit_calls_back_instead_of_running_a_command():
    quit_calls: list[bool] = []
    ran: list[list[str]] = []
    assert power.apply(
        PostAction.EXIT, runner=lambda argv: ran.append(list(argv)),
        on_exit=lambda: quit_calls.append(True),
    )
    assert quit_calls == [True] and ran == []
    assert power.apply(PostAction.NONE, runner=lambda argv: ran.append(list(argv))) is False


def test_a_failing_runner_is_reported_not_raised():
    def boom(argv):
        raise OSError("no such command")

    assert power.apply(PostAction.SHUTDOWN, runner=boom) is False
    assert power.cancel(runner=boom) is False
