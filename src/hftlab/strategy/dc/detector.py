"""The Directional Change detector (ADR 0024): a price series read in its own time, by reversals of a fixed size.

Nothing here trades. The detector is a pure fold over ticks:

    step(state, price, at_ns) -> (state, events)

  price    the price of one tick, as an exact rational. The series is a named setting, and the one there is,
           is `mid`: half the sum of the best bid and the best ask (`mid`). A float is refused.
  state    everything remembered of the ticks so far. A value: handed back, never kept anywhere else.

One machine per threshold, each on its own. A machine is in one of two modes, up or down, and keeps the
running extreme of its mode: the highest price while up, the lowest while down. On a tick

  1. a price beyond the extreme becomes the extreme, and nothing else happens;
  2. otherwise the move against the mode is measured as a share of the extreme (the difference divided by
     the extreme), and when it is at least the threshold an event CONFIRMS. The comparison is inclusive.
     A machine keeps the price at which that becomes true, the extreme less (or plus) the threshold's share
     of it, so the test on a tick is one exact comparison with that level and no division.

On a confirmation the mode switches and the extreme becomes the confirming price. So there is exactly one
event for each switch, and events alternate, up and down. An event is booked at the price and the receive
time of the tick that crossed, never at the level where the threshold stood: a tick that jumps past the level
confirms at its own price.

A tick whose price equals the price of the tick before it is dropped before any machine sees it. Ticks are
taken in the order they came and are never put on a clock first.

The move after a confirmation is called the overshoot, and it exists in two forms that are two types:

  Provisional   from the last confirmation to the extreme so far. It changes with the ticks, and it is read
                from the state (`provisional`).
  Completed     from a confirmation to the extreme the next event turned from. It is fixed at the moment
                that next event confirms, and the only place it can be had from is that event (`completes`).
                There is no way to ask a state for one, so nothing can read it early.

The mode a machine starts in is arbitrary, so its first events say more about the start than about the
market. The first `warm_up` events of each machine are flagged, and so are the first after `gap`, which is
called when the series was interrupted: the extreme is forgotten, the overshoot in hand is dropped and is
completed by nothing, and the mode and the event counter go on. A flagged event is not to be traded on.

A state can be written out and read back exactly (`dump`, `load`), so that a restart does not put a machine
with a large threshold back into its warm-up.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final

from hftlab.core import Quote

UP, DOWN = "up", "down"
MID: Final = "mid"
VERSION: Final = 1
_MODES: Final = (UP, DOWN)
_MACHINE: Final = frozenset(
    {"mode", "warm_left", "extreme", "extreme_ns", "mark", "events", "confirmed", "confirmed_ns", "origin", "warm"}
)
_STATE: Final = frozenset({"version", "price", "thresholds", "warm_up", "start", "last_price", "ticks", "machines"})


class DetectorError(ValueError):
    """Settings that describe no detector, or a saved state that is not this detector's."""


def mid(quote: Quote) -> Fraction:
    """The mid of a quote: half the sum of its bid and its ask, exactly."""
    return Fraction(quote.bid + quote.ask, 2)


def from_percent(text: object) -> Fraction:
    """A threshold written in percent, as a quoted decimal such as "0.25", as a share of the price."""
    if not isinstance(text, str):
        raise DetectorError(f"a threshold in percent is a quoted decimal such as '0.25', not {text!r}")
    try:
        share = Fraction(Decimal(text)) / 100
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise DetectorError(f"a threshold in percent is a quoted decimal such as '0.25', not {text!r}") from exc
    return share


def _move(start: Fraction, end: Fraction) -> Fraction:
    """How far `end` is from `start`, as a share of `start`. Never negative."""
    return abs(end - start) / start


@dataclass(frozen=True, slots=True)
class Completed:
    """The overshoot of one event, over and fixed: the next event has confirmed."""

    event: int  # the number of the event whose overshoot this is
    direction: str  # that event's direction, and so the direction of the overshoot
    origin_price: Fraction  # the extreme that event turned from
    start_ns: int  # when that event confirmed
    start_price: Fraction  # and at what price: where the overshoot is measured from
    extreme_ns: int
    extreme_price: Fraction  # the furthest the price went before it turned again

    @property
    def overshoot(self) -> Fraction:
        """From the confirmation to the extreme, as a share of the confirming price."""
        return _move(self.start_price, self.extreme_price)

    @property
    def total_move(self) -> Fraction:
        """From one extreme to the next, as a share of the first: the confirmed move and its overshoot."""
        return _move(self.origin_price, self.extreme_price)


@dataclass(frozen=True, slots=True)
class Provisional:
    """The overshoot of the last event as far as it has gone. The next tick may change it."""

    event: int
    direction: str
    start_ns: int
    start_price: Fraction
    extreme_ns: int
    extreme_price: Fraction
    warm_up: bool  # the event it is measured from was flagged as warm-up

    @property
    def overshoot(self) -> Fraction:
        """From the confirmation to the extreme so far, as a share of the confirming price."""
        return _move(self.start_price, self.extreme_price)


@dataclass(frozen=True, slots=True)
class DcEvent:
    """One confirmed change of direction of one machine."""

    threshold: Fraction
    number: int  # this machine's event counter, from 1; it goes on across a gap
    direction: str  # UP: the price rose from a low by the threshold. DOWN: it fell from a high
    confirmed_ns: int  # the receive time of the tick that crossed
    confirmed_price: Fraction  # and its own price
    extreme_ns: int
    extreme_price: Fraction  # the extreme the price turned from
    ticks: int  # ticks since the event before, or since the start or the gap, this one included
    warm_up: bool  # one of the first after the start or a gap: not to be traded on
    completes: Completed | None  # the overshoot of the event before, fixed by this one; None across a gap


@dataclass(frozen=True, slots=True)
class Machine:
    """What one threshold remembers."""

    mode: str
    warm_left: int  # events still to be flagged
    extreme_price: Fraction | None = None  # None until the first tick, and again after a gap
    extreme_ns: int = 0
    level: Fraction | None = None  # the price at or beyond which, against the mode, the next event confirms
    mark: int = 0  # the detector's tick count at the event before, or at the start or the gap
    events: int = 0
    confirmed_price: Fraction | None = None  # the last confirmation since the start or the gap
    confirmed_ns: int = 0
    origin_price: Fraction | None = None  # the extreme that confirmation turned from
    confirmed_warm: bool = False


@dataclass(frozen=True, slots=True)
class DetectorState:
    last_price: Fraction | None  # the price of the tick before, for dropping a repeat of it
    ticks: int  # ticks taken so far, the dropped ones not counted
    machines: tuple[Machine, ...]  # one per threshold, in the detector's order


class Detector:
    """It holds settings only. Everything it remembers is in the state it returns."""

    def __init__(
        self, thresholds: Sequence[Fraction], *, warm_up: int = 2, start: str = UP, price: str = MID
    ) -> None:
        if not thresholds or any(type(share) is not Fraction or not 0 < share < 1 for share in thresholds):
            raise DetectorError("a threshold is an exact share of the price, above 0 and below 1, and one is needed")
        if len(set(thresholds)) != len(thresholds):
            raise DetectorError("two machines with the same threshold are one machine")
        if type(warm_up) is not int or warm_up < 0:
            raise DetectorError("the number of events flagged as warm-up is a whole number, not negative")
        if start not in _MODES:
            raise DetectorError(f"a machine starts up or down, not {start!r}")
        if price != MID:
            raise DetectorError(f"the one price series there is, is {MID!r}, not {price!r}")
        self.thresholds: tuple[Fraction, ...] = tuple(thresholds)
        self.warm_up, self.start, self.price = warm_up, start, price

    def settings(self) -> dict[str, object]:
        """Every setting as a plain value, for a log line: what the events of a run were detected with."""
        shares = [[share.numerator, share.denominator] for share in self.thresholds]
        return {"price": self.price, "thresholds": shares, "warm_up": self.warm_up, "start": self.start}

    def initial(self) -> DetectorState:
        return DetectorState(None, 0, tuple(Machine(self.start, self.warm_up) for _ in self.thresholds))

    # ---- one tick ---------------------------------------------------------------------------------------------

    def step(self, state: DetectorState, price: Fraction, at_ns: int) -> tuple[DetectorState, tuple[DcEvent, ...]]:
        """The state after this tick, and the events it confirmed: at most one for each threshold."""
        if type(price) is not Fraction or price <= 0:
            raise DetectorError(f"a price is an exact rational above zero, not {price!r}")
        if price == state.last_price:
            return state, ()
        ticks, machines, events = state.ticks + 1, [], []
        for threshold, machine in zip(self.thresholds, state.machines, strict=True):
            after, event = _tick(threshold, machine, price, at_ns, ticks)
            machines.append(after)
            if event is not None:
                events.append(event)
        return DetectorState(price, ticks, tuple(machines)), tuple(events)

    def gap(self, state: DetectorState) -> DetectorState:
        """The series was interrupted here. Each machine forgets its extreme and warms up again."""
        machines = tuple(
            Machine(machine.mode, self.warm_up, mark=state.ticks, events=machine.events) for machine in state.machines
        )
        return DetectorState(None, state.ticks, machines)

    def provisional(self, state: DetectorState, slot: int = 0) -> Provisional | None:
        """The overshoot so far of the last event of one machine. None before its first event since a gap."""
        machine = state.machines[slot]
        if machine.confirmed_price is None or machine.extreme_price is None:
            return None
        return Provisional(
            machine.events, machine.mode, machine.confirmed_ns, machine.confirmed_price, machine.extreme_ns,
            machine.extreme_price, machine.confirmed_warm,
        )  # fmt: skip

    # ---- written out and read back ----------------------------------------------------------------------------

    def dump(self, state: DetectorState) -> dict[str, object]:
        """The state as plain values a JSON file holds exactly: whole numbers, text, and pairs of whole numbers."""
        machines = [
            {
                "mode": machine.mode,
                "warm_left": machine.warm_left,
                "extreme": _pair(machine.extreme_price),
                "extreme_ns": machine.extreme_ns,
                "mark": machine.mark,
                "events": machine.events,
                "confirmed": _pair(machine.confirmed_price),
                "confirmed_ns": machine.confirmed_ns,
                "origin": _pair(machine.origin_price),
                "warm": 1 if machine.confirmed_warm else 0,
            }
            for machine in state.machines
        ]
        kept = {"last_price": _pair(state.last_price), "ticks": state.ticks, "machines": machines}
        return {"version": VERSION, **self.settings(), **kept}

    def load(self, saved: object) -> DetectorState:
        """The state `dump` wrote. Refused unless it was written by a detector with these settings."""
        if not isinstance(saved, Mapping) or set(saved) != _STATE or saved["version"] != VERSION:
            raise DetectorError(f"not a version {VERSION} detector state")
        if {key: saved[key] for key in ("price", "thresholds", "warm_up", "start")} != self.settings():
            raise DetectorError("the saved state belongs to a detector with other settings")
        rows = saved["machines"]
        if not isinstance(rows, list) or len(rows) != len(self.thresholds):
            raise DetectorError("the saved state does not hold one machine for each threshold")
        machines = tuple(_machine(row, share) for row, share in zip(rows, self.thresholds, strict=True))
        return DetectorState(_price(saved["last_price"]), _whole(saved["ticks"], "ticks"), machines)


def _level(threshold: Fraction, extreme: Fraction, mode: str) -> Fraction:
    """Where the move against the mode, divided by the extreme, is exactly the threshold."""
    return extreme * (1 - threshold) if mode == UP else extreme * (1 + threshold)


def _tick(
    threshold: Fraction, machine: Machine, price: Fraction, at_ns: int, ticks: int
) -> tuple[Machine, DcEvent | None]:
    extreme, level, rising = machine.extreme_price, machine.level, machine.mode == UP
    if extreme is None or level is None or (price > extreme if rising else price < extreme):
        moved = _level(threshold, price, machine.mode)
        return replace(machine, extreme_price=price, extreme_ns=at_ns, level=moved), None
    if price > level if rising else price < level:
        return machine, None  # the move against the mode is still short of the threshold
    number, direction, flagged = machine.events + 1, DOWN if rising else UP, machine.warm_left > 0
    completes = None
    if machine.confirmed_price is not None and machine.origin_price is not None:
        completes = Completed(
            machine.events, machine.mode, machine.origin_price, machine.confirmed_ns, machine.confirmed_price,
            machine.extreme_ns, extreme,
        )  # fmt: skip
    event = DcEvent(
        threshold, number, direction, at_ns, price, machine.extreme_ns, extreme, ticks - machine.mark, flagged,
        completes,
    )  # fmt: skip
    after = Machine(
        direction, max(0, machine.warm_left - 1), price, at_ns, _level(threshold, price, direction), ticks, number,
        price, at_ns, extreme, flagged,
    )  # fmt: skip
    return after, event


def _pair(price: Fraction | None) -> list[int] | None:
    return None if price is None else [price.numerator, price.denominator]


def _whole(value: object, what: str) -> int:
    if type(value) is not int or value < 0:
        raise DetectorError(f"a saved machine has no whole, non-negative number for {what}: {value!r}")
    return value


def _price(value: object) -> Fraction | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2 or any(type(part) is not int or part <= 0 for part in value):
        raise DetectorError(f"a saved price is a pair of whole numbers above zero, not {value!r}")
    price = Fraction(value[0], value[1])
    if [price.numerator, price.denominator] != value:
        raise DetectorError(f"a saved price is written in lowest terms, not {value!r}")
    return price


def _machine(row: Any, threshold: Fraction) -> Machine:
    if not isinstance(row, Mapping) or set(row) != _MACHINE or row["mode"] not in _MODES or row["warm"] not in (0, 1):
        raise DetectorError(f"not a saved machine: {row!r}")
    extreme = _price(row["extreme"])
    return Machine(
        mode=row["mode"],
        warm_left=_whole(row["warm_left"], "warm_left"),
        extreme_price=extreme,
        extreme_ns=_whole(row["extreme_ns"], "extreme_ns"),
        level=None if extreme is None else _level(threshold, extreme, row["mode"]),
        mark=_whole(row["mark"], "mark"),
        events=_whole(row["events"], "events"),
        confirmed_price=_price(row["confirmed"]),
        confirmed_ns=_whole(row["confirmed_ns"], "confirmed_ns"),
        origin_price=_price(row["origin"]),
        confirmed_warm=row["warm"] == 1,
    )
