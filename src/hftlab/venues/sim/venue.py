"""The shadow venue: orders filled against quotes it is fed, under an explicit cost model (ADR 0003, ADR 0013).

It has no broker and no network. Quotes come from outside, recorded or live, through `feed`; orders are
answered here. The model is pessimistic where a paper account is optimistic:

  * An order arrives after a delay, and meets the quote that exists then, not the one the decision saw.
  * A marketable order crosses the spread, pays slippage, and gets no more than the size on display. The rest
    of an immediate order is cancelled; the rest of a resting one waits for the next quote.
  * A resting limit order fills only when the opposite side trades THROUGH its price, never at the touch, and
    then at its own price, for no more than the size on display on the side that came through. The rest goes
    on resting.
  * Nothing fills on a quote the venue has not confirmed lately, nor inside the rollover window, and near the
    rollover the spread is taken as wider than quoted.
  * Commission is a rate on the order's traded value with a minimum per order.

It implements the venue port, so code written against the port runs on it unchanged, and it is the registered
venue `sim`: exempt from the paper-only lock because there is nothing on the other side of it.
"""
from __future__ import annotations

import heapq
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Final, Protocol

from hftlab.calendar import Calendar
from hftlab.core import (
    Clock,
    Fill,
    Instrument,
    InstrumentKind,
    Liquidity,
    Order,
    OrderEvent,
    OrderState,
    Quote,
    Side,
    TimeInForce,
)
from hftlab.venues.port import (
    Capabilities,
    CommissionEvent,
    ConnectionEvent,
    ConnectionState,
    NotReady,
    OpenOrder,
    OrderRefused,
    UnknownInstrument,
    UnknownOrder,
    Unsupported,
    Venue,
    VenueEvent,
    VenueNotice,
    VenuePosition,
)
from hftlab.venues.sim.cost import NO_FILL, WIDENED, CostModel

NAME: Final = "sim"
_IMMEDIATE: Final = (TimeInForce.IOC, TimeInForce.FOK)
_UP: Final = (ConnectionState.RECONCILING, ConnectionState.READY)


class Latency(Protocol):
    """How long an order takes from the decision to the venue. Injected, so a test can pin it."""

    def sample_ns(self, rng: random.Random) -> int: ...


@dataclass(frozen=True, slots=True)
class UniformLatency:
    min_ns: int
    max_ns: int

    def sample_ns(self, rng: random.Random) -> int:
        return rng.randint(self.min_ns, self.max_ns)


@dataclass(slots=True)
class _Ticket:
    order: Order
    instrument: Instrument
    arrival_ns: int
    number: int
    arrived: bool = False
    active: bool = True
    aggressive: bool = False
    filled: int = 0
    notional_micro: int = 0
    charged_micro: int = 0


class SimVenue:
    def __init__(
        self,
        clock: Clock,
        instruments: Iterable[Instrument],
        model: CostModel,
        *,
        calendar: Calendar | None = None,
        seed: int = 0,
        latency: Latency | None = None,
    ) -> None:
        self._clock, self.model, self._calendar = clock, model, calendar or Calendar()
        self._instruments = {(i.pair, i.kind): i for i in instruments}
        self._latency = latency or UniformLatency(model.latency_min_ns, model.latency_max_ns)
        self._rng = random.Random(seed)
        self._seed = seed
        self._state = ConnectionState.DISCONNECTED
        self._orders: dict[str, _Ticket] = {}
        self._due: list[tuple[int, int, str, str]] = []
        self._scheduled = 0
        self._top: dict[str, Quote] = {}
        self._fresh: dict[str, int] = {}
        self._shown: dict[str, tuple[int, int, int, int]] = {}
        self._subscribed: set[str] = set()
        # What happened and has not been reported yet: each makes its event when `poll` gives it a time and a number.
        self._pending: list[Callable[[int, int], VenueEvent]] = []
        self._fills: list[Fill] = []
        self._positions: dict[tuple[str, InstrumentKind], int] = {}
        self._seq = 0

    # ---- the port: connection and identity ----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return NAME

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(frozenset(kind for _, kind in self._instruments), depth=False, historical_quotes=False)

    def connect(self) -> None:
        if self._state not in _UP:
            for state in (ConnectionState.CONNECTING, ConnectionState.RECONCILING):
                self._set(state)

    def disconnect(self) -> None:
        self._set(ConnectionState.DISCONNECTED)

    def _set(self, state: ConnectionState) -> None:
        self._state = state
        self._pending.append(lambda now, seq: ConnectionEvent(state, now, seq))

    def mark_reconciled(self) -> None:
        if self._state is ConnectionState.RECONCILING:
            self._set(ConnectionState.READY)
        elif self._state is not ConnectionState.READY:
            raise NotReady(f"there is no connection to reconcile: the venue is {self._state.name}")

    def managed_accounts(self) -> tuple[str, ...]:
        return ()

    def server_time_ns(self) -> int:
        return self._clock.now_ns()

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument:
        found = self._instruments.get((pair, kind))
        if found is None:
            raise UnknownInstrument(f"the shadow venue has no {pair} as {kind.value}")
        return found

    def subscribe_quotes(self, instrument: Instrument) -> None:
        self._known(instrument)
        self._subscribed.add(instrument.pair)
        self._shown.pop(instrument.pair, None)

    def unsubscribe_quotes(self, instrument: Instrument) -> None:
        self._subscribed.discard(instrument.pair)

    def subscribe_depth(self, instrument: Instrument, levels: int) -> None:
        raise Unsupported("the shadow venue has no market depth")

    def unsubscribe_depth(self, instrument: Instrument) -> None:
        return None

    def historical_quotes(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]:
        raise Unsupported("the shadow venue has no history of its own")

    # ---- quotes come in from outside ----------------------------------------------------------------------------

    def feed(self, quote: Quote) -> None:
        """Take the next quote. Orders that arrived before it meet the book as it was; then this is the book."""
        at = quote.recv_ts_ns
        self._advance(at - 1)
        self._top[quote.pair] = quote
        self._fresh[quote.pair] = at
        self._advance(at)
        for ticket in list(self._orders.values()):
            if ticket.active and ticket.arrived and ticket.order.pair == quote.pair and ticket.arrival_ns < at:
                self._work(ticket, at, arriving=False)
        if quote.pair in self._subscribed:
            top = (quote.bid, quote.ask, quote.bid_size, quote.ask_size)
            if self._shown.get(quote.pair) != top:
                self._shown[quote.pair] = top
                self._pending.append(lambda now, seq: Quote(quote.pair, *top, now, seq))

    def quiet(self, pair: str, at_ns: int) -> None:
        """The source confirmed that this pair's book has not changed: its last quote is not stale."""
        if pair in self._top:
            self._fresh[pair] = max(self._fresh[pair], at_ns)

    def poll(self, timeout_ns: int = 0) -> list[VenueEvent]:
        """Everything that happened up to the clock's time, stamped with it, in order."""
        now = self._clock.now_ns()
        self._advance(now)
        events: list[VenueEvent] = []
        for make in self._pending:
            self._seq += 1
            events.append(make(now, self._seq))
        self._pending = []
        return events

    # ---- orders -------------------------------------------------------------------------------------------------

    def place(self, order: Order, instrument: Instrument) -> None:
        if self._state is not ConnectionState.READY:
            raise NotReady(f"orders are refused while the venue is {self._state.name}")
        self._known(instrument)
        if order.pair != instrument.pair:
            raise OrderRefused(f"a {order.pair} order cannot be sent as {instrument.pair}")
        if reasons := instrument.refusals(order.quantity, order.limit_price):
            raise OrderRefused("; ".join(reasons))
        if order.client_order_id in self._orders:
            raise OrderRefused(f"client order id {order.client_order_id} was already sent on this venue")
        self._scheduled += 1
        arrival = self._clock.now_ns() + self._latency.sample_ns(self._rng)
        self._orders[order.client_order_id] = _Ticket(order, instrument, arrival, self._scheduled)
        heapq.heappush(self._due, (arrival, self._scheduled, "arrive", order.client_order_id))
        self._report(order.client_order_id, OrderState.SUBMITTED)

    def cancel(self, client_order_id: str) -> None:
        if self._state not in _UP:
            raise NotReady(f"nothing can be cancelled while the venue is {self._state.name}")
        if client_order_id not in self._orders:
            raise UnknownOrder(f"the shadow venue holds no order {client_order_id}")
        self._scheduled += 1
        arrival = self._clock.now_ns() + self._latency.sample_ns(self._rng)
        heapq.heappush(self._due, (arrival, self._scheduled, "cancel", client_order_id))

    def open_orders(self) -> tuple[OpenOrder, ...]:
        out = []
        for ticket in self._orders.values():
            if ticket.active:
                order = ticket.order
                state = OrderState.PARTIALLY_FILLED if ticket.filled else OrderState.WORKING
                out.append(
                    OpenOrder(
                        client_order_id=order.client_order_id,
                        broker_order_id=f"S{ticket.number:06d}",
                        pair=order.pair,
                        side=order.side,
                        quantity=order.quantity,
                        filled_quantity=ticket.filled,
                        order_type=order.order_type,
                        limit_price=order.limit_price,
                        tif=order.tif,
                        state=state if ticket.arrived else OrderState.SUBMITTED,
                    )
                )
        return tuple(out)

    def positions(self) -> tuple[VenuePosition, ...]:
        return tuple(VenuePosition(pair, kind, qty) for (pair, kind), qty in sorted(self._positions.items()) if qty)

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        now = self._clock.now_ns()
        return tuple(replace(f, ts_ns=now, seq=n) for n, f in enumerate(f for f in self._fills if f.ts_ns >= ts_ns))

    # ---- what happens inside ------------------------------------------------------------------------------------

    def _known(self, instrument: Instrument) -> None:
        if self._instruments.get((instrument.pair, instrument.kind)) != instrument:
            raise UnknownInstrument(f"{instrument.pair} {instrument.kind.value} is not one of this venue's instruments")

    def _advance(self, to_ns: int) -> None:
        while self._due and self._due[0][0] <= to_ns:
            at, _, what, client_order_id = heapq.heappop(self._due)
            ticket = self._orders[client_order_id]
            if what == "cancel":
                if ticket.active:
                    self._end(ticket, at, "cancelled on request")
                else:
                    self._too_late(client_order_id)
            elif ticket.active:
                ticket.arrived = True
                if ticket.order.quantity < self.model.min_order_size:
                    ticket.active = False
                    reason = f"below the cost model's minimum order size of {self.model.min_order_size}"
                    self._report(client_order_id, OrderState.REJECTED, reason=reason)
                    continue
                self._report(client_order_id, OrderState.WORKING, broker=f"S{ticket.number:06d}")
                self._work(ticket, at, arriving=True)

    def _work(self, ticket: _Ticket, at: int, *, arriving: bool) -> None:
        """Try to fill an order at time `at`: on arrival, or against a quote that came after it arrived."""
        order = ticket.order
        immediate = order.tif in _IMMEDIATE

        def give_up(reason: str) -> None:
            if immediate and arriving:
                self._end(ticket, at, reason)

        quote = self._top.get(order.pair)
        if quote is None:
            return give_up("no quote to trade against")
        phase = self.model.phase(at)
        if phase == NO_FILL or not self._calendar.is_open(at):
            return give_up("inside the rollover window or the market is shut")
        if at - self._fresh[order.pair] > self.model.stale_after_ns:
            return give_up("the quote is stale")
        buying = order.side is Side.BUY
        touch, displayed = (quote.ask, quote.ask_size) if buying else (quote.bid, quote.bid_size)
        limit, remaining = order.limit_price, order.quantity - ticket.filled
        marketable = limit is None or (limit >= touch if buying else limit <= touch)
        available = min(remaining, displayed)
        if arriving:
            ticket.aggressive = marketable
        if not ticket.aggressive:
            if arriving:
                return give_up("not immediately executable")
            assert limit is not None
            through = quote.ask < limit if buying else quote.bid > limit  # traded through, not merely touched
            if through and available > 0:
                self._fill(ticket, available, limit, Liquidity.MAKER, at)
            return None
        price = self.model.taker_price(order.side, quote.bid, quote.ask, ticket.instrument, widened=phase == WIDENED)
        beyond = limit is not None and (price > limit if buying else price < limit)
        if not marketable or beyond or available <= 0 or (order.tif is TimeInForce.FOK and available < remaining):
            return give_up("not immediately executable")
        self._fill(ticket, available, price, Liquidity.TAKER, at)
        if immediate and ticket.active:
            self._end(ticket, at, "unfilled remainder of an immediate order")
        return None

    def _too_late(self, client_order_id: str) -> None:
        message = "the order was already done"
        self._pending.append(lambda now, seq: VenueNotice("cancel-too-late", message, now, seq, client_order_id))

    def _report(
        self,
        client_order_id: str,
        state: OrderState,
        filled: int = 0,
        *,
        broker: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._pending.append(lambda now, seq: OrderEvent(client_order_id, state, now, seq, filled, broker, reason))

    def _fill(self, ticket: _Ticket, quantity: int, price: int, liquidity: Liquidity, at: int) -> None:
        order, instrument = ticket.order, ticket.instrument
        ticket.filled += quantity
        ticket.notional_micro += self.model.notional_micro(price, quantity, instrument)
        due = self.model.commission_micro(ticket.notional_micro, instrument.quote)
        charged, ticket.charged_micro = due - ticket.charged_micro, due
        done = ticket.filled == order.quantity
        ticket.active = not done
        key = (order.pair, instrument.kind)
        self._positions[key] = self._positions.get(key, 0) + order.side.sign * quantity
        fill_id = f"S{self._seed:04d}.{len(self._fills) + 1:06d}"
        # Kept with the venue's own time; the event carries the time it is delivered, like any other venue's.
        executed = Fill(fill_id, order.client_order_id, order.pair, order.side, quantity, price, at, liquidity)
        executed = replace(executed, commission=charged, commission_currency=instrument.quote, venue_ts_ns=at)
        self._fills.append(executed)
        bare = replace(executed, commission=0, commission_currency=None)  # its commission follows as its own event
        self._pending.append(lambda now, seq: replace(bare, ts_ns=now, seq=seq))
        state = OrderState.FILLED if done else OrderState.PARTIALLY_FILLED
        self._report(order.client_order_id, state, ticket.filled, broker=f"S{ticket.number:06d}")
        self._pending.append(lambda now, seq: CommissionEvent(fill_id, charged, instrument.quote, now, seq))

    def _end(self, ticket: _Ticket, at: int, reason: str) -> None:
        ticket.active = False
        self._report(ticket.order.client_order_id, OrderState.CANCELLED, ticket.filled, reason=reason)


def _is_a_venue(venue: SimVenue) -> Venue:
    return venue  # checked by mypy: the shadow venue satisfies the port
