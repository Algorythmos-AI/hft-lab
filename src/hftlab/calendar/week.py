"""The forex week and the daily rollover, as pure functions of a UTC time.

The market follows New York's clock, not UTC's: it opens on Sunday at 17:15 New York time and closes on Friday
at 17:00, and the daily rollover is at 17:00. New York moves its clock twice a year, so in UTC those moments
move by an hour; a rule written in UTC, or in any other zone's local time, is wrong for part of every year.
Everything here converts through the New York time zone and returns integer UTC nanoseconds.

17:00 and 17:15 never fall in the hour New York skips or repeats (that happens at 02:00), so each of these
local times names exactly one instant.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from hftlab.core.money import require_int

NEW_YORK: Final = ZoneInfo("America/New_York")
SECOND: Final = 1_000_000_000
MINUTE: Final = 60 * SECOND
OPEN_TIME: Final = time(17, 15)
CLOSE_TIME: Final = time(17, 0)
ROLLOVER_TIME: Final = time(17, 0)
SUNDAY: Final = 6
FRIDAY: Final = 4
SATURDAY: Final = 5
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def new_york(ts_ns: int) -> datetime:
    """The New York wall-clock time of a UTC instant, to the second."""
    if require_int("ts_ns", ts_ns) < 0:
        raise ValueError(f"a time before 1970 is not a market time: {ts_ns}")
    return datetime.fromtimestamp(ts_ns // SECOND, tz=NEW_YORK)


def instant(day: date, at: time) -> int:
    """The UTC nanoseconds of a New York wall-clock time on a New York date."""
    since = datetime.combine(day, at, tzinfo=NEW_YORK) - _EPOCH
    return (since.days * 86_400 + since.seconds) * SECOND


def is_week_open(ts_ns: int) -> bool:
    """Is the forex week open at this instant? Holidays are not considered here (see `Calendar`)."""
    local = new_york(ts_ns)
    weekday = local.weekday()
    if weekday == SATURDAY:
        return False
    if weekday == SUNDAY:
        return ts_ns >= instant(local.date(), OPEN_TIME)
    if weekday == FRIDAY:
        return ts_ns < instant(local.date(), CLOSE_TIME)
    return True


def next_open(ts_ns: int) -> int:
    """The first instant at or after `ts_ns` when the week is open (`ts_ns` itself if it is)."""
    if is_week_open(ts_ns):
        return ts_ns
    day = new_york(ts_ns).date()
    for ahead in range(8):
        candidate = day + timedelta(days=ahead)
        if candidate.weekday() == SUNDAY and (opened := instant(candidate, OPEN_TIME)) >= ts_ns:
            return opened
    raise AssertionError("there is a Sunday within any eight days")


def next_close(ts_ns: int) -> int:
    """The first weekly close after `ts_ns`: the Friday 17:00 that ends the week now open, or the next one."""
    day = new_york(ts_ns).date()
    for ahead in range(8):
        candidate = day + timedelta(days=ahead)
        if candidate.weekday() == FRIDAY and (closed := instant(candidate, CLOSE_TIME)) > ts_ns:
            return closed
    raise AssertionError("there is a Friday within any eight days")


def rollover_near(ts_ns: int) -> int:
    """The daily rollover instant (17:00 New York) closest to `ts_ns`."""
    day = new_york(ts_ns).date()
    candidates = [instant(day + timedelta(days=offset), ROLLOVER_TIME) for offset in (-1, 0, 1)]
    return min(candidates, key=lambda at: abs(at - ts_ns))


def last_rollover(ts_ns: int) -> int:
    """The latest daily rollover instant (17:00 New York) at or before `ts_ns`: when the trading day in hand began."""
    day = new_york(ts_ns).date()
    candidates = [instant(day + timedelta(days=offset), ROLLOVER_TIME) for offset in (-1, 0)]
    return max(at for at in candidates if at <= ts_ns)


def next_rollover(ts_ns: int) -> int:
    """The first daily rollover instant (17:00 New York) after `ts_ns`: when the trading day in hand ends."""
    day = new_york(ts_ns).date()
    candidates = [instant(day + timedelta(days=offset), ROLLOVER_TIME) for offset in (0, 1)]
    return min(at for at in candidates if at > ts_ns)


def in_rollover_window(ts_ns: int, before_ns: int, after_ns: int) -> bool:
    """Is `ts_ns` within `before_ns` before or `after_ns` after a daily rollover? Both ends are included."""
    if require_int("before_ns", before_ns) < 0 or require_int("after_ns", after_ns) < 0:
        raise ValueError("the rollover window cannot have a negative side")
    offset = ts_ns - rollover_near(ts_ns)
    return -before_ns <= offset <= after_ns
