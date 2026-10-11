"""A time as a file writes it, into integer UTC nanoseconds. The format and the zone are stated, never guessed.

A format is either one of four counts since 1970 (`epoch_s`, `epoch_ms`, `epoch_us`, `epoch_ns`, which are UTC
by definition) or a pattern of these fields and any text between them:

    %Y year, four digits      %m month    %d day    %H hour    %M minute    %S second    (two digits each)
    %f the fraction of a second, one to nine digits

Each of the first six appears exactly once and `%f` at most once. A time that does not match the pattern
digit for digit is an error: the description is wrong for this file, and nothing here tries another reading.

A zone is `UTC`, a fixed offset written `+02:00` or `-05:00`, or a name of the time-zone database. In a zone
that changes its clocks, a wall-clock time in the hour that is repeated names two instants, and one in the
hour that is skipped names none. `instant` answers None for both: which one was meant is not in the file.
"""
from __future__ import annotations

import functools
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

SECOND: Final = 1_000_000_000
EPOCHS: Final = {"epoch_s": SECOND, "epoch_ms": 1_000_000, "epoch_us": 1_000, "epoch_ns": 1}
_FIELDS: Final = {"Y": 4, "m": 2, "d": 2, "H": 2, "M": 2, "S": 2}
_OFFSET = re.compile(r"([+-])([0-9]{2}):([0-9]{2})")
_WHOLE = re.compile(r"[0-9]{1,19}")
_DAY_ZERO: Final = date(1970, 1, 1).toordinal()


class TimeError(ValueError):
    """A time format or a zone that cannot be used, or a time that is not written the way the format says."""


def zone_of(stated: object) -> tzinfo:
    """The zone a description states. No abbreviation is expanded and nothing is assumed when none is given."""
    if not isinstance(stated, str) or not stated:
        raise TimeError("no time zone is stated; a file's times are never guessed")
    if stated == "UTC":
        return UTC
    fixed = _OFFSET.fullmatch(stated)
    if fixed:
        hours, minutes = int(fixed[2]), int(fixed[3])
        if hours > 14 or minutes > 59:
            raise TimeError(f"{stated!r} is not an offset from UTC")
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(-offset if fixed[1] == "-" else offset)
    try:
        if stated not in _zones():  # by its exact name: a file system that ignores case does not decide this
            raise ZoneInfoNotFoundError(stated)
        return ZoneInfo(stated)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimeError(f"{stated!r} is not UTC, an offset like +02:00 or a zone of the time-zone database") from exc


@functools.cache
def _zones() -> frozenset[str]:
    return frozenset(available_timezones())


def _offset_s(local: datetime, zone: tzinfo, fold: int) -> int:
    offset = local.replace(tzinfo=zone, fold=fold).utcoffset()
    assert offset is not None
    return offset.days * 86_400 + offset.seconds


def _pattern(text: str) -> tuple[re.Pattern[str], bool]:
    """The format as a pattern with one named group per field, and whether it has a fraction."""
    out, seen, at = [], [], 0
    while at < len(text):
        if text[at] != "%":
            out.append(re.escape(text[at]))
            at += 1
            continue
        letter = text[at + 1 : at + 2]
        if letter == "f":
            out.append("(?P<f>[0-9]{1,9})")
        elif letter in _FIELDS:
            out.append(f"(?P<{letter}>[0-9]{{{_FIELDS[letter]}}})")
        else:
            raise TimeError(f"a time format is made of %Y %m %d %H %M %S %f and plain text, not %{letter}")
        seen.append(letter)
        at += 2
    if sorted(seen) not in (sorted(_FIELDS), sorted([*_FIELDS, "f"])):
        raise TimeError("a time format names the year, month, day, hour, minute and second once each")
    return re.compile("".join(out)), "f" in seen


def reader(time_format: object, stated_zone: object) -> Callable[[str], int | None]:
    """A function from a time as written to UTC nanoseconds, or None when it names no instant or two.

    It raises `TimeError` for a time that is not written as the format says.
    """
    zone = zone_of(stated_zone)
    if not isinstance(time_format, str) or not time_format:
        raise TimeError("no time format is stated")
    if time_format in EPOCHS:
        if zone is not UTC:
            raise TimeError(f"{time_format} counts from 1970 in UTC; the stated zone must be UTC")
        unit = EPOCHS[time_format]

        def count(text: str) -> int | None:
            if not _WHOLE.fullmatch(text):
                raise TimeError(f"{text!r} is not a whole count of {time_format}")
            return int(text) * unit

        return count
    pattern, _ = _pattern(time_format)
    fixed = None if isinstance(zone, ZoneInfo) else _offset_s(datetime(2000, 1, 1), zone, 0)
    hours: dict[tuple[int, int, int, int], int | None] = {}  # the offset of a whole local hour, when it has one

    def offset(local: datetime) -> int | None:
        if fixed is not None:
            return fixed
        key = (local.year, local.month, local.day, local.hour)
        if key not in hours:
            ends = (local.replace(minute=0, second=0), local.replace(minute=59, second=59))
            found = {_offset_s(end, zone, fold) for end in ends for fold in (0, 1)}
            hours[key] = found.pop() if len(found) == 1 else None
        whole = hours[key]
        if whole is not None:
            return whole
        first, second = _offset_s(local, zone, 0), _offset_s(local, zone, 1)
        return first if first == second else None  # repeated or skipped: two instants, or none

    def clock(text: str) -> int | None:
        match = pattern.fullmatch(text)
        if match is None:
            raise TimeError(f"{text!r} is not written as {time_format!r}")
        year, month, day, hour, minute, second = (int(match[name]) for name in "YmdHMS")
        try:
            local = datetime(year, month, day, hour, minute, second)
        except ValueError as exc:
            raise TimeError(f"{text!r} is not a date and a time: {exc}") from exc
        away = offset(local)
        if away is None:
            return None
        seconds = (local.toordinal() - _DAY_ZERO) * 86_400 + hour * 3_600 + minute * 60 + second - away
        if seconds < 0:
            raise TimeError(f"{text!r} is before 1970")
        fraction = match.groupdict().get("f")
        return seconds * SECOND + (int(fraction.ljust(9, "0")) if fraction else 0)

    return clock
