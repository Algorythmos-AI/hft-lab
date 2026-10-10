"""The venue port: everything the platform asks of a broker, in core types (ADR 0009, ADR 0011).

The port is synchronous and pumped. A command (`place`, `cancel`, `subscribe_quotes`) returns at once; what the
venue has to say comes back from `poll()` as an ordered list of events, each stamped with the time this process
received it and a sequence number. The caller owns the loop: poll, hand the events to code that is a pure
function of them, send the commands it returns. No callback, thread or coroutine crosses this boundary, which
is what lets a recorded day be replayed through the same code (ADR 0005).

What a caller may rely on:

  * A new connection is RECONCILING, never READY. Orders are refused until the caller has looked at open
    orders, positions and executions and called `mark_reconciled()`. Cancels are allowed while RECONCILING.
  * Nothing is sent while DISCONNECTED, and nothing at all once REFUSED: the paper-only lock said no.
  * An `OrderEvent` with state SUBMITTED is emitted when `place` returns; later states come from the venue.
  * A `Fill` may arrive more than once, and before or after the `OrderEvent` that counts it. `fill_id` is the
    key. Its commission arrives separately, as a `CommissionEvent`, and may be late.
  * Events lost while disconnected are not replayed. `executions_since` and `open_orders` are how to find out.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol

from hftlab.core import (
    Fill,
    Instrument,
    InstrumentKind,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Quote,
    Side,
    TimeInForce,
)


@unique
class ConnectionState(Enum):
    DISCONNECTED = "DISCONNECTED"  # no connection; a retry may be pending
    CONNECTING = "CONNECTING"  # stage one of the lock passed, the handshake is in progress
    RECONCILING = "RECONCILING"  # connected and shown to be paper; orders refused until the caller has reconciled
    READY = "READY"  # orders may be sent
    REFUSED = "REFUSED"  # the paper-only lock refused; terminal for this venue object


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    state: ConnectionState
    ts_ns: int
    seq: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CommissionEvent:
    """The commission of one fill, at CASH_SCALE. Reported by the venue after the fill itself."""

    fill_id: str
    commission: int
    currency: str
    ts_ns: int
    seq: int


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    """The visible book for one pair: (price, size) per level, best first."""

    pair: str
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]
    recv_ts_ns: int
    seq: int


@dataclass(frozen=True, slots=True)
class VenueNotice:
    """Something the venue said that is not an order event: a warning, a data-farm message, a late cancel."""

    code: str
    message: str
    ts_ns: int
    seq: int
    client_order_id: str | None = None


VenueEvent = ConnectionEvent | Quote | OrderEvent | Fill | CommissionEvent | DepthSnapshot | VenueNotice


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """A working order as the venue holds it. `client_order_id` is None for an order this platform did not send."""

    client_order_id: str | None
    broker_order_id: str
    pair: str
    side: Side
    quantity: int
    filled_quantity: int
    order_type: OrderType
    limit_price: int | None
    tif: TimeInForce
    state: OrderState


@dataclass(frozen=True, slots=True)
class VenuePosition:
    """The venue's own view of a position: signed base-currency units. Compared with ours, never trusted over it."""

    pair: str
    kind: InstrumentKind
    quantity: int


@dataclass(frozen=True, slots=True)
class Capabilities:
    kinds: frozenset[InstrumentKind]
    depth: bool
    historical_quotes: bool
    modify: bool = False  # an amended order is a new order with a new id; no venue offers modify through this port


class VenueError(Exception):
    """Base of everything a venue raises on purpose."""


class VenueUnavailable(VenueError):
    """There is no usable connection, or a request to the venue failed or timed out. Worth retrying later."""


class NotReady(VenueError):
    """The command needs a state the connection is not in (an order while RECONCILING, anything while REFUSED)."""


class OrderRefused(VenueError):
    """The order was refused here and never sent: it does not fit the instrument, or its id was already used."""


class UnknownInstrument(VenueError):
    """The venue has no such pair in that form, or the instrument did not come from this venue."""


class UnknownOrder(VenueError):
    """The venue holds no working order with that client order id."""


class Unsupported(VenueError):
    """The venue does not offer this capability."""


class Venue(Protocol):
    """What the rest of the platform depends on. Implemented by the broker adapter and by the fake broker."""

    @property
    def name(self) -> str: ...

    @property
    def state(self) -> ConnectionState: ...

    @property
    def capabilities(self) -> Capabilities: ...

    def connect(self) -> None:
        """Open a connection and pass the paper-only lock. Leaves the venue RECONCILING, or raises."""
        ...

    def disconnect(self) -> None: ...

    def mark_reconciled(self) -> None:
        """The caller has reconciled this connection: RECONCILING becomes READY."""
        ...

    def poll(self, timeout_ns: int = 0) -> list[VenueEvent]:
        """Let the venue do its I/O for up to `timeout_ns`, then return every event since the last call, in order."""
        ...

    def managed_accounts(self) -> tuple[str, ...]: ...

    def server_time_ns(self) -> int: ...

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument: ...

    def subscribe_quotes(self, instrument: Instrument) -> None:
        """Start `Quote` events for the instrument's pair. One form per pair at a time."""
        ...

    def unsubscribe_quotes(self, instrument: Instrument) -> None: ...

    def subscribe_depth(self, instrument: Instrument, levels: int) -> None: ...

    def unsubscribe_depth(self, instrument: Instrument) -> None: ...

    def historical_quotes(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]:
        """Up to `count` past quotes ending at `end_ts_ns`, oldest first. `recv_ts_ns` is the venue's tick time."""
        ...

    def place(self, order: Order, instrument: Instrument) -> None: ...

    def cancel(self, client_order_id: str) -> None: ...

    def open_orders(self) -> tuple[OpenOrder, ...]: ...

    def positions(self) -> tuple[VenuePosition, ...]: ...

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        """Fills the venue executed at or after `ts_ns` (its own time). One it cannot date is included."""
        ...
