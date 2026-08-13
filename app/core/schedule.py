"""When a queue should start and stop - pure logic, no clock of its own.

Every function takes `now` explicitly so the whole module is testable without
sleeping or freezing time. The database stores exactly these fields; the GUI
timer only asks "is anything due?" once every few seconds.

Two rules make the behaviour predictable when the app is not running at the
scheduled minute:

* a start fires as long as `now` is past today's start time and the queue has
  not already been started for that occurrence - so a 02:00 job still runs if
  the machine was switched on at 07:00, unless a stop time has passed too;
* a stop fires only after a start for the same day, so an idle queue is never
  paused by a schedule that never began.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

ONE_DAY = timedelta(days=1)

#: bit 0 = Monday ... bit 6 = Sunday (matches `datetime.weekday()`)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
EVERY_DAY = 0b1111111
WEEKDAYS = 0b0011111
WEEKEND = 0b1100000


class PostAction(str, Enum):
    """What to do once the scheduled work is finished."""

    NONE = "none"
    EXIT = "exit"
    SHUTDOWN = "shutdown"
    HIBERNATE = "hibernate"
    SLEEP = "sleep"


def parse_hhmm(text: str | None) -> time | None:
    """`"02:30"` -> `time(2, 30)`. Anything unparsable is None."""
    if not text:
        return None
    parts = text.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour, minute)


def format_hhmm(value: time | None) -> str | None:
    return f"{value.hour:02d}:{value.minute:02d}" if value is not None else None


def mask_for(days: list[int] | tuple[int, ...]) -> int:
    mask = 0
    for day in days:
        mask |= 1 << (day % 7)
    return mask


def days_of(mask: int) -> list[int]:
    return [day for day in range(7) if mask & (1 << day)]


def describe_days(mask: int) -> str:
    mask &= EVERY_DAY
    if mask == EVERY_DAY:
        return "Every day"
    if mask == WEEKDAYS:
        return "Weekdays"
    if mask == WEEKEND:
        return "Weekend"
    if mask == 0:
        return "Never"
    return ", ".join(DAY_NAMES[day] for day in days_of(mask))


@dataclass(slots=True)
class Schedule:
    """One row of the `schedules` table."""

    id: int | None = None
    queue_id: int | None = None
    enabled: bool = True
    start_at: str | None = None      # "HH:MM", None = no automatic start
    stop_at: str | None = None       # "HH:MM", None = run until the queue drains
    days_mask: int = EVERY_DAY
    on_complete: str = PostAction.NONE.value
    #: epoch seconds of the last start this schedule triggered
    last_run: float | None = None

    # ------------------------------------------------------------------ times

    @property
    def start_time(self) -> time | None:
        return parse_hhmm(self.start_at)

    @property
    def stop_time(self) -> time | None:
        return parse_hhmm(self.stop_at)

    @property
    def action(self) -> PostAction:
        try:
            return PostAction(self.on_complete or "none")
        except ValueError:
            return PostAction.NONE

    def runs_on(self, day: date) -> bool:
        return bool(self.days_mask & (1 << day.weekday()))

    def start_on(self, day: date) -> datetime | None:
        start = self.start_time
        if start is None or not self.runs_on(day):
            return None
        return datetime.combine(day, start)

    def stop_on(self, day: date) -> datetime | None:
        stop = self.stop_time
        if stop is None or not self.runs_on(day):
            return None
        moment = datetime.combine(day, stop)
        start = self.start_on(day)
        if start is not None and moment <= start:
            # "23:00 to 02:00" means the stop belongs to the next morning.
            return moment + ONE_DAY
        return moment

    # --------------------------------------------------------------- decisions

    def due_start(self, now: datetime) -> datetime | None:
        """The occurrence `now` should start, or None.

        Returns the occurrence time (not `now`) so the caller can record
        exactly which run it fired and never repeat it.
        """
        if not self.enabled or self.start_time is None:
            return None
        for day in (now.date(), now.date() - ONE_DAY):
            occurrence = self.start_on(day)
            if occurrence is None or occurrence > now:
                continue
            if self.already_ran(occurrence):
                continue
            stop = self.stop_on(day)
            if stop is not None and now >= stop:
                continue  # the window closed while we were away
            if day != now.date() and stop is None:
                # Yesterday's run with no window: too stale to start now, the
                # only reason to look back a day is an overnight window.
                continue
            return occurrence
        return None

    def due_stop(self, now: datetime) -> bool:
        """True when the running window has closed."""
        if self.stop_time is None or self.last_run is None:
            return False
        started = datetime.fromtimestamp(self.last_run)
        stop = self.stop_on(started.date())
        if stop is None:
            # The stop belongs to a day this schedule does not cover; fall
            # back to the plain time on the day the run started.
            stop = datetime.combine(started.date(), self.stop_time)
            if stop <= started:
                stop += ONE_DAY
        return now >= stop

    def already_ran(self, occurrence: datetime) -> bool:
        return self.last_run is not None and self.last_run >= occurrence.timestamp()

    def covers(self, now: datetime) -> bool:
        """True while `now` is inside today's window.

        Used by the bandwidth schedule, where the question is not "should
        something fire" but "which limit applies right this second".
        """
        if not self.enabled or self.start_time is None:
            return False
        for day in (now.date(), now.date() - ONE_DAY):
            start = self.start_on(day)
            if start is None or start > now:
                continue
            stop = self.stop_on(day)
            if stop is None:
                # No end time: the window is the rest of that day.
                return day == now.date()
            if now < stop:
                return True
        return False

    def next_start(self, now: datetime) -> datetime | None:
        """Next future occurrence, for display in the scheduler dialog."""
        if self.start_time is None or not self.days_mask:
            return None
        for offset in range(0, 8):
            day = now.date() + ONE_DAY * offset
            occurrence = self.start_on(day)
            if occurrence is not None and occurrence > now:
                return occurrence
        return None
