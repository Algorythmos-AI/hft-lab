"""What the risk gate is told about the room left on the trader's event tape (ADR 0027, addendum).

The tape is what a replay runs on, and it is held under a byte quota. A record that is running out of room is
a reason to stop taking risk on, and then to take it off, long before it is a reason to stop watching:

    ROOM   there is room: nothing stands in the way
    SOFT   the soft limit is reached: entries stop, exits go on
    HARD   the hard limit is reached: entries stop, and every position is closed, as under FLATTEN

The gate does not measure a tape. It is shown a `TapeRoom`, like every other event, by the trader, which reads
the level off the turn the tape recorded: a replayed day shows the gate the same levels at the same turns.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

ROOM: Final = 0
SOFT: Final = 1
HARD: Final = 2
LEVELS: Final = (ROOM, SOFT, HARD)


@dataclass(frozen=True, slots=True)
class TapeRoom:
    """The room on the event tape as of this turn."""

    level: int

    def __post_init__(self) -> None:
        if type(self.level) is not int or self.level not in LEVELS:
            raise ValueError(f"the tape's room is one of {LEVELS}, not {self.level!r}")


@dataclass(frozen=True, slots=True)
class FloatState:
    """Where the float of a cash account stands (ADR 0031), shown to the gate by the trader like the tape's room.

    `anchor` is the cash the account began with, by currency in millionths, as far as the broker book is
    concerned: the gate adds what its own book's fills moved and has the cash it reasons about. `funded` says a
    baseline stands, so a sleeve may deviate from it. `open` says an operator asked for a funding or an
    unfunding that has not ended: only then may a conversion of the float go. `unwinding` says it is an
    unfunding: the float is being converted back to the hub, so the reserve of every other currency is no
    longer kept (nothing will trade around it), and the hub's still is.
    """

    anchor: Mapping[str, int] | None = None
    funded: bool = False
    open: bool = False
    unwinding: bool = False
