"""The life of one order as the order manager sees it, and the only moves it may make (ADR 0016).

    INTENDED -> JOURNALLED -> SENT -> WORKING -> PARTIALLY_FILLED -> FILLED
                                         \\-> CANCELLED | REJECTED | EXPIRED
    UNKNOWN: it may be at the venue or it may not; nothing is assumed until the venue has been asked.

`MOVES` is the whole table. A move that is not in it is never made: it is reported as a `Fault`, which the
manager records and which stops entries.

Venue reports arrive duplicated and out of order: a fill before the acknowledgement, a FILLED before the fills
it counts, an old partial report after the final one. So the stage is not driven by the order in which reports
arrive. It is driven by two numbers that only grow, `booked` (the fills booked, each once) and `reported` (the
most any report has claimed), and by the first terminal report. A report that says nothing new is ignored. Any
sequence that is a reordering, with repeats, of the same true history ends in the same stage with the same
fills. A report that contradicts what is already established is a fault, and a fill is booked all the same:
the venue says it happened, and the book has to say what the venue says.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, unique
from types import MappingProxyType
from typing import Final

from hftlab.core import Fill, Order, OrderEvent, OrderState


@unique
class Stage(Enum):
    INTENDED = "INTENDED"  # allowed by the risk gate; nothing written, nothing sent
    JOURNALLED = "JOURNALLED"  # durably in the outbox; not sent
    SENT = "SENT"  # handed to the venue; not acknowledged
    WORKING = "WORKING"  # acknowledged by the venue
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # in flight when sight of it was lost


S = Stage
TERMINAL: Final = frozenset({S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED})
IN_FLIGHT: Final = frozenset({S.JOURNALLED, S.SENT, S.WORKING, S.PARTIALLY_FILLED, S.UNKNOWN})
_ENDS: Final = frozenset({S.FILLED, S.CANCELLED, S.EXPIRED})
MOVES: Final[Mapping[Stage, frozenset[Stage]]] = MappingProxyType(
    {
        S.INTENDED: frozenset({S.JOURNALLED, S.REJECTED}),
        S.JOURNALLED: frozenset({S.SENT, S.REJECTED, S.UNKNOWN}),
        S.SENT: frozenset({S.WORKING, S.PARTIALLY_FILLED, S.REJECTED, S.UNKNOWN}) | _ENDS,
        S.WORKING: frozenset({S.PARTIALLY_FILLED, S.REJECTED, S.UNKNOWN}) | _ENDS,
        S.PARTIALLY_FILLED: frozenset({S.UNKNOWN}) | _ENDS,
        S.UNKNOWN: frozenset({S.WORKING, S.PARTIALLY_FILLED, S.REJECTED}) | _ENDS,
        S.FILLED: frozenset(),
        S.CANCELLED: frozenset(),
        S.REJECTED: frozenset(),
        S.EXPIRED: frozenset(),
    }
)
_OF_STATE: Final = {
    OrderState.WORKING: S.WORKING,
    OrderState.PARTIALLY_FILLED: S.PARTIALLY_FILLED,
    OrderState.FILLED: S.FILLED,
    OrderState.CANCELLED: S.CANCELLED,
    OrderState.REJECTED: S.REJECTED,
    OrderState.EXPIRED: S.EXPIRED,
}


@unique
class Fault(Enum):
    """What cannot happen, and did. Each is recorded with the order it concerns, and stops entries."""

    IMPOSSIBLE_MOVE = "IMPOSSIBLE_MOVE"  # a move that is not in the table
    REPORT_BEFORE_SEND = "REPORT_BEFORE_SEND"  # the venue reported on an order that was never handed to it
    FILL_MISMATCH = "FILL_MISMATCH"  # a fill in another pair or on the other side than its order
    OVERFILL = "OVERFILL"  # more filled, or reported filled, than was ordered
    FILL_ON_REJECTED = "FILL_ON_REJECTED"
    FILL_AFTER_CLOSE = "FILL_AFTER_CLOSE"  # a fill beyond what the venue said was filled when it closed the order
    CONTRADICTION = "CONTRADICTION"  # two reports that cannot both be true
    FILL_WITHOUT_ORDER = "FILL_WITHOUT_ORDER"  # a fill for an order the outbox does not hold
    REPORT_WITHOUT_ORDER = "REPORT_WITHOUT_ORDER"  # an order event for one of our ids the outbox does not hold
    UNBOOKABLE_FILL = "UNBOOKABLE_FILL"  # the book refused it: an unknown pair, or two fills under one id
    ID_REUSED = "ID_REUSED"  # a different order was proposed under an id already used
    FOREIGN_ORDER = "FOREIGN_ORDER"  # the venue holds a working order that is not this platform's
    UNTRACKED_ORDER = "UNTRACKED_ORDER"  # the venue holds a working order of ours the outbox does not hold
    OPEN_AFTER_CLOSE = "OPEN_AFTER_CLOSE"  # the venue holds as working an order established as finished
    JOURNAL_FAILED = "JOURNAL_FAILED"  # the outbox could not be written; nothing was sent
    SEND_FAILED = "SEND_FAILED"  # the venue raised something unexpected while being handed an order


@dataclass(slots=True)
class Tracked:
    """One order, and what has been established about it."""

    order: Order
    stage: Stage = Stage.INTENDED
    booked: int = 0  # the sum of its fills that were booked, each once
    reported: int = 0  # the most any order event has said was filled
    acked: bool = False  # the venue has said it holds, or held, the order
    cancel_sent: bool = False
    fills: set[str] = field(default_factory=set)

    @property
    def owed(self) -> int:
        """Quantity the venue has counted as filled and no booked fill accounts for yet."""
        return max(self.reported - self.booked, 0)

    def move(self, to: Stage) -> bool:
        """Make a move from the table. False, and nothing changes, when it is not in the table."""
        if to is self.stage:
            return True
        if to not in MOVES[self.stage]:
            return False
        self.stage = to
        return True

    def _advance(self, to: Stage, faults: list[Fault]) -> None:
        if not self.move(to):
            faults.append(Fault.IMPOSSIBLE_MOVE)

    def _was_sent_after_all(self, faults: list[Fault]) -> None:
        """The venue has reported on an order held as unsent. It was sent; say so, loudly, and carry on from there."""
        if self.stage in (Stage.INTENDED, Stage.JOURNALLED):
            faults.append(Fault.REPORT_BEFORE_SEND)
            self.stage = Stage.SENT

    def take_fill(self, fill: Fill) -> tuple[Fault, ...]:
        """Count a fill that has just been booked, for the first time, and move the stage it implies."""
        faults: list[Fault] = []
        self.fills.add(fill.fill_id)
        self.booked += fill.quantity
        if fill.pair != self.order.pair or fill.side is not self.order.side:
            faults.append(Fault.FILL_MISMATCH)
        self._was_sent_after_all(faults)
        if self.booked > self.order.quantity:
            faults.append(Fault.OVERFILL)
        implied = Stage.FILLED if self.booked >= self.order.quantity else Stage.PARTIALLY_FILLED
        if self.stage in TERMINAL and not self.acked:
            # Written off without a word from the venue, and here is a fill. The venue's word stands.
            faults.append(Fault.FILL_AFTER_CLOSE)
            self.stage = implied
        elif self.stage is Stage.REJECTED:
            faults.append(Fault.FILL_ON_REJECTED)
        elif self.stage in (Stage.CANCELLED, Stage.EXPIRED):
            if self.booked > self.reported:
                faults.append(Fault.FILL_AFTER_CLOSE)
        elif self.stage is not Stage.FILLED:
            self._advance(implied, faults)
        self.acked = True
        return tuple(faults)

    def take_event(self, event: OrderEvent) -> tuple[Fault, ...]:
        """Take one order event from the venue. A report that adds nothing changes nothing and is no fault."""
        to = _OF_STATE.get(event.state)
        if to is None:
            return ()  # NEW and SUBMITTED echo what the manager did itself
        faults: list[Fault] = []
        self._was_sent_after_all(faults)
        claimed = event.filled_quantity
        if claimed > self.order.quantity or (to is Stage.FILLED and claimed != self.order.quantity):
            faults.append(Fault.OVERFILL if claimed > self.order.quantity else Fault.CONTRADICTION)
            claimed = min(claimed, self.order.quantity)
        if self.stage in TERMINAL:
            if not self.acked and to is not self.stage:
                # Written off without a word from the venue, and here is the venue. Its word stands.
                faults.append(Fault.CONTRADICTION)
                self.acked, self.stage, self.reported = True, to, max(self.reported, claimed)
                return tuple(faults)
            closed_at = self.order.quantity if self.stage is Stage.FILLED else self.reported
            if claimed > closed_at or (to in TERMINAL and to is not self.stage):
                faults.append(Fault.CONTRADICTION)
            else:
                self.reported = max(self.reported, claimed)
            return tuple(faults)
        self.acked = True
        if to is Stage.REJECTED:
            if self.booked or claimed:
                faults.append(Fault.FILL_ON_REJECTED)
            else:
                self._advance(Stage.REJECTED, faults)
            return tuple(faults)
        self.reported = max(self.reported, claimed)
        if to in (Stage.CANCELLED, Stage.EXPIRED) and self.booked > claimed:
            faults.append(Fault.CONTRADICTION)
        if to in TERMINAL or to is Stage.PARTIALLY_FILLED or self.stage in (Stage.SENT, Stage.UNKNOWN):
            self._advance(to, faults)
        return tuple(faults)
