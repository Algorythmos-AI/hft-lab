"""What is the same for every venue: the paper lock in stages, the connection states, the reconcile gate.

A venue adapter subclasses `GuardedVenue` and fills in the underscore methods, which only talk to the broker.
Everything that decides *whether* to talk to it lives here, once, and a subclass cannot replace it: the public
methods are sealed. So there is one path to `_send`, and on that path the connection has passed the full lock
since it was opened, the caller has reconciled it, and the lock has just been checked again.

  connect()          stage one, then `_open()`, then stage two on the accounts the broker reported
  mark_reconciled()  RECONCILING -> READY
  place()            READY, instrument and increments, an unused id, stage three, then `_send()`
  poll()             `_pump()`, and a reconnect attempt when one is due

A connection that drops (`_lost`) takes its permit with it. The reconnect goes through `connect()`'s own path,
so it is unchecked until stage two passes again, and RECONCILING until the caller says otherwise.
"""
from __future__ import annotations

import abc
import contextlib
from collections import deque
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar, Final

from hftlab.core import Clock, Fill, Instrument, InstrumentKind, Liquidity, Order, OrderEvent, OrderState, Quote, Side
from hftlab.safety.gate import PaperGate, Permit
from hftlab.safety.lock import PaperOnlyError
from hftlab.venues.port import (
    Capabilities,
    CommissionEvent,
    ConnectionEvent,
    ConnectionState,
    DepthSnapshot,
    NotReady,
    OpenOrder,
    OrderRefused,
    UnknownInstrument,
    Unsupported,
    VenueError,
    VenueEvent,
    VenueNotice,
    VenuePosition,
    VenueUnavailable,
)
from hftlab.venues.supervisor import Backoff

_CONNECTED: Final = (ConnectionState.RECONCILING, ConnectionState.READY)


class GuardedVenue(abc.ABC):
    """The sealed half of a venue. See the module docstring for what it guarantees."""

    SEALED: ClassVar[tuple[str, ...]] = (
        "connect", "disconnect", "mark_reconciled", "poll", "place", "cancel", "state", "managed_accounts",
        "instrument", "subscribe_quotes", "unsubscribe_quotes", "subscribe_depth", "unsubscribe_depth",
        "historical_quotes", "open_orders", "positions", "executions_since", "server_time_ns",
    )  # fmt: skip

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        replaced = sorted(name for name in GuardedVenue.SEALED if name in cls.__dict__)
        if replaced:
            raise TypeError(f"{cls.__name__} may not replace the guarded methods: {', '.join(replaced)}")

    def __init__(self, *, clock: Clock, gate: PaperGate, backoff: Backoff | None = None) -> None:
        self._clock = clock
        self._gate = gate
        self._backoff = backoff
        self._state = ConnectionState.DISCONNECTED
        self._permit: Permit | None = None
        self._events: deque[VenueEvent] = deque()
        self._seq = 0
        self._wanted = False
        self._attempt = 0
        self._retry_at: int | None = None
        self._instruments: set[Instrument] = set()
        self._quotes: dict[str, Instrument] = {}
        self._depth: dict[str, tuple[Instrument, int]] = {}
        self._last_quote: dict[str, tuple[int, int, int, int]] = {}
        self._sent: set[str] = set()

    # ---- what a subclass provides: talking to the broker, and nothing else ------------------------------------------

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @property
    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abc.abstractmethod
    def _port(self) -> int:
        """The port this venue dials. It must be the port the lock was given to judge."""

    @abc.abstractmethod
    def _open(self) -> Sequence[str]:
        """Open the transport, complete the handshake and return the managed account ids. Ask nothing else."""

    @abc.abstractmethod
    def _close(self) -> None: ...

    @abc.abstractmethod
    def _accounts(self) -> Sequence[str]:
        """The managed account ids of the open connection, as the broker reports them now."""

    @abc.abstractmethod
    def _pump(self, timeout_ns: int) -> None:
        """Do I/O for up to `timeout_ns` and `_emit_*` what arrived. Call `_lost` if the connection is gone."""

    @abc.abstractmethod
    def _lookup(self, pair: str, kind: InstrumentKind) -> Instrument: ...

    @abc.abstractmethod
    def _start_quotes(self, instrument: Instrument) -> None: ...

    @abc.abstractmethod
    def _stop_quotes(self, instrument: Instrument) -> None: ...

    @abc.abstractmethod
    def _start_depth(self, instrument: Instrument, levels: int) -> None: ...

    @abc.abstractmethod
    def _stop_depth(self, instrument: Instrument) -> None: ...

    @abc.abstractmethod
    def _history(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]: ...

    @abc.abstractmethod
    def _send(self, order: Order, instrument: Instrument, permit: Permit) -> None:
        """Send the order. Called from `place` only, after every check; `permit` is the proof."""

    @abc.abstractmethod
    def _cancel(self, client_order_id: str, permit: Permit) -> None: ...

    @abc.abstractmethod
    def _open_orders(self) -> tuple[OpenOrder, ...]: ...

    @abc.abstractmethod
    def _positions(self) -> tuple[VenuePosition, ...]: ...

    @abc.abstractmethod
    def _executions_since(self, ts_ns: int) -> tuple[Fill, ...]: ...

    @abc.abstractmethod
    def _server_time_ns(self) -> int: ...

    # ---- connection ---------------------------------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    def connect(self) -> None:
        if self._state in _CONNECTED:
            return
        self._refuse_if_refused()
        self._wanted = True
        self._attempt_connection()

    def disconnect(self) -> None:
        self._wanted = False
        self._retry_at = None
        if self._state is ConnectionState.REFUSED:
            return
        self._drop_permit()
        self._close()
        self._set(ConnectionState.DISCONNECTED, "disconnected on request")

    def mark_reconciled(self) -> None:
        if self._state is ConnectionState.READY:
            return
        if self._state is not ConnectionState.RECONCILING:
            raise NotReady(f"there is no connection to reconcile: the venue is {self._state.name}")
        self._set(ConnectionState.READY)

    def poll(self, timeout_ns: int = 0) -> list[VenueEvent]:
        if self._state is not ConnectionState.REFUSED:
            self._pump(timeout_ns)
            due = self._retry_at is not None and self._clock.now_ns() >= self._retry_at
            if self._state is ConnectionState.DISCONNECTED and self._wanted and due:
                # A failure here is not the caller's exception: the event stream already says which, and why.
                with contextlib.suppress(VenueUnavailable, PaperOnlyError):
                    self._attempt_connection()
        events = list(self._events)
        self._events.clear()
        return events

    def _attempt_connection(self) -> None:
        try:
            if self._port() != self._gate.port:
                raise PaperOnlyError("the port being dialled is not the port the paper-only lock was given")
            self._gate.before_connect()
        except PaperOnlyError as exc:
            self._refused(str(exc))
            raise
        self._set(ConnectionState.CONNECTING)
        try:
            accounts = tuple(self._open())
        except VenueUnavailable as exc:
            self._schedule_retry()
            self._set(ConnectionState.DISCONNECTED, str(exc))
            raise
        try:
            self._permit = self._gate.after_handshake(accounts)
        except PaperOnlyError as exc:
            self._close()
            self._refused(str(exc))
            raise
        self._attempt = 0
        self._retry_at = None
        self._last_quote.clear()  # the first quote of a new connection is news even if the book did not move
        self._set(ConnectionState.RECONCILING)
        for instrument in self._quotes.values():
            self._start_quotes(instrument)
        for instrument, levels in self._depth.values():
            self._start_depth(instrument, levels)

    def _lost(self, reason: str) -> None:
        """The connection is gone. Its permit goes with it; a retry is scheduled if the caller wants to be connected."""
        if self._state not in (*_CONNECTED, ConnectionState.CONNECTING):
            return
        self._drop_permit()
        if self._wanted:
            self._schedule_retry()
        self._set(ConnectionState.DISCONNECTED, reason)

    def _retry_now(self) -> None:
        """A subclass has learned the venue is reachable again; do not wait out the backoff."""
        if self._state is ConnectionState.DISCONNECTED and self._wanted:
            self._retry_at = self._clock.now_ns()

    def _schedule_retry(self) -> None:
        self._attempt += 1
        if self._backoff is None:
            self._retry_at = None
        else:
            self._retry_at = self._clock.now_ns() + self._backoff.delay_ns(self._attempt)

    def _drop_permit(self) -> None:
        self._permit = None
        self._gate.connection_closed()

    def _refused(self, reason: str) -> None:
        self._permit = None
        self._wanted = False
        self._retry_at = None
        self._set(ConnectionState.REFUSED, reason)

    def _refuse_if_refused(self) -> None:
        if self._state is ConnectionState.REFUSED:
            raise PaperOnlyError(f"the paper-only lock refused this venue: {self._gate.refusal}")

    def _require_connected(self) -> None:
        self._refuse_if_refused()
        if self._state not in _CONNECTED:
            raise VenueUnavailable(f"the venue is {self._state.name}")

    def _check_lock(self) -> Permit:
        """Stage three. Returns the current permit, or raises; a refusal closes the connection for good."""
        try:
            self._gate.before_order(self._permit, tuple(self._accounts()))
        except PaperOnlyError as exc:
            if self._gate.refusal is not None:
                self._close()
                self._refused(str(exc))
            raise
        assert self._permit is not None
        return self._permit

    # ---- orders -------------------------------------------------------------------------------------------------

    def place(self, order: Order, instrument: Instrument) -> None:
        self._refuse_if_refused()
        if self._state is not ConnectionState.READY:
            raise NotReady(f"orders are refused while the venue is {self._state.name}")
        self._require_known(instrument)
        if order.pair != instrument.pair:
            raise OrderRefused(f"a {order.pair} order cannot be sent as {instrument.pair}")
        if reasons := instrument.refusals(order.quantity, order.limit_price):
            raise OrderRefused("; ".join(reasons))
        if order.client_order_id in self._sent:
            raise OrderRefused(f"client order id {order.client_order_id} was already sent on this venue")
        permit = self._check_lock()
        # Recorded before the send: if the send fails half way, the id may be at the venue and is never reused.
        self._sent.add(order.client_order_id)
        self._send(order, instrument, permit)
        self._emit_order(order.client_order_id, OrderState.SUBMITTED)

    def cancel(self, client_order_id: str) -> None:
        self._refuse_if_refused()
        if self._state not in _CONNECTED:
            raise NotReady(f"nothing can be cancelled while the venue is {self._state.name}")
        self._cancel(client_order_id, self._check_lock())

    # ---- lookups, subscriptions and queries: all need a checked connection ----------------------------------------

    def managed_accounts(self) -> tuple[str, ...]:
        self._require_connected()
        return tuple(self._accounts())

    def server_time_ns(self) -> int:
        self._require_connected()
        return self._server_time_ns()

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument:
        self._require_connected()
        if kind not in self.capabilities.kinds:
            raise UnknownInstrument(f"{self.name} does not offer {kind.value} instruments")
        found = self._lookup(pair, kind)
        self._instruments.add(found)
        return found

    def subscribe_quotes(self, instrument: Instrument) -> None:
        self._require_connected()
        self._require_known(instrument)
        current = self._quotes.get(instrument.pair)
        if current == instrument:
            return
        if current is not None:
            raise VenueError(f"{instrument.pair} quotes are already subscribed as {current.kind.value}")
        self._start_quotes(instrument)
        self._quotes[instrument.pair] = instrument
        self._last_quote.pop(instrument.pair, None)

    def unsubscribe_quotes(self, instrument: Instrument) -> None:
        if self._quotes.get(instrument.pair) == instrument:
            del self._quotes[instrument.pair]
            if self._state in _CONNECTED:
                self._stop_quotes(instrument)

    def subscribe_depth(self, instrument: Instrument, levels: int) -> None:
        self._require_connected()
        self._require_known(instrument)
        if not self.capabilities.depth:
            raise Unsupported(f"{self.name} offers no market depth")
        if levels < 1:
            raise ValueError("depth needs at least one level")
        self._start_depth(instrument, levels)
        self._depth[instrument.pair] = (instrument, levels)

    def unsubscribe_depth(self, instrument: Instrument) -> None:
        if self._depth.get(instrument.pair, (None, 0))[0] == instrument:
            del self._depth[instrument.pair]
            if self._state in _CONNECTED:
                self._stop_depth(instrument)

    def historical_quotes(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]:
        self._require_connected()
        self._require_known(instrument)
        if not self.capabilities.historical_quotes:
            raise Unsupported(f"{self.name} offers no historical quotes")
        return self._history(instrument, end_ts_ns, count)

    def open_orders(self) -> tuple[OpenOrder, ...]:
        self._require_connected()
        return self._open_orders()

    def positions(self) -> tuple[VenuePosition, ...]:
        self._require_connected()
        return self._positions()

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        self._require_connected()
        return self._executions_since(ts_ns)

    def _require_known(self, instrument: Instrument) -> None:
        if instrument not in self._instruments:
            raise UnknownInstrument(f"{instrument.pair} {instrument.kind.value} was not looked up on this venue")

    # ---- events: every one gets the time it was received and the next sequence number -----------------------------

    def _next(self) -> tuple[int, int]:
        self._seq += 1
        return self._clock.now_ns(), self._seq

    def _set(self, state: ConnectionState, reason: str = "") -> None:
        self._state = state
        ts_ns, seq = self._next()
        self._events.append(ConnectionEvent(state, ts_ns, seq, reason))

    def _subscribed(self, pair: str) -> bool:
        return pair in self._quotes

    def _emit_quote(self, pair: str, bid: int, ask: int, bid_size: int, ask_size: int) -> None:
        """A quote, when the top of the book changed. A venue that repeats itself is not telling us anything."""
        top = (bid, ask, bid_size, ask_size)
        if self._last_quote.get(pair) == top:
            return
        self._last_quote[pair] = top
        ts_ns, seq = self._next()
        self._events.append(Quote(pair, bid, ask, bid_size, ask_size, ts_ns, seq))

    def _emit_depth(self, pair: str, bids: tuple[tuple[int, int], ...], asks: tuple[tuple[int, int], ...]) -> None:
        ts_ns, seq = self._next()
        self._events.append(DepthSnapshot(pair, bids, asks, ts_ns, seq))

    def _emit_order(
        self,
        client_order_id: str,
        state: OrderState,
        *,
        filled_quantity: int = 0,
        broker_order_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        ts_ns, seq = self._next()
        self._events.append(OrderEvent(client_order_id, state, ts_ns, seq, filled_quantity, broker_order_id, reason))

    def _emit_fill(
        self,
        *,
        fill_id: str,
        client_order_id: str,
        pair: str,
        side: Side,
        quantity: int,
        price: int,
        liquidity: Liquidity,
        venue_ts_ns: int | None,
    ) -> None:
        ts_ns, seq = self._next()
        fill = Fill(fill_id, client_order_id, pair, side, quantity, price, ts_ns, liquidity)
        self._events.append(replace(fill, venue_ts_ns=venue_ts_ns, seq=seq))

    def _emit_commission(self, fill_id: str, commission: int, currency: str) -> None:
        ts_ns, seq = self._next()
        self._events.append(CommissionEvent(fill_id, commission, currency, ts_ns, seq))

    def _emit_notice(self, code: str, message: str, client_order_id: str | None = None) -> None:
        ts_ns, seq = self._next()
        self._events.append(VenueNotice(code, message, ts_ns, seq, client_order_id))
