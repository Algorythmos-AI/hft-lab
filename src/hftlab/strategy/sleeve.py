"""The sleeve interface (ADR 0014): a strategy is a pure function, folded over the events it is shown.

    decide(state, event, now_ns, view) -> (state, actions)

  state     everything the sleeve remembers of the events so far. It is a value the sleeve returns and is
            handed back; the sleeve keeps nothing anywhere else.
  event     the next event: a quote, a heartbeat, a change of connection, or news of the sleeve's own orders
            (`Placed`, `Refused`, and the venue's `OrderEvent` and `Fill`).
  now_ns    the receive time of that event. It is the only clock a sleeve has, so a decision is the same
            whether the event came from the wire a moment ago or from a recording a year later.
  view      the sleeve's own book, built from fills: what it holds, never what it believes it holds.

A sleeve is handed no venue, no clock object, no file and no random generator. It returns what it wants done
(`Intent`, `Cancel`) and whoever runs it decides where that goes: the shadow venue today, a risk engine and a
broker later. The order's id is made by the runner from the decision, so the same decision is the same order.

A sleeve may also say how its state is written down and read back (`Saves`, ADR 0026):

    save(state)     -> plain values a JSON line holds exactly: whole numbers, text, lists and mappings of those
    restore(saved)  -> the state `save` was given, or `StateError`

The two are exact inverses, and `saved` carries the sleeve's settings: a state written by a sleeve with other
settings, or one that is not whole, is refused and never read as something near it. The sleeve still touches
no file. Whoever runs it decides whether a saved state may be believed and where it is kept.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable

from hftlab.book import SleeveView
from hftlab.core import Fill, OrderEvent, Quote, Side, TimeInForce, _checks
from hftlab.record.format import Heartbeat, Marker
from hftlab.venues.port import ConnectionEvent

ENTRY, EXIT = "entry", "exit"


@dataclass(frozen=True, slots=True)
class Intent:
    """An order the sleeve wants: a market order, or a limit order when `limit_price` is given."""

    pair: str
    side: Side
    quantity: int
    leg: str = ENTRY
    attempt: int = 0
    tif: TimeInForce = TimeInForce.FOK
    limit_price: int | None = None
    expected_micro: int | None = None  # what a model expected of it after costs; journalled, and routed on by nothing

    def __post_init__(self) -> None:
        _checks.pair(self.pair)
        _checks.member("side", self.side, Side)
        _checks.positive("quantity", self.quantity)
        _checks.token("leg", self.leg)
        _checks.non_negative("attempt", self.attempt)
        _checks.member("tif", self.tif, TimeInForce)
        if self.limit_price is not None:
            _checks.positive("limit_price", self.limit_price)
        if self.expected_micro is not None and type(self.expected_micro) is not int:
            raise ValueError("what a model expected of an order is a whole number of millionths")


@dataclass(frozen=True, slots=True)
class Cancel:
    """Take back one of the sleeve's own orders, named by the id it was told in `Placed`."""

    client_order_id: str


@dataclass(frozen=True, slots=True)
class Placed:
    """The runner sent this intent as an order with this id. What happens to it follows as order events."""

    intent: Intent
    client_order_id: str


@dataclass(frozen=True, slots=True)
class Refused:
    """The runner did not send this: the venue, or later the risk engine, would not take it."""

    action: Intent | Cancel
    reason: str


Action = Intent | Cancel
Event = Quote | Heartbeat | ConnectionEvent | Marker | Placed | Refused | OrderEvent | Fill

S = TypeVar("S")


class Sleeve(Protocol[S]):
    @property
    def name(self) -> str:
        """Lower-case words joined by hyphens. It is readable in every order id the sleeve causes."""
        ...

    def initial(self) -> S:
        """The state before any event."""
        ...

    def decide(self, state: S, event: Event, now_ns: int, view: SleeveView) -> tuple[S, Sequence[Action]]:
        """The state after `event`, and what the sleeve wants done about it. No side effects."""
        ...


# ---- a state written down and read back ----------------------------------------------------------------------------


class StateError(ValueError):
    """A saved state that is not this sleeve's: written with other settings, by another version, or not whole."""


@runtime_checkable
class Saves(Protocol):
    """What a sleeve has when its state can be kept across a restart. Optional: a sleeve without it starts cold."""

    def save(self, state: Any) -> dict[str, object]:
        """The state as plain values, with the settings it was reached under."""
        ...

    def restore(self, saved: Mapping[str, object]) -> Any:
        """The state `save` wrote. Raises `StateError` unless a sleeve with these settings wrote it, whole."""
        ...


def opened(saved: object, kind: str, version: int, settings: Mapping[str, object]) -> Any:
    """The `state` part of what a sleeve of this kind saved, once its kind, version and settings are these."""
    if not isinstance(saved, Mapping) or set(saved) != {"kind", "version", "settings", "state"}:
        raise StateError("not a saved sleeve state")
    if saved["kind"] != kind or saved["version"] != version:
        raise StateError(f"the saved state is not a version {version} state of a {kind} sleeve")
    if saved["settings"] != settings:
        raise StateError("the saved state belongs to a sleeve with other settings")
    return saved["state"]


def whole(value: object, what: str) -> int:
    """A saved whole number that is not negative. A bool, a float or a text is not one."""
    if type(value) is not int or value < 0:
        raise StateError(f"a saved state has no whole, non-negative number for {what}: {value!r}")
    return value


def whole_or_none(value: object, what: str) -> int | None:
    return None if value is None else whole(value, what)


def one_of(value: object, choices: Sequence[str], what: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise StateError(f"a saved state's {what} is one of {', '.join(choices)}, not {value!r}")
    return value


def text_or_none(value: object, what: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise StateError(f"a saved state's {what} is text or nothing, not {value!r}")
    return value


def flag(value: object, what: str) -> bool:
    if value not in (0, 1) or type(value) is not int:
        raise StateError(f"a saved state's {what} is 0 or 1, not {value!r}")
    return value == 1
