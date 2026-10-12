"""The fake broker: the venue port over `FakeMarket`, in process, for tests and CI.

It goes through the same guard as the real adapter, so the paper lock, the connection states and the reconcile
gate behave identically; give it a live-looking account list and it is refused like a real one would be. It is
driven by the injected clock. Hand it a `SimClock` with `auto_advance=True` and `poll(timeout_ns)` moves time
forward to the next thing that happens, which is what lets a plain polling loop run against it unchanged.

While the link is down nothing is delivered: quotes in the gap are gone, and what happened to orders is found
afterwards with `open_orders` and `executions_since`, exactly as with a real broker.

It can also play a gateway that is up but cut off from the broker: `FakeConfig.gateway_messages` are the status
messages it sends, as notices with the gateway's own codes, and during `FakeConfig.unanswered_lookups` an
instrument lookup waits out its timeout and fails, as it does on a real gateway in that state.

And it can play the broker's servers turning an order down after the gateway acknowledged it
(`FakeConfig.late_rejections`): WORKING first, then REJECTED, and the reason as a notice with the gateway's
number, tied to the order, as the real gateway sends it.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from hftlab.core import Clock, Fill, Instrument, InstrumentKind, Order, OrderState, Quote, SimClock
from hftlab.safety.gate import PaperGate, Permit
from hftlab.venues.fake.market import (
    REFUSED,
    REJECTION_CODE,
    Acked,
    Cancelled,
    CancelTooLate,
    Commissioned,
    Executed,
    FakeConfig,
    FakeMarket,
    Happening,
    QuoteTick,
    Rejected,
    Said,
)
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.port import (
    Capabilities,
    ConnectionState,
    OpenOrder,
    UnknownInstrument,
    UnknownOrder,
    Venue,
    VenueCash,
    VenuePosition,
    VenueUnavailable,
)
from hftlab.venues.supervisor import Backoff


class FakeBroker(GuardedVenue):
    def __init__(
        self,
        clock: Clock,
        market: FakeMarket,
        *,
        gate: PaperGate | None = None,
        backoff: Backoff | None = None,
        auto_advance: bool = False,
    ) -> None:
        if auto_advance and not isinstance(clock, SimClock):
            raise TypeError("only a SimClock can be advanced by the fake broker")
        # Without a gate of its own the fake is judged against a fixed paper setting, not the process environment.
        default = PaperGate(port=market.config.port, env={"MODE": "paper"})
        super().__init__(clock=clock, gate=gate or default, backoff=backoff)
        self.market = market
        self._auto_advance = auto_advance
        self._link = False
        self._reported: dict[str, tuple[OrderState, int]] = {}
        self._pumped_ns = clock.now_ns()
        self._messages = sorted(market.config.gateway_messages, key=lambda said: said[0])
        self._said = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> Capabilities:
        config = self.market.config
        kinds = frozenset({InstrumentKind.CASH, InstrumentKind.CFD})
        return Capabilities(kinds, depth=config.depth_levels > 0, historical_quotes=config.history)

    # ---- connection ---------------------------------------------------------------------------------------------

    def _port(self) -> int:
        return self.market.config.port

    def _open(self) -> Sequence[str]:
        now = self._clock.now_ns()
        if not self.market.link_up(now):
            raise VenueUnavailable("the fake broker's link is down")
        self.market.advance(now)  # whatever happened while nobody was connected was never delivered
        self._pumped_ns = now
        self._link = True
        self._say_what_is_due(now)
        return self.market.config.accounts

    def _close(self) -> None:
        self._link = False

    def _accounts(self) -> Sequence[str]:
        return self.market.config.accounts

    def _pump(self, timeout_ns: int) -> None:
        if self._auto_advance and timeout_ns > 0:
            self._wait(timeout_ns)
        now = self._clock.now_ns()
        if not self._link:
            return
        self._say_what_is_due(now)
        down_at = self.market.outage_start(self._pumped_ns, now)
        for happening in self.market.advance(now):
            if down_at is None or happening.at_ns < down_at:
                self._deliver(happening)
        self._pumped_ns = now
        if down_at is not None:
            self._link = False
            self._lost("the fake broker's link went down")

    def _wait(self, timeout_ns: int) -> None:
        """Move a SimClock to the next thing that can happen, or by `timeout_ns` if nothing does sooner."""
        assert isinstance(self._clock, SimClock)
        now = self._clock.now_ns()
        candidates = [now + timeout_ns]
        if self._link and (next_at := self.market.next_at()) is not None:
            candidates.append(next_at)
        if self._link and self._said < len(self._messages):
            candidates.append(self._messages[self._said][0])
        if not self._link and self._retry_at is not None:
            candidates.append(self._retry_at)
        self._clock.advance_to(max(now, min(candidates)))

    def _say_what_is_due(self, now: int) -> None:
        """The gateway's status messages whose time has come, in order, each as a notice with the gateway's code."""
        while self._said < len(self._messages) and self._messages[self._said][0] <= now:
            _, code, text = self._messages[self._said]
            self._said += 1
            self._emit_notice(f"ib:{code}", text)

    # ---- what the broker says, as port events -------------------------------------------------------------------

    def _deliver(self, happening: Happening) -> None:
        if isinstance(happening, QuoteTick):
            self._deliver_quote(happening)
        elif isinstance(happening, Acked):
            self._report(happening.client_order_id, OrderState.WORKING, 0, happening.broker_order_id)
        elif isinstance(happening, Rejected):
            if happening.reason.startswith(REFUSED) and not happening.late:
                # A real gateway says it twice: as the order's last state, and as a message with its number.
                # (A late rejection's message is the market's to send, in its own time.)
                self._emit_notice(f"ib:{REJECTION_CODE}", happening.reason, happening.client_order_id)
            self._report(happening.client_order_id, OrderState.REJECTED, 0, None, happening.reason)
        elif isinstance(happening, Said):
            self._emit_notice(f"ib:{happening.code}", happening.text, happening.client_order_id)
        elif isinstance(happening, Executed):
            self._emit_fill(
                fill_id=happening.exec_id,
                client_order_id=happening.client_order_id,
                pair=happening.pair,
                side=happening.side,
                quantity=happening.quantity,
                price=happening.price,
                liquidity=happening.liquidity,
                venue_ts_ns=happening.at_ns,
            )
            done = happening.cumulative == happening.order_quantity
            state = OrderState.FILLED if done else OrderState.PARTIALLY_FILLED
            self._report(happening.client_order_id, state, happening.cumulative, happening.broker_order_id)
        elif isinstance(happening, Commissioned):
            self._emit_commission(happening.exec_id, happening.commission, happening.currency)
        elif isinstance(happening, Cancelled):
            cancelled = OrderState.CANCELLED
            self._report(happening.client_order_id, cancelled, happening.cumulative, None, happening.reason)
        elif isinstance(happening, CancelTooLate):
            self._emit_notice("cancel-too-late", "the order was already done", happening.client_order_id)

    def _report(
        self, client_order_id: str, state: OrderState, filled: int, broker_id: str | None, reason: str | None = None
    ) -> None:
        """An order event, unless it repeats the last one: a duplicated execution report is not a new status."""
        if self._reported.get(client_order_id) == (state, filled):
            return
        self._reported[client_order_id] = (state, filled)
        self._emit_order(client_order_id, state, filled_quantity=filled, broker_order_id=broker_id, reason=reason)

    def _deliver_quote(self, tick: QuoteTick) -> None:
        if self._subscribed(tick.pair):
            self._emit_quote(tick.pair, tick.bid, tick.ask, tick.bid_size, tick.ask_size)
        if tick.pair in self._depth:
            instrument, levels = self._depth[tick.pair]
            step = instrument.price_increment
            bids = tuple((tick.bid - i * step, tick.bid_size) for i in range(levels) if tick.bid - i * step > 0)
            asks = tuple((tick.ask + i * step, tick.ask_size) for i in range(levels))
            self._emit_depth(tick.pair, bids, asks)

    # ---- the broker-facing half of the port ---------------------------------------------------------------------

    def _lookup(self, pair: str, kind: InstrumentKind) -> Instrument:
        config, now = self.market.config, self._clock.now_ns()
        if any(start <= now < end for start, end in config.unanswered_lookups):
            # Cut off from the broker: the request is accepted and never answered, and the wait runs out.
            if self._auto_advance:
                assert isinstance(self._clock, SimClock)
                self._clock.advance(config.lookup_timeout_ns)
            self._say_what_is_due(self._clock.now_ns())  # what the gateway said while the request waited
            raise VenueUnavailable("the gateway did not answer: TimeoutError")
        found = self.market.instrument(pair, kind)
        if found is None:
            raise UnknownInstrument(f"the fake broker has no {pair} as {kind.value}")
        return found

    def _start_quotes(self, instrument: Instrument) -> None:
        pass  # the guard's subscription table is all the fake needs

    def _stop_quotes(self, instrument: Instrument) -> None:
        pass

    def _start_depth(self, instrument: Instrument, levels: int) -> None:
        pass

    def _stop_depth(self, instrument: Instrument) -> None:
        pass

    def _history(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]:
        ticks = self.market.quotes_before(instrument.pair, end_ts_ns, count)
        return tuple(Quote(t.pair, t.bid, t.ask, t.bid_size, t.ask_size, t.at_ns, i) for i, t in enumerate(ticks))

    def _send(self, order: Order, instrument: Instrument, permit: Permit) -> None:
        if not self._gate.holds(permit):
            raise VenueUnavailable("the connection this order was checked for is gone")
        self.market.submit(order, instrument, self._clock.now_ns())

    def _cancel(self, client_order_id: str, permit: Permit) -> None:
        if not self._gate.holds(permit):
            raise VenueUnavailable("the connection this cancel was checked for is gone")
        if not self.market.holds(client_order_id):
            raise UnknownOrder(f"the fake broker holds no order {client_order_id}")
        self.market.request_cancel(client_order_id, self._clock.now_ns())

    def _open_orders(self) -> tuple[OpenOrder, ...]:
        self._settle()
        out = []
        for booked in self.market.working():
            order = booked.order
            state = OrderState.PARTIALLY_FILLED if booked.executed else OrderState.WORKING
            state = state if booked.acked else OrderState.SUBMITTED
            out.append(
                OpenOrder(
                    client_order_id=order.client_order_id,
                    broker_order_id=booked.broker_order_id,
                    pair=order.pair,
                    side=order.side,
                    quantity=order.quantity,
                    filled_quantity=booked.executed,
                    order_type=order.order_type,
                    limit_price=order.limit_price,
                    tif=order.tif,
                    state=state,
                )
            )
        return tuple(out)

    def _positions(self) -> tuple[VenuePosition, ...]:
        self._settle()
        return tuple(VenuePosition(pair, kind, qty) for (pair, kind), qty in sorted(self.market.positions().items()))

    def _executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        self._settle()
        now = self._clock.now_ns()
        out = []
        for seq, executed in enumerate(e for e in self.market.executions() if e.at_ns >= ts_ns):
            report = self.market.commission_of(executed.exec_id)
            out.append(
                Fill(
                    fill_id=executed.exec_id,
                    client_order_id=executed.client_order_id,
                    pair=executed.pair,
                    side=executed.side,
                    quantity=executed.quantity,
                    price=executed.price,
                    ts_ns=now,
                    liquidity=executed.liquidity,
                    commission=report.commission if report else 0,
                    commission_currency=report.currency if report else None,
                    venue_ts_ns=executed.at_ns,
                    seq=seq,
                )
            )
        return tuple(out)

    def _server_time_ns(self) -> int:
        return self._clock.now_ns() + self.market.config.server_clock_offset_ns

    def _cash(self) -> tuple[VenueCash, ...]:
        self._settle()
        return tuple(VenueCash(currency, amount) for currency, amount in sorted(self.market.cash().items()))

    def _settle(self) -> None:
        """A query sees the broker as it is now; what became due is still delivered by the next `poll`."""
        if self._link and self._state in (ConnectionState.RECONCILING, ConnectionState.READY):
            self._pump(0)


def build(
    clock: Clock,
    instruments: Iterable[Instrument],
    config: FakeConfig | None = None,
    *,
    seed: int = 0,
    reject: Callable[[Order], str | None] | None = None,
    gate: PaperGate | None = None,
    backoff: Backoff | None = None,
    auto_advance: bool = False,
) -> FakeBroker:
    """A fake broker and its market in one call. The market is at `broker.market` for scripting."""
    market = FakeMarket(instruments, config, seed=seed, reject=reject)
    return FakeBroker(clock, market, gate=gate, backoff=backoff, auto_advance=auto_advance)


def _is_a_venue(broker: FakeBroker) -> Venue:
    return broker  # checked by mypy: the fake broker satisfies the port
