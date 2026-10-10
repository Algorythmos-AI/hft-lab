"""Time is injected.

Strategy, risk and book code never asks the operating system what time it is: it is handed a `Clock`. In a
replay that clock is a `SimClock` driven by the recording, so a recorded day replays to the same decisions
(ADR 0005). `WallClock` is the only code in the package allowed to read system time, and a test scans every
module to keep it that way (tests/core/test_clock.py).

All times are integer UTC nanoseconds since the Unix epoch (ADR 0002).
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from hftlab.core.money import require_int


class ClockWentBackwards(ValueError):
    """A simulated clock was asked to move to the past: a replay ordering bug, never something to absorb."""


@runtime_checkable
class Clock(Protocol):
    def now_ns(self) -> int:
        """The current time in integer UTC nanoseconds."""
        ...


class SimClock:
    """A clock that only moves when told to, and only forwards."""

    __slots__ = ("_now",)

    def __init__(self, start_ns: int = 0) -> None:
        if require_int("start_ns", start_ns) < 0:
            raise ValueError(f"start_ns cannot be negative: {start_ns}")
        self._now = start_ns

    def now_ns(self) -> int:
        return self._now

    def advance(self, delta_ns: int) -> int:
        """Move forward by `delta_ns` (zero is allowed) and return the new time."""
        if require_int("delta_ns", delta_ns) < 0:
            raise ClockWentBackwards(f"cannot advance by {delta_ns} ns")
        self._now += delta_ns
        return self._now

    def advance_to(self, ts_ns: int) -> int:
        """Move to `ts_ns`, which must not be before now, and return it."""
        if require_int("ts_ns", ts_ns) < self._now:
            raise ClockWentBackwards(f"cannot move from {self._now} back to {ts_ns}")
        self._now = ts_ns
        return self._now


class WallClock:
    """The system clock. The one place in `hftlab` that reads it."""

    __slots__ = ()

    def now_ns(self) -> int:
        return time.time_ns()
