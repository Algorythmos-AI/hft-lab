"""The shadow runner: sleeves decide on real quotes, and their orders go to the shadow venue and nowhere else.

    quotes (a recording, or a live venue seen through an order-free view)
        -> Shadow.handle(event)
             the shadow venue is fed the quote and reports what happened to earlier orders
             fills and commissions are booked; the journal gets a line for each
             each sleeve is shown its own news, then the event, and says what it wants
             what it wants becomes an order on the shadow venue; the journal gets the decision and the order

No broker order can be sent from here, and not by promise. `Shadow` builds its own `SimVenue`, the venue with
no broker and no network, and that is the only object in this package anything is ever placed on; there is no
parameter through which another venue could be handed in. The live quote source is a `QuoteSource`, which has
no order methods at all. `tests/shadow/test_structure.py` holds the package to both.

Time is the events' own. The runner keeps a simulated clock and moves it to each event's receive time, live
and from a recording alike, so a session over the same events writes the same journal, byte for byte.
A session starts flat: the journal continues across restarts, positions do not.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from typing import Any, Final

from hftlab.book import Book, SleeveView
from hftlab.calendar import Calendar
from hftlab.core import (
    Clock,
    Fill,
    Instrument,
    InstrumentKind,
    Order,
    OrderEvent,
    OrderType,
    Quote,
    SimClock,
    client_order_id,
    sleeve_of,
)
from hftlab.record.format import Heartbeat, RecordedEvent, describe
from hftlab.replay.reader import at_ns
from hftlab.shadow.journal import Journal
from hftlab.strategy import Action, Cancel, Event, Intent, Placed, Refused, Sleeve
from hftlab.venues.port import CommissionEvent, ConnectionEvent, ConnectionState, VenueError, VenueNotice
from hftlab.venues.sim import CostModel, SimVenue
from hftlab.venues.view import QuoteSource

MS: Final = 1_000_000
ANSWERS_PER_EVENT: Final = 64
_UP: Final = (ConnectionState.RECONCILING, ConnectionState.READY)


class ShadowError(RuntimeError):
    """The runner was set up wrongly, or a sleeve does not behave like one."""


class Shadow:
    def __init__(
        self,
        instruments: Iterable[Instrument],
        model: CostModel,
        sleeves: Sequence[Sleeve[Any]],
        journal: Journal,
        *,
        calendar: Calendar | None = None,
        seed: int = 0,
        source: str = "unnamed",
    ) -> None:
        self._instruments = {instrument.pair: instrument for instrument in instruments}
        self._sleeves = list(sleeves)
        names = [sleeve.name for sleeve in self._sleeves]
        if len(set(names)) != len(names):
            raise ShadowError(f"two sleeves share a name: {sorted(names)}")
        self._clock = SimClock()
        self._venue = SimVenue(self._clock, self._instruments.values(), model, calendar=calendar, seed=seed)
        self._venue.connect()
        self._venue.mark_reconciled()
        self._venue.poll()
        self._book = Book(self._instruments.values())
        self._states: dict[str, Any] = {sleeve.name: sleeve.initial() for sleeve in self._sleeves}
        self._views: dict[str, SleeveView] = {name: self._book.view(name) for name in names}
        self._last: dict[str, Quote] = {}
        self._counts = {name: {"decisions": 0, "orders": 0, "refused": 0, "fills": 0} for name in names}
        self._journal = journal
        found = journal.open()
        self._n = found.last_event
        self.session = found.sessions + 1
        self.events = 0
        journal.write(
            "start",
            session=self.session,
            source=source,
            seed=seed,
            n=self._n,
            sleeves=names,
            instruments=[describe(instrument) for instrument in self._instruments.values()],
            cost_model=model.as_dict(),
        )
        journal.flush()

    # ---- one event ----------------------------------------------------------------------------------------------

    def handle(self, event: RecordedEvent) -> None:
        """Take the next event of the stream. Its receive time is the time of everything decided on it."""
        now = self._clock.advance_to(max(self._clock.now_ns(), at_ns(event)))
        self._n += 1
        self.events += 1
        if isinstance(event, Quote):
            if event.pair in self._instruments:
                self._venue.feed(event if event.recv_ts_ns == now else replace(event, recv_ts_ns=now))
                self._last[event.pair] = event
        elif isinstance(event, Heartbeat):
            self._venue.quiet(event.pair, now)
        news = self._settle(now)
        for sleeve in self._sleeves:
            for item in (*news.get(sleeve.name, ()), event):
                self._show(sleeve, item, now)
        self._journal.flush()

    def _settle(self, now: int) -> dict[str, list[Event]]:
        """Collect what the shadow venue has to report, book it, journal it, and sort it by the sleeve it concerns."""
        news: dict[str, list[Event]] = {}
        booked = False
        for item in self._venue.poll():
            if isinstance(item, Fill):
                owner = sleeve_of(item.client_order_id) or ""
                booked = self._book.apply(item) or booked
                self._count(owner, "fills")
                self._journal.write(
                    "fill", n=self._n, at_ns=now, sleeve=owner, fill_id=item.fill_id,
                    client_order_id=item.client_order_id, pair=item.pair, side=item.side.value,
                    quantity=item.quantity, price=item.price, liquidity=item.liquidity.value,
                    venue_ts_ns=item.venue_ts_ns,
                )  # fmt: skip
                news.setdefault(owner, []).append(item)
            elif isinstance(item, CommissionEvent):
                self._book.charge(item)
                booked = True
                self._journal.write(
                    "commission", n=self._n, at_ns=now, fill_id=item.fill_id, commission=item.commission,
                    currency=item.currency,
                )  # fmt: skip
            elif isinstance(item, OrderEvent):
                self._journal.write(
                    "order_event", n=self._n, at_ns=now, client_order_id=item.client_order_id,
                    state=item.state.value, filled_quantity=item.filled_quantity, reason=item.reason,
                )  # fmt: skip
                news.setdefault(sleeve_of(item.client_order_id) or "", []).append(item)
            elif isinstance(item, VenueNotice):
                self._journal.write("notice", n=self._n, at_ns=now, code=item.code, message=item.message)
        if booked:
            self._views = {name: self._book.view(name) for name in self._views}
        return news

    def _show(self, sleeve: Sleeve[Any], item: Event, now: int) -> None:
        """Show a sleeve one event, do what it asks, and show it how that went, until it has nothing more to ask."""
        name = sleeve.name
        waiting: deque[Event] = deque([item])
        shown = 0
        while waiting:
            shown += 1
            if shown > ANSWERS_PER_EVENT:
                raise ShadowError(f"sleeve {name} keeps answering its own acknowledgements; stopped at {shown}")
            self._states[name], actions = sleeve.decide(self._states[name], waiting.popleft(), now, self._views[name])
            for action in actions:
                answer = self._act(name, action, now)
                if answer is not None:
                    waiting.append(answer)

    def _act(self, name: str, action: Action, now: int) -> Placed | Refused | None:
        if isinstance(action, Cancel):
            if sleeve_of(action.client_order_id) != name:
                return self._refuse(name, action, now, "not this sleeve's order")
            try:
                self._venue.cancel(action.client_order_id)
            except VenueError as exc:
                return self._refuse(name, action, now, str(exc))
            self._journal.write("cancel", n=self._n, at_ns=now, sleeve=name, client_order_id=action.client_order_id)
            return None
        if not isinstance(action, Intent):
            raise ShadowError(f"sleeve {name} returned {action!r}, which is neither an intent nor a cancel")
        self._count(name, "decisions")
        self._journal.write(
            "decision", n=self._n, at_ns=now, sleeve=name, pair=action.pair, side=action.side.value,
            quantity=action.quantity, leg=action.leg, attempt=action.attempt, tif=action.tif.value,
            limit_price=action.limit_price,
        )  # fmt: skip
        instrument = self._instruments.get(action.pair)
        if instrument is None:
            return self._refuse(name, action, now, f"{action.pair} is not one of this session's instruments")
        order_id = client_order_id(
            sleeve=name, pair=action.pair, side=action.side, decision_seq=self._n, leg=action.leg,
            attempt=action.attempt,
        )  # fmt: skip
        kind = OrderType.MARKET if action.limit_price is None else OrderType.LIMIT
        try:
            order = Order(
                order_id, name, action.pair, action.side, action.quantity, kind, action.tif, now, action.limit_price
            )
            self._venue.place(order, instrument)
        except (VenueError, ValueError) as exc:
            return self._refuse(name, action, now, str(exc))
        self._count(name, "orders")
        self._journal.write(
            "order", n=self._n, at_ns=now, sleeve=name, client_order_id=order_id, pair=order.pair,
            side=order.side.value, quantity=order.quantity, order_type=kind.value, tif=order.tif.value,
            limit_price=order.limit_price,
        )  # fmt: skip
        return Placed(action, order_id)

    def _refuse(self, name: str, action: Action, now: int, reason: str) -> Refused:
        self._count(name, "refused")
        self._journal.write("refused", n=self._n, at_ns=now, sleeve=name, reason=reason)
        return Refused(action, reason)

    def _count(self, name: str, what: str) -> None:
        if name in self._counts:
            self._counts[name][what] += 1

    # ---- the result ---------------------------------------------------------------------------------------------

    def summary(self) -> dict[str, object]:
        """Each sleeve's result per pair, in exact integers. Money is in millionths of the pair's quote currency;

        an open position is valued at the last quote seen, a long at the bid and a short at the ask.
        """
        sleeves: dict[str, object] = {}
        for name, counts in self._counts.items():
            pairs: dict[str, object] = {}
            for pair, instrument in self._instruments.items():
                last = self._last.get(pair)
                result = self._book.pnl(name, pair, *((last.bid, last.ask) if last else (1, 1)))
                pairs[pair] = {
                    "currency": instrument.quote,
                    "quantity": self._book.position(name, pair).quantity,
                    "realised": result.realised,
                    "unrealised": result.unrealised,
                    "price_scale": instrument.price_scale,
                    "fees_micro": result.fees_micro,
                    "net_micro": result.net_micro,
                }
            sleeves[name] = {**counts, "pairs": pairs}
        return {"session": self.session, "events": self.events, "last_event": self._n, "sleeves": sleeves}

    def close(self) -> dict[str, object]:
        """Write the result as the session's last entry and close the journal."""
        result = self.summary()
        self._journal.write("stop", n=self._n, at_ns=self._clock.now_ns(), summary=result)
        self._journal.close()
        return result

    @property
    def book(self) -> Book:
        return self._book


# ---- where the events come from -------------------------------------------------------------------------------


def open_live(source: QuoteSource, pairs: Sequence[tuple[str, InstrumentKind]]) -> list[Instrument]:
    """Connect the quote source, look the pairs up and subscribe. The source re-subscribes by itself after a drop."""
    source.connect()
    instruments = [source.instrument(pair, kind) for pair, kind in pairs]
    for instrument in instruments:
        source.subscribe_quotes(instrument)
    return instruments


def live_events(
    source: QuoteSource,
    clock: Clock,
    pairs: Sequence[str],
    should_stop: Callable[[], bool],
    *,
    poll_ns: int = 50 * MS,
    heartbeat_ns: int = 1_000 * MS,
) -> Iterator[RecordedEvent]:
    """A live source as the same stream a recording holds: quotes, connection changes, and a heartbeat for each

    pair whose book was still while the venue answered. `clock` is the injected clock the source stamps with.
    """
    last_quote: dict[str, int] = {}
    beat_at = clock.now_ns()
    while not should_stop():
        for event in source.poll(poll_ns):
            if isinstance(event, Quote):
                last_quote[event.pair] = event.recv_ts_ns
                yield event
            elif isinstance(event, ConnectionEvent):
                yield event
        now = clock.now_ns()
        if now - beat_at < heartbeat_ns:
            continue
        quiet = [pair for pair in pairs if last_quote.get(pair, -1) < beat_at]
        beat_at = now
        if quiet and source.state in _UP:
            try:
                answered = source.server_time_ns()
            except VenueError:
                continue  # no answer, no heartbeat: the quotes go stale, and the shadow venue stops filling
            for pair in quiet:
                yield Heartbeat(pair, now, answered, 0)


def run(shadow: Shadow, stream: Iterable[RecordedEvent]) -> dict[str, object]:
    """Feed a whole stream through and close the session. The journal is closed even when the stream fails."""
    try:
        for event in stream:
            shadow.handle(event)
    finally:
        result = shadow.close()
    return result
