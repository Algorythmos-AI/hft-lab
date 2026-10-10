"""Dated configuration: when the market is shut for a holiday, and when a macro release is due.

Both are lists of facts with dates, kept in config/calendar/ and never guessed here. Every time in those files
carries its own UTC offset ("2026-12-25T22:00:00Z" or "...-05:00"); a time without one is refused, because
which zone the author meant is exactly the mistake this is here to prevent.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hftlab.calendar.week import MINUTE, SECOND, in_rollover_window, is_week_open, next_close, next_open

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class CalendarError(ValueError):
    """A calendar file that cannot be read as what it claims to be."""


def parse_instant(text: object, what: str) -> int:
    """An ISO-8601 time with an explicit offset, as UTC nanoseconds."""
    if not isinstance(text, str):
        raise CalendarError(f"{what} must be a quoted ISO-8601 time with an offset, not {text!r}")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CalendarError(f"{what} is not an ISO-8601 time: {text!r}") from exc
    if moment.tzinfo is None:
        raise CalendarError(f"{what} has no UTC offset: {text!r}. Write 'Z' or '+HH:MM'")
    since = moment - _EPOCH
    return (since.days * 86_400 + since.seconds) * SECOND + since.microseconds * 1_000


@dataclass(frozen=True, slots=True)
class Closure:
    """A span when the market is shut although the week is open: a holiday. `[start_ns, end_ns)`."""

    name: str
    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class MacroEvent:
    """A scheduled release. Trading in a pair of one of its currencies is blacked out around it."""

    name: str
    at_ns: int
    currencies: frozenset[str]
    before_ns: int
    after_ns: int

    def covers(self, ts_ns: int, pair: str | None = None) -> bool:
        if not self.at_ns - self.before_ns <= ts_ns <= self.at_ns + self.after_ns:
            return False
        return pair is None or bool(self.currencies & set(pair.split(".")))


@dataclass(frozen=True, slots=True)
class Calendar:
    """The forex week with its dated exceptions. Every answer is a pure function of a UTC time."""

    closures: tuple[Closure, ...] = ()
    events: tuple[MacroEvent, ...] = ()

    def is_open(self, ts_ns: int) -> bool:
        return is_week_open(ts_ns) and not any(c.start_ns <= ts_ns < c.end_ns for c in self.closures)

    def closed_reason(self, ts_ns: int) -> str | None:
        """Why the market is shut at `ts_ns`: the weekend, a named holiday, or None when it is open."""
        if not is_week_open(ts_ns):
            return "weekend"
        return next((c.name for c in self.closures if c.start_ns <= ts_ns < c.end_ns), None)

    def open_ns_between(self, start_ns: int, end_ns: int) -> int:
        """Exactly how long the market was open in `[start_ns, end_ns)`."""
        total, at = 0, start_ns
        while at < end_ns:
            if not is_week_open(at):
                at = min(end_ns, next_open(at))
                continue
            until = min(end_ns, next_close(at))
            shut, covered = 0, at
            for closure in sorted(self.closures, key=lambda c: c.start_ns):
                begin, finish = max(closure.start_ns, covered), min(closure.end_ns, until)
                if finish > begin:
                    shut += finish - begin
                    covered = finish
            total += until - at - shut
            at = until
        return total

    def in_blackout(self, ts_ns: int, pair: str | None = None) -> bool:
        return any(event.covers(ts_ns, pair) for event in self.events)

    @staticmethod
    def in_rollover(ts_ns: int, before_ns: int, after_ns: int) -> bool:
        return in_rollover_window(ts_ns, before_ns, after_ns)


def _minutes(value: object, what: str) -> int:
    if type(value) is not int or value < 0:
        raise CalendarError(f"{what} must be a whole, non-negative number of minutes, not {value!r}")
    return value * MINUTE


def _load(path: Path, key: str) -> tuple[Mapping[str, Any], list[Any]]:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CalendarError(f"cannot read {path}: {exc}") from exc
    rows = loaded.get(key) if isinstance(loaded, dict) else None
    if not isinstance(rows, list):
        raise CalendarError(f"{path} needs a `{key}` list (it may be empty)")
    return loaded, rows


def load_closures(path: Path) -> tuple[Closure, ...]:
    _, rows = _load(path, "closures")
    out = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "from", "to"}:
            raise CalendarError(f"{path}: a closure needs exactly name, from and to: {row!r}")
        start, end = parse_instant(row["from"], "from"), parse_instant(row["to"], "to")
        if not isinstance(row["name"], str) or not row["name"].strip() or end <= start:
            raise CalendarError(f"{path}: a closure needs a name and must end after it starts: {row!r}")
        out.append(Closure(row["name"], start, end))
    return tuple(sorted(out, key=lambda c: c.start_ns))


def load_events(path: Path) -> tuple[MacroEvent, ...]:
    loaded, rows = _load(path, "events")
    defaults = loaded.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise CalendarError(f"{path}: `defaults` must be a mapping")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not {"name", "at", "currencies"} <= set(row):
            raise CalendarError(f"{path}: an event needs name, at and currencies: {row!r}")
        if set(row) - {"name", "at", "currencies", "before_minutes", "after_minutes"}:
            raise CalendarError(f"{path}: an event has a key this file does not define: {row!r}")
        currencies = row["currencies"]
        well_formed = isinstance(currencies, list) and currencies and all(
            isinstance(c, str) and len(c) == 3 and c.isalpha() and c.isupper() for c in currencies
        )
        if not well_formed or not isinstance(row["name"], str) or not row["name"].strip():
            raise CalendarError(f"{path}: an event needs a name and three-letter currency codes: {row!r}")
        before = _minutes(row.get("before_minutes", defaults.get("before_minutes")), "before_minutes")
        after = _minutes(row.get("after_minutes", defaults.get("after_minutes")), "after_minutes")
        out.append(MacroEvent(row["name"], parse_instant(row["at"], "at"), frozenset(currencies), before, after))
    return tuple(sorted(out, key=lambda e: e.at_ns))


def load_calendar(directory: Path) -> Calendar:
    """The calendar in `directory`: holidays.yaml and macro_events.yaml. A missing file is an empty list."""
    holidays, events = directory / "holidays.yaml", directory / "macro_events.yaml"
    return Calendar(
        load_closures(holidays) if holidays.exists() else (),
        load_events(events) if events.exists() else (),
    )
