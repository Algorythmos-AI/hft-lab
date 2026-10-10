"""The order manager: the only code that hands an order to a venue, or asks for one back (ADR 0016).

    manager.propose(order)      the risk gate rules; an allowed order is journalled, durably, and then sent
    manager.withdraw(order_id)  ask the venue to cancel a working order of ours
    manager.handle(event)       every venue event: the gate sees it first, then the state machine and the book
    manager.reconcile()         one step of making a connection safe to trade on; True once it is

One path leads to the venue, and the gate is on it: `propose` asks the gate itself, and there is no other
method that sends. The gate is the `RiskGate` class, not anything shaped like it.

The write-ahead rule: an order is on the disk in the outbox before `venue.place` is called. After a kill at any
point a new manager on the same outbox knows every order that could be at the venue. It does not know which
are, so every order that was in flight starts as UNKNOWN and the manager starts blind.

While blind nothing is proposed to the venue. `reconcile` ends it, on a first connection, after a restart and
after every reconnect alike, and it is a step to be called until it returns True, with `poll` and `handle` in
between:

  1. every working order the venue lists is asked to be cancelled, and nothing more happens until it lists none;
  2. the venue's executions since the last known time are booked, each once;
  3. every order still in flight is settled by that evidence, under its client order id: filled if its fills
     add up, cancelled if some did or the venue ever held it, expired if the venue shows no trace of it;
  4. the venue's positions must equal the book's; while they do not, the connection is not marked;
  5. only then `mark_reconciled()`.

An order whose fate cannot be established is never sent again. It is settled as dead by step 3, and if the
venue later speaks of it after all, that is a fault: it is recorded, entries stop, and a fill is booked all the
same, because the book has to say what the venue says.

Pessimistic about: the send (unknown until the venue confirms), reports (any order, any repeat), the journal
(an order that could not be written is not sent, and entries stop), the venue's clock (executions are asked
for from well before the earliest order in doubt). Not covered: an outbox that is lost or replaced. The
position check of step 4 is what would notice.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from hftlab.book import Book, BookError, Difference, differences
from hftlab.core import Clock, Fill, Instrument, Order, OrderEvent, OrderState, is_ours
from hftlab.exec.outbox import Outbox, OutboxError, commission_from, fill_fields, fill_from, order_from, text, whole
from hftlab.exec.states import IN_FLIGHT, TERMINAL, Fault, Stage, Tracked
from hftlab.journal import JournalError
from hftlab.risk import RiskGate
from hftlab.safety.lock import PaperOnlyError
from hftlab.venues.port import (
    CommissionEvent,
    ConnectionEvent,
    ConnectionState,
    NotReady,
    OpenOrder,
    OrderRefused,
    UnknownInstrument,
    Venue,
    VenueError,
    VenueUnavailable,
)

MINUTE: Final = 60 * 1_000_000_000
_LIVE: Final = (Stage.SENT, Stage.WORKING, Stage.PARTIALLY_FILLED)
_UP: Final = (ConnectionState.RECONCILING, ConnectionState.READY)
_SETTLED: Final = {
    Stage.FILLED: OrderState.FILLED,
    Stage.CANCELLED: OrderState.CANCELLED,
    Stage.EXPIRED: OrderState.EXPIRED,
    Stage.REJECTED: OrderState.REJECTED,
}


@unique
class Held(Enum):
    """Why the manager took an order no further, when it was not the risk gate that stopped it."""

    NOT_RECONCILED = "NOT_RECONCILED"  # the connection is down, new, or not yet made safe
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    ALREADY_KNOWN = "ALREADY_KNOWN"  # this very order was proposed before; it is not sent twice
    ID_REUSED = "ID_REUSED"  # another order was proposed under this id
    NOT_JOURNALLED = "NOT_JOURNALLED"  # the outbox could not be written, so nothing was sent
    REFUSED_BY_VENUE = "REFUSED_BY_VENUE"  # refused before it was sent; it is not at the venue
    SEND_UNCERTAIN = "SEND_UNCERTAIN"  # the send failed part way; it may be at the venue


@unique
class Cause(Enum):
    """Why an order moved from one stage to the next."""

    JOURNALLED = "JOURNALLED"
    PLACED = "PLACED"
    REFUSED_BY_VENUE = "REFUSED_BY_VENUE"
    SEND_FAILED = "SEND_FAILED"
    REPORT = "REPORT"  # an order event from the venue
    FILL = "FILL"
    CONNECTION_LOST = "CONNECTION_LOST"
    RESTART = "RESTART"
    OPEN_AT_VENUE = "OPEN_AT_VENUE"
    FILLS_AT_VENUE = "FILLS_AT_VENUE"
    NOT_OPEN_AT_VENUE = "NOT_OPEN_AT_VENUE"
    NO_TRACE = "NO_TRACE"


@dataclass(frozen=True, slots=True)
class Proposal:
    """What became of a proposed order. `code` is the gate's code, or the manager's own (`Held`)."""

    client_order_id: str
    sent: bool  # this call handed it to the venue
    code: str
    stage: Stage | None  # where the order stands; None when it never became one


def _terms(order: Order) -> tuple[object, ...]:
    """What an order asks for. Two orders with the same terms under one id are one decision, proposed twice."""
    return (order.sleeve, order.pair, order.side, order.quantity, order.order_type, order.tif, order.limit_price)


class OrderManager:
    def __init__(
        self,
        venue: Venue,
        gate: RiskGate,
        outbox: Outbox,
        *,
        clock: Clock,
        instruments: Iterable[Instrument],
        skew_ns: int = 2 * MINUTE,
    ) -> None:
        if not isinstance(gate, RiskGate):
            raise TypeError("the order manager is built on the risk gate itself; there is no stand-in for it")
        self._venue, self._gate, self._outbox, self._clock, self._skew_ns = venue, gate, outbox, clock, skew_ns
        self._instruments = {instrument.pair: instrument for instrument in instruments}
        self._book = Book(self._instruments.values())
        self._orders: dict[str, Tracked] = {}
        self._faults: list[tuple[Fault, str | None]] = []
        self._blind = True  # until `reconcile` has said otherwise, on every start
        self._cancelling: set[str] = set()
        self._flagged: set[tuple[Fault, str]] = set()
        self._charged: dict[str, tuple[int, str]] = {}  # the commission journalled for each fill
        self._latest_fill_ns = 0
        self._differing: tuple[Difference, ...] = ()
        self._replay(outbox.open())

    # ---- reading ------------------------------------------------------------------------------------------------

    @property
    def book(self) -> Book:
        """The broker book: fed by the venue's fills and commissions, and by nothing else."""
        return self._book

    @property
    def blind(self) -> bool:
        """True from every start and every lost connection until `reconcile` has returned True."""
        return self._blind

    @property
    def faults(self) -> tuple[tuple[Fault, str | None], ...]:
        """Every fault since this manager was built, with the order it concerns."""
        return tuple(self._faults)

    def tracked(self, client_order_id: str) -> Tracked | None:
        """What is established about one order. To be read, not changed."""
        return self._orders.get(client_order_id)

    def in_flight(self) -> tuple[str, ...]:
        """The ids of the orders that are neither finished nor known never to have been sent."""
        return tuple(order_id for order_id, tracked in self._orders.items() if tracked.stage in IN_FLIGHT)

    # ---- the one way out ----------------------------------------------------------------------------------------

    def propose(self, order: Order) -> Proposal:
        """Ask the gate; journal an allowed order durably; only then hand it to the venue."""
        now = self._clock.now_ns()
        order_id = order.client_order_id
        known = self._orders.get(order_id)
        if known is not None:
            if _terms(known.order) == _terms(order):
                return Proposal(order_id, False, Held.ALREADY_KNOWN.value, known.stage)
            self._fault(Fault.ID_REUSED, order_id)
            return Proposal(order_id, False, Held.ID_REUSED.value, known.stage)
        instrument = self._instruments.get(order.pair)
        if self._blind or self._venue.state is not ConnectionState.READY:
            return self._refuse(order, Held.NOT_RECONCILED.value, now)
        if instrument is None:
            return self._refuse(order, Held.UNKNOWN_INSTRUMENT.value, now)
        verdict = self._gate.check(order)
        if not verdict.allowed:
            return self._refuse(order, verdict.code, now)

        tracked = Tracked(order)
        try:
            self._outbox.intend(order, code=verdict.code, outcome=verdict.outcome.value, at_ns=now)
        except (OSError, JournalError):
            # Not on the disk, so not sent. The gate hears that the order came to nothing, and entries stop.
            self._faults.append((Fault.JOURNAL_FAILED, order_id))
            self._gate.observe(OrderEvent(order_id, OrderState.REJECTED, now, 0))
            self._gate.halt()
            return Proposal(order_id, False, Held.NOT_JOURNALLED.value, None)
        tracked.move(Stage.JOURNALLED)
        self._orders[order_id] = tracked
        try:
            self._venue.place(order, instrument)
        except (NotReady, OrderRefused, UnknownInstrument, PaperOnlyError) as refusal:
            # Refused before anything was sent: every one of these is raised ahead of the venue's own send.
            self._move(tracked, Stage.REJECTED, Cause.REFUSED_BY_VENUE)
            self._tell_gate(tracked)
            self._outbox.commit()
            if isinstance(refusal, PaperOnlyError):
                raise
            return Proposal(order_id, False, Held.REFUSED_BY_VENUE.value, tracked.stage)
        except Exception as failure:
            # It may be at the venue and it may not. Nothing is assumed, and nothing more is sent blind.
            self._move(tracked, Stage.UNKNOWN, Cause.SEND_FAILED)
            self._blind = True
            expected = isinstance(failure, VenueUnavailable)
            if not expected:
                self._fault(Fault.SEND_FAILED, order_id)
            self._outbox.commit()
            if not expected:
                raise
            return Proposal(order_id, False, Held.SEND_UNCERTAIN.value, tracked.stage)
        self._move(tracked, Stage.SENT, Cause.PLACED)
        self._outbox.commit()
        return Proposal(order_id, True, verdict.code, tracked.stage)

    def withdraw(self, client_order_id: str) -> bool:
        """Ask the venue to cancel a working order of ours. True when the request was sent."""
        tracked = self._orders.get(client_order_id)
        if tracked is None or tracked.stage not in (*_LIVE, Stage.UNKNOWN):
            return False
        sent = self._recall(client_order_id)
        self._outbox.commit()
        return sent

    def _recall(self, client_order_id: str) -> bool:
        try:
            self._venue.cancel(client_order_id)
        except VenueError:
            return False  # not sent, or not known to have been; the next reconciliation asks again
        self._gate.count_cancel(client_order_id)
        tracked = self._orders.get(client_order_id)
        if tracked is not None:
            tracked.cancel_sent = True
        self._outbox.record("cancel", at_ns=self._clock.now_ns(), client_order_id=client_order_id)
        return True

    def _refuse(self, order: Order, code: str, now: int) -> Proposal:
        self._outbox.record(
            "refused", at_ns=now, code=code, client_order_id=order.client_order_id, sleeve=order.sleeve,
            pair=order.pair, side=order.side.value, quantity=order.quantity,
        )  # fmt: skip
        return Proposal(order.client_order_id, False, code, None)

    # ---- what the venue says ------------------------------------------------------------------------------------

    def handle(self, event: object) -> None:
        """Take one event. The gate is shown it as it came, before the manager draws any conclusion from it.

        If the gate fails on it (its journal cannot be written, say), the event is still taken here before the
        failure is passed on: a fill is booked whatever else goes wrong.
        """
        try:
            self._gate.observe(event)
        finally:
            if isinstance(event, Fill):
                self._on_fill(event)
            elif isinstance(event, CommissionEvent):
                self._book.charge(event)
                self._charge(event.fill_id, event.commission, event.currency)
            elif isinstance(event, OrderEvent):
                self._on_report(event)
            elif isinstance(event, ConnectionEvent) and event.state is not ConnectionState.READY:
                self._lose_sight(event.state)
            self._outbox.commit()

    def _charge(self, fill_id: str, commission: int, currency: str) -> None:
        """Keep a fill's commission in the outbox, so a restart books the fill with what it cost."""
        if self._charged.get(fill_id) != (commission, currency):
            self._charged[fill_id] = (commission, currency)
            self._outbox.record("commission", fill_id=fill_id, commission=commission, currency=currency)

    def _on_fill(self, fill: Fill) -> None:
        try:
            new = self._book.apply(fill)
        except BookError:
            self._fault(Fault.UNBOOKABLE_FILL, fill.client_order_id, fill_id=fill.fill_id)
            return
        if fill.commission_currency is not None and fill.fill_id not in self._charged:
            self._charge(fill.fill_id, fill.commission, fill.commission_currency)  # a listed fill says what it cost
        if not new:
            return  # booked before: a repeat moves no position
        self._outbox.record("fill", **fill_fields(fill))
        self._latest_fill_ns = max(self._latest_fill_ns, fill.venue_ts_ns or fill.ts_ns)
        tracked = self._orders.get(fill.client_order_id)
        if tracked is None:
            self._fault(Fault.FILL_WITHOUT_ORDER, fill.client_order_id, fill_id=fill.fill_id)
            return
        before = tracked.stage
        self._after(tracked, before, Cause.FILL, tracked.take_fill(fill))

    def _on_report(self, event: OrderEvent) -> None:
        tracked = self._orders.get(event.client_order_id)
        if tracked is not None:
            before = tracked.stage
            self._after(tracked, before, Cause.REPORT, tracked.take_event(event))
        elif is_ours(event.client_order_id):
            self._fault_once(Fault.REPORT_WITHOUT_ORDER, event.client_order_id)

    def _lose_sight(self, state: ConnectionState) -> None:
        """The connection is gone or new. Nothing is known about a live order until the venue has been asked."""
        self._blind = True
        for tracked in self._orders.values():
            if tracked.stage in _LIVE:
                self._move(tracked, Stage.UNKNOWN, Cause.CONNECTION_LOST)
        if state is ConnectionState.RECONCILING:
            self._cancelling.clear()  # a cancel asked for on the old connection may never have arrived

    # ---- making a connection safe to trade on -------------------------------------------------------------------

    def reconcile(self) -> bool:
        """One step towards a connection that may carry orders. True once it may; call again until then."""
        state = self._venue.state
        if state not in _UP:
            return False
        if state is ConnectionState.READY and not self._blind:
            return True
        try:
            working = self._venue.open_orders()
            for found in working:
                self._take_back(found)
            if working:
                return False  # nothing is settled while anything can still fill
            executed = self._venue.executions_since(self._since())
            reported = self._venue.positions()
        except VenueError:
            return False
        finally:
            self._outbox.commit()
        for fill in executed:
            self.handle(fill)
        for tracked in list(self._orders.values()):
            if tracked.stage in IN_FLIGHT:
                self._settle(tracked)
        differing = differences(self._book, reported)
        if differing:
            if differing != self._differing:
                self._differing = differing
                listed = [[d.pair, d.kind.value, d.ours, d.venue] for d in differing]
                self._outbox.record("positions_differ", at_ns=self._clock.now_ns(), differences=listed)
            self._outbox.commit()
            return False
        if state is ConnectionState.RECONCILING:
            self._venue.mark_reconciled()
        self._blind, self._differing = False, ()
        self._cancelling.clear()
        self._outbox.record("reconciled", at_ns=self._clock.now_ns())
        self._outbox.commit()
        return True

    def _take_back(self, found: OpenOrder) -> None:
        """Ask for a working order to be cancelled, whatever this manager believed about it."""
        order_id = found.client_order_id
        if order_id is None:
            self._fault_once(Fault.FOREIGN_ORDER, None, broker_order_id=found.broker_order_id)
            return  # not ours to cancel through this port; the connection stays unmarked while it works
        tracked = self._orders.get(order_id)
        if tracked is None:
            self._fault_once(Fault.UNTRACKED_ORDER, order_id)
        elif tracked.stage in TERMINAL:
            self._fault_once(Fault.OPEN_AFTER_CLOSE, order_id)
        else:
            tracked.acked = True
            tracked.reported = max(tracked.reported, min(found.filled_quantity, tracked.order.quantity))
            if tracked.stage is Stage.UNKNOWN:
                seen = Stage.PARTIALLY_FILLED if found.filled_quantity else Stage.WORKING
                self._move(tracked, seen, Cause.OPEN_AT_VENUE)
        if order_id not in self._cancelling and self._recall(order_id):
            self._cancelling.add(order_id)

    def _settle(self, tracked: Tracked) -> None:
        """The venue lists no working order and its executions are booked: what an order in flight came to."""
        tracked.reported = max(tracked.reported, tracked.booked)  # a late honest report of these fills is no fault
        if tracked.booked >= tracked.order.quantity:
            self._move(tracked, Stage.FILLED, Cause.FILLS_AT_VENUE)
        elif tracked.booked or tracked.acked or tracked.cancel_sent:
            self._move(tracked, Stage.CANCELLED, Cause.NOT_OPEN_AT_VENUE)
        else:
            self._move(tracked, Stage.EXPIRED, Cause.NO_TRACE)
        self._tell_gate(tracked)

    def _since(self) -> int:
        """The venue time to ask for executions from: before the earliest order in doubt, with room for its clock."""
        in_doubt = [t.order.created_ts_ns for t in self._orders.values() if t.stage in IN_FLIGHT or t.owed]
        known = [*in_doubt, *([self._latest_fill_ns] if self._latest_fill_ns else [])]
        return max(0, min(known) - self._skew_ns) if known else 0

    # ---- the record ---------------------------------------------------------------------------------------------

    def _move(self, tracked: Tracked, to: Stage, cause: Cause) -> None:
        before = tracked.stage
        if not tracked.move(to):
            self._fault(Fault.IMPOSSIBLE_MOVE, tracked.order.client_order_id, before=before.value, to=to.value)
        elif tracked.stage is not before:
            self._staged(tracked, before, cause)

    def _after(self, tracked: Tracked, before: Stage, cause: Cause, faults: tuple[Fault, ...]) -> None:
        if tracked.stage is not before:
            self._staged(tracked, before, cause)
        for fault in faults:
            self._fault(fault, tracked.order.client_order_id, stage=tracked.stage.value)

    def _staged(self, tracked: Tracked, before: Stage, cause: Cause) -> None:
        self._outbox.record(
            "stage", at_ns=self._clock.now_ns(), client_order_id=tracked.order.client_order_id, before=before.value,
            to=tracked.stage.value, cause=cause.value, booked=tracked.booked, reported=tracked.reported,
            acked=int(tracked.acked),
        )  # fmt: skip

    def _fault(self, fault: Fault, client_order_id: str | None, **detail: str) -> None:
        """Stop entries, and record what cannot happen. Nothing is dropped and nothing is repaired."""
        self._faults.append((fault, client_order_id))
        self._gate.halt()  # first: the stop must not depend on the record being writable
        self._outbox.record(
            "fault", at_ns=self._clock.now_ns(), fault=fault.value, client_order_id=client_order_id, **detail
        )

    def _fault_once(self, fault: Fault, client_order_id: str | None, **detail: str) -> None:
        """A fault that would otherwise be recorded again at every report or every step about the same order."""
        key = (fault, client_order_id or detail.get("broker_order_id", ""))
        if key not in self._flagged:
            self._flagged.add(key)
            self._fault(fault, client_order_id, **detail)

    def _tell_gate(self, tracked: Tracked) -> None:
        """An order ended by the manager's own conclusion: the gate is told, so it stops holding the size."""
        state = _SETTLED.get(tracked.stage)
        if state is not None:
            now, order_id = self._clock.now_ns(), tracked.order.client_order_id
            self._gate.observe(OrderEvent(order_id, state, now, 0, filled_quantity=tracked.booked))

    def _replay(self, entries: list[dict[str, object]]) -> None:
        """Rebuild from the outbox: every order, every booked fill, and the stage each order was last seen in."""
        for entry in entries:
            kind = entry.get("type")
            if kind == "intent":
                order = order_from(entry)
                self._orders[order.client_order_id] = Tracked(order, Stage.JOURNALLED)
            elif kind in ("stage", "cancel"):
                tracked = self._orders.get(text(entry, "client_order_id"))
                if tracked is None:
                    raise OutboxError(f"entry {entry.get('i')} is about an order the outbox never recorded")
                if kind == "cancel":
                    tracked.cancel_sent = True
                    continue
                if text(entry, "to") not in Stage.__members__:
                    raise OutboxError(f"entry {entry.get('i')} names a stage that does not exist")
                tracked.stage, tracked.reported = Stage(text(entry, "to")), whole(entry, "reported")
                tracked.acked = bool(whole(entry, "acked"))
            elif kind == "fill":
                fill = fill_from(entry)
                try:
                    booked = self._book.apply(fill)
                except BookError as exc:
                    raise OutboxError(f"entry {entry.get('i')} cannot be booked again: {exc}") from exc
                self._gate.observe(fill)
                self._latest_fill_ns = max(self._latest_fill_ns, fill.venue_ts_ns or fill.ts_ns)
                owner = self._orders.get(fill.client_order_id)
                if booked and owner is not None:
                    owner.fills.add(fill.fill_id)
                    owner.booked += fill.quantity
            elif kind == "commission":
                charge = commission_from(entry)
                self._book.charge(charge)
                self._gate.observe(charge)
                self._charged[charge.fill_id] = (charge.commission, charge.currency)
        lost = [tracked for tracked in self._orders.values() if tracked.stage in IN_FLIGHT]
        for tracked in lost:
            self._move(tracked, Stage.UNKNOWN, Cause.RESTART)
        self._outbox.record("start", at_ns=self._clock.now_ns(), orders=len(self._orders), in_flight=len(lost))
        self._outbox.commit()

    def close(self) -> None:
        self._outbox.close()
