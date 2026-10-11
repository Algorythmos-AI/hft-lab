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
