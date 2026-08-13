"""Scheduling decisions and the queue/schedule tables."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.schedule import (
    EVERY_DAY,
    WEEKDAYS,
    PostAction,
    Schedule,
    days_of,
    describe_days,
    format_hhmm,
    mask_for,
    parse_hhmm,
)
from app.storage.db import SCHEMA_VERSION, Database

# 2026-08-12 is a Wednesday (weekday 2).
WED = datetime(2026, 8, 12, 9, 0)


def at(hour: int, minute: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, minute)


@pytest.mark.parametrize(
    "text, expected",
    [("02:30", (2, 30)), ("2:5", (2, 5)), ("23:59", (23, 59)), ("00:00", (0, 0))],
)
def test_parse_hhmm(text, expected):
    parsed = parse_hhmm(text)
    assert parsed is not None and (parsed.hour, parsed.minute) == expected


@pytest.mark.parametrize("text", ["", None, "nonsense", "24:00", "10:60", "10"])
def test_parse_hhmm_rejects_junk(text):
    assert parse_hhmm(text) is None


def test_format_and_mask_round_trip():
    assert format_hhmm(parse_hhmm("07:05")) == "07:05"
    assert days_of(mask_for([0, 6])) == [0, 6]
    assert describe_days(EVERY_DAY) == "Every day"
    assert describe_days(WEEKDAYS) == "Weekdays"
    assert describe_days(mask_for([1, 3])) == "Tue, Thu"


def test_a_schedule_fires_once_per_occurrence():
    schedule = Schedule(start_at="02:00")
    occurrence = schedule.due_start(at(2, 1))
    assert occurrence == at(2, 0)

    # Recording the run stops it from firing again the same day...
    schedule.last_run = occurrence.timestamp()
    assert schedule.due_start(at(2, 5)) is None
    assert schedule.due_start(at(23, 0)) is None
    # ...but tomorrow is a new occurrence.
    assert schedule.due_start(at(2, 30, day=13)) == at(2, 0, day=13)


def test_a_missed_schedule_still_runs_when_the_app_opens():
    """The machine was off at 02:00; starting it at 07:00 must still download."""
    schedule = Schedule(start_at="02:00")
    assert schedule.due_start(at(7, 0)) == at(2, 0)


def test_a_missed_schedule_is_skipped_once_its_window_closed():
    schedule = Schedule(start_at="02:00", stop_at="04:00")
    assert schedule.due_start(at(3, 0)) == at(2, 0)
    assert schedule.due_start(at(7, 0)) is None


def test_nothing_fires_before_the_start_time():
    assert Schedule(start_at="02:00").due_start(at(1, 59)) is None


def test_disabled_and_dayless_schedules_never_fire():
    assert Schedule(start_at="02:00", enabled=False).due_start(at(9, 0)) is None
    assert Schedule(start_at="02:00", days_mask=0).due_start(at(9, 0)) is None
    assert Schedule().due_start(at(9, 0)) is None


def test_day_mask_limits_the_occurrences():
    weekdays = Schedule(start_at="08:00", days_mask=WEEKDAYS)
    assert weekdays.due_start(at(9, 0)) == at(8, 0)          # Wednesday
    assert weekdays.due_start(at(9, 0, day=15)) is None      # Saturday
    assert weekdays.next_start(at(9, 0, day=15)) == at(8, 0, day=17)  # Monday


def test_an_overnight_window_stops_the_next_morning():
    schedule = Schedule(start_at="23:00", stop_at="02:00")
    occurrence = schedule.due_start(at(23, 30))
    assert occurrence == at(23, 0)
    schedule.last_run = occurrence.timestamp()
    assert not schedule.due_stop(at(1, 0, day=13))
    assert schedule.due_stop(at(2, 0, day=13))


def test_a_stop_needs_a_start_first():
    schedule = Schedule(start_at="01:00", stop_at="03:00")
    assert not schedule.due_stop(at(4, 0))
    schedule.last_run = at(1, 0).timestamp()
    assert schedule.due_stop(at(4, 0))
    assert not schedule.due_stop(at(2, 0))


def test_post_action_falls_back_to_none():
    assert Schedule(on_complete="shutdown").action is PostAction.SHUTDOWN
    assert Schedule(on_complete="nonsense").action is PostAction.NONE
    assert Schedule().action is PostAction.NONE


# ------------------------------------------------------------------- storage


def test_queue_crud(tmp_path):
    with Database(tmp_path / "q.db") as db:
        night = db.add_queue("Night", max_concurrent=2)
        main = db.add_queue("Main")
        assert [row["name"] for row in db.list_queues()] == ["Night", "Main"]

        db.update_queue(night, name="Ban đêm", max_concurrent=4)
        row = db.get_queue(night)
        assert row["name"] == "Ban đêm" and row["max_concurrent"] == 4
        db.update_queue(night, nonsense="ignored")  # unknown columns are dropped

        download = db.add_download(
            url="https://x/a.zip", filename="a.zip", save_path=".", queue_id=main
        )
        db.set_download_queue(download, night)
        assert db.get_download(download)["queue_id"] == night

        # Deleting a queue must not delete its downloads.
        db.delete_queue(night)
        assert db.get_download(download)["queue_id"] is None
        assert len(db.list_queues()) == 1


def test_schedule_is_saved_once_per_queue(tmp_path):
    with Database(tmp_path / "s.db") as db:
        queue = db.add_queue("Night")
        first = db.save_schedule(queue, start_at="02:00", days_mask=WEEKDAYS)
        second = db.save_schedule(
            queue, start_at="03:00", stop_at="05:00", on_complete="shutdown"
        )
        assert first == second, "a queue keeps a single schedule row"

        row = db.get_schedule(queue)
        assert row["start_at"] == "03:00" and row["stop_at"] == "05:00"
        assert row["on_complete"] == "shutdown"

        db.mark_schedule_run(first, 1_760_000_000.0)
        assert db.get_schedule(queue)["last_run"] == 1_760_000_000.0

        db.delete_schedule(queue)
        assert db.get_schedule(queue) is None


def test_deleting_a_queue_takes_its_schedule_with_it(tmp_path):
    with Database(tmp_path / "s2.db") as db:
        queue = db.add_queue("Night")
        db.save_schedule(queue, start_at="02:00")
        db.delete_queue(queue)
        assert db.list_schedules() == []


def test_a_v1_database_is_migrated(tmp_path):
    """Older files predate stop_at / last_run; they must not need deleting."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE queues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            max_concurrent INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, queue_id INTEGER,
            start_at TEXT, days_mask INTEGER NOT NULL DEFAULT 127,
            enabled INTEGER NOT NULL DEFAULT 1, on_complete TEXT);
        INSERT INTO queues(name) VALUES ('Night');
        INSERT INTO schedules(queue_id, start_at) VALUES (1, '02:00');
        """
    )
    conn.commit()
    conn.close()

    with Database(path) as db:
        columns = {row["name"] for row in db.query("PRAGMA table_info(schedules)")}
        assert {"stop_at", "last_run"} <= columns
        assert db.get_schedule(1)["start_at"] == "02:00"
        # A v1 file walks through every later step in one go.
        assert db.query("SELECT value FROM meta WHERE key='schema_version'")[0][
            "value"
        ] == str(SCHEMA_VERSION)


def test_a_v2_database_gains_the_name_locked_column(tmp_path):
    """Downloads already in flight keep their name after the upgrade."""
    import sqlite3

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '2');
        CREATE TABLE downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL,
            final_url TEXT, filename TEXT NOT NULL, save_path TEXT NOT NULL,
            size INTEGER, downloaded INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL, category TEXT,
            connections INTEGER NOT NULL DEFAULT 8, speed_limit INTEGER,
            queue_id INTEGER, error TEXT, added_at REAL NOT NULL,
            started_at REAL, finished_at REAL);
        INSERT INTO downloads(url, filename, save_path, downloaded, state, added_at)
             VALUES ('https://x/a.bin', 'a.bin', '.', 4096, 'paused', 0);
        INSERT INTO downloads(url, filename, save_path, downloaded, state, added_at)
             VALUES ('https://x/b.bin', 'b.bin', '.', 0, 'paused', 0);
        """
    )
    conn.commit()
    conn.close()

    with Database(path) as db:
        rows = {r["filename"]: r["name_locked"] for r in db.list_downloads()}
        assert rows == {"a.bin": 1, "b.bin": 0}
        assert db.query("SELECT value FROM meta WHERE key='schema_version'")[0][
            "value"
        ] == str(SCHEMA_VERSION)


def test_a_v3_database_gains_the_site_profiles_table(tmp_path):
    """Upgrading must not need the file deleted, whatever version it is on."""
    import sqlite3

    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '3');
        """
    )
    conn.commit()
    conn.close()

    with Database(path) as db:
        db.save_profile("example.com", connections=4)
        assert [r["pattern"] for r in db.list_profiles()] == ["example.com"]
        assert db.query("SELECT value FROM meta WHERE key='schema_version'")[0][
            "value"
        ] == str(SCHEMA_VERSION)
