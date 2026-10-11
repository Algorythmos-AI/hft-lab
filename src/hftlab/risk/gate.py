"""The independent risk gate: every order passes it before it can be sent (ADR 0015).

The gate trusts nothing a sleeve says about itself. It keeps its own book, built from the fills it is shown,
and its verdict is a function of the events it has been shown, the injected clock, the calendar, its limits
and what its journal held when it was built. So a recorded day replays to the same verdicts.

    gate.observe(event)     a quote, a fill, a commission, an order event, a change of connection, a break
    gate.quiet(pair, at)    the venue answered and the pair's book had not changed: its quote is not stale
    gate.check(order)       the verdict; an order that is allowed is counted and its size is held at once
    gate.flatten_orders()   while FLATTEN is set: the orders that close what the book holds

A verdict is ALLOW, REFUSE, or EXITS_ONLY: entries are stopped and an order may go only if it cannot leave the
sleeve holding more than it holds, or holding the other way (`hftlab.risk.verdict`).

What it is pessimistic about:

  * An order it allowed can still fill, in full, until it is shown that it cannot. Its size is held against
    the position limit from the verdict on, and released only by a terminal order event, and then only down
    to what that event says was filled and the book has not seen yet. Time never releases it.
  * A quote ages by the gate's own clock, and that clock never goes back: after a clock that jumped back, new
    quotes look old until time has caught up, and nothing is sent on them.
  * A latch, once tripped, stays tripped: across a restart, across the end of the day, until `reset` is called.
    The trip and the reset are in the journal, and so are the marks a latch is measured from (the day's
    opening result and the highest result reached), so a restart does not forgive a loss.
  * Rate limits and the order-to-trade ratio hold for exits too. A flood of exits is still a flood.
  * A restart forgets nothing that limits orders. The rate windows, the ids already ruled on and the size held
    for orders in flight are rebuilt from the order manager's outbox (`recall`), and the running count of
    rejects is in the gate's own journal, so a crash loop is not a reset.
  * An order that can only reduce the sleeve's position is measured against a wider collar of its own, never
    against none. "Can only reduce" counts every order of the sleeve still in flight as filled.
  * Money in different currencies is never added. A loss is judged per currency, and a loss in a currency
    with no latch stops entries by itself.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from hftlab.book import DESK, UNATTRIBUTED, Book, BookError, BreakEvent
from hftlab.calendar import Calendar, in_rollover_window, last_rollover, next_close
from hftlab.core import (
    CASH_SCALE,
    Clock,
    Fill,
    Instrument,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Quote,
    Side,
    TimeInForce,
    client_order_id,
    sleeve_of,
)
from hftlab.core._checks import token
from hftlab.journal import Journal, read
from hftlab.risk.config import LossLatch, RiskConfig, RiskConfigError
from hftlab.risk.verdict import Outcome, Reason, Verdict
from hftlab.venues.port import CommissionEvent, ConnectionEvent, ConnectionState

SECOND: Final = 1_000_000_000
MINUTE: Final = 60 * SECOND
MARK_EVERY_NS: Final = SECOND  # a rising high-water mark is written to the journal at most this often
FLATTEN_LEG: Final = "flatten"
_ACCEPTED: Final = (OrderState.WORKING, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED)
_LOSS: Final = {
    (True, True): Reason.DAILY_LOSS_DESK,
    (True, False): Reason.DRAWDOWN_DESK,
    (False, True): Reason.DAILY_LOSS_SLEEVE,
    (False, False): Reason.DRAWDOWN_SLEEVE,
}

Switches = Callable[[], Collection[str]]


class LatchError(ValueError):
    """A reset that cannot be carried out: no such latch is tripped, or its new starting point cannot be valued."""


@dataclass(slots=True)
class _Pending:
    """An order the gate allowed and has not been shown the end of."""

    order: Order
    booked: int = 0  # fills of it in the gate's book
    reported: int = 0  # the most any order event has said was filled
    closed: bool = False

    @property
    def held(self) -> int:
        """What this order may still add to the position, as far as the gate has been shown."""
        ceiling = self.reported if self.closed else self.order.quantity
        return max(ceiling - self.booked, 0)


@dataclass(slots=True)
class _Mark:
    """What a scope's loss in one currency is measured from."""

    day: int  # the rollover instant that opened the trading day in hand
    day_open: int  # the result when that day opened, in millionths
    peak: int  # the highest result reached
    written_ns: int = -MARK_EVERY_NS
    written: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class _Tripped:
    reason: Reason
    sleeve: str | None  # None: the whole desk
    currency: str | None


def latch_name(reason: Reason, sleeve: str | None = None, currency: str | None = None) -> str:
    """The name a latch is journalled and reset under: `DAILY_LOSS_SLEEVE/control/USD`, `REJECT_STREAK`."""
    return "/".join(part for part in (reason.value, sleeve, currency) if part)


class RiskGate:
    def __init__(
        self,
        config: RiskConfig,
        instruments: Iterable[Instrument],
        *,
        calendar: Calendar,
        clock: Clock,
        switches: Switches,
        journal: Journal,
    ) -> None:
        self._config, self._calendar, self._clock, self._switches = config, calendar, clock, switches
        self._instruments = {instrument.pair: instrument for instrument in instruments}
        missing = sorted(set(config.pairs) - set(self._instruments))
        if missing:
            raise RiskConfigError(f"limits are set for pairs the gate has no instrument for: {', '.join(missing)}")
        too_fine = sorted(pair for pair, i in self._instruments.items() if i.price_scale > CASH_SCALE)
        if too_fine:
            raise RiskConfigError(f"quoted finer than a millionth, so a loss cannot be valued: {', '.join(too_fine)}")
        self._book = Book(self._instruments.values())
        self._now = 0
        self._state = ConnectionState.DISCONNECTED
        self._broken = False
        self._quotes: dict[str, Quote] = {}
        self._fresh: dict[str, int] = {}
        self._quoted: set[str] = set()  # pairs quoted on the connection in hand; only those can be confirmed fresh
        self._pending: dict[str, _Pending] = {}
        self._admitted: dict[str, bool] = {}  # every id allowed; True once the venue's first answer was counted
        self._sent: deque[tuple[int, str]] = deque()  # (when, sleeve) of each order allowed
        self._messages: deque[tuple[int, str]] = deque()  # orders and cancels
        self._trades: deque[tuple[int, str]] = deque()  # the first fill of each order
        self._rejects = 0
        self._flattened = 0
        self._closing: dict[tuple[str, str], tuple[str, int]] = {}  # the last close proposed for a line, and when
        self._tripped: dict[str, _Tripped] = {}
        self._marks: dict[tuple[str, str], _Mark] = {}
        self._unlatched: set[str] = set()
        self._unmarked = False
        self._fees: dict[str, dict[str, int]] | None = None
        self._dirty = True
        self._journal = journal
        for entry in read(journal.path)[1]:
            self._restore(entry)
        journal.open()
        journal.write("start", decision=config.decision, latched=sorted(self._tripped))
        journal.flush()

    # ---- what the gate is shown ---------------------------------------------------------------------------------

    def observe(self, event: object) -> None:
        """Take the next event. Anything the gate has no use for is ignored; nothing here raises on a bad event."""
        now = self._tick()
        if isinstance(event, Quote):
            if event.pair in self._instruments:
                # The last quote shown is the book. It is as fresh as its own stamp says, and never fresher than
                # the gate's time: after a clock set back, new quotes carry old stamps and are judged old.
                self._quotes[event.pair] = event
                self._fresh[event.pair] = min(now, event.recv_ts_ns)
                self._quoted.add(event.pair)
                if self._dirty or self._unmarked or any(pair == event.pair for _, pair in self._book.open_lines()):
                    self._evaluate(now)
        elif isinstance(event, Fill):
            self._on_fill(event, now)
        elif isinstance(event, CommissionEvent):
            self._book.charge(event)
            self._fees, self._dirty = None, True
        elif isinstance(event, OrderEvent):
            self._on_order_event(event, now)
        elif isinstance(event, ConnectionEvent):
            self._state = event.state
            if event.state is not ConnectionState.READY:
                # A quote from before a lost or new connection says nothing about the book now. It still values
                # positions, but it is stale, and no confirmation can freshen it: only a new quote can.
                self._quoted.clear()
        elif isinstance(event, BreakEvent):
            self._broken = event.broken

    def quiet(self, pair: str, at_ns: int) -> None:
        """The venue confirmed that this pair's book has not changed: its last quote is as fresh as `at_ns`."""
        if pair in self._quoted:
            self._fresh[pair] = max(self._fresh[pair], min(self._tick(), at_ns))

    def count_cancel(self, order_id: str) -> None:
        """A cancel was sent for this order. It is a message to the venue and counts against the trade ratio."""
        self._messages.append((self._tick(), sleeve_of(order_id) or UNATTRIBUTED))

    def halt(self) -> None:
        """Something happened that cannot happen. Entries stop until an operator has looked and reset it."""
        self._trip(Reason.ORDER_FAULT, None, None, self._tick())

    def _streak(self, rejects: int, now: int) -> None:
        """Move the running count of rejects, and keep it in the journal: a restart goes on counting from it."""
        if rejects != self._rejects:
            self._rejects = rejects
            self._journal.write("streak", rejects=rejects, at_ns=now)
            self._journal.flush()

    def recall(
        self,
        *,
        allowed: Iterable[tuple[int, Order]],
        in_flight: Mapping[str, int],
        unanswered: Collection[str],
        cancels: Iterable[tuple[int, str]],
        trades: Iterable[tuple[int, str]],
    ) -> None:
        """Take back, from the order manager's outbox, what this gate knew before the process was started again.

        `allowed` is every order a gate on this outbox let through, with the time it did; `in_flight` gives,
        for those that may still fill, what has been booked of each; `unanswered` are the ones the venue has
        not answered yet; `cancels` and `trades` are (when, order id) and (when, sleeve) of every cancel sent
        and every order's first fill. Afterwards the rate windows hold what they held, an id is ruled on once
        across restarts, and an order in flight holds its size against the position limit until it is settled.
        """
        for at, order in allowed:
            order_id = order.client_order_id
            if order_id in self._admitted:
                continue
            self._admitted[order_id] = order_id not in unanswered
            self._sent.append((at, order.sleeve))
            self._messages.append((at, order.sleeve))
            if order_id in in_flight:
                self._pending[order_id] = _Pending(order, booked=min(in_flight[order_id], order.quantity))
        self._messages.extend((at, sleeve_of(order_id) or UNATTRIBUTED) for at, order_id in cancels)
        self._trades.extend(trades)
        for ages in (self._sent, self._messages, self._trades):
            kept = sorted(ages)  # each window drops from its old end, so it has to be in order of time
            ages.clear()
            ages.extend(kept)

    def _on_fill(self, fill: Fill, now: int) -> None:
        try:
            new = self._book.apply(fill)
        except BookError:
            self.halt()  # two fills under one id, or a pair the gate has no instrument for
            return
        if new or fill.commission_currency is not None:
            self._fees, self._dirty = None, True  # a repeat can be the first to carry what the fill cost
        if not new:
            return
        pending = self._pending.get(fill.client_order_id)
        if pending is None:
            return
        if fill.pair != pending.order.pair or fill.side is not pending.order.side:
            self.halt()
        if pending.booked == 0:
            self._trades.append((now, pending.order.sleeve))
        pending.booked += fill.quantity
        if pending.held == 0:
            del self._pending[fill.client_order_id]

    def _on_order_event(self, event: OrderEvent, now: int) -> None:
        if self._admitted.get(event.client_order_id) is False:  # the venue's first answer to an order of ours
            if event.state is OrderState.REJECTED:
                self._admitted[event.client_order_id] = True
                self._streak(self._rejects + 1, now)
                if self._rejects >= self._config.desk.reject_streak_breaker:
                    self._trip(Reason.REJECT_STREAK, None, None, now, rejects=self._rejects)
            elif event.state in _ACCEPTED:
                self._admitted[event.client_order_id] = True
                self._streak(0, now)
        pending = self._pending.get(event.client_order_id)
        if pending is None:
            return
        pending.reported = max(pending.reported, min(event.filled_quantity, pending.order.quantity))
        pending.closed = pending.closed or event.state.is_terminal
        if pending.closed and pending.held == 0:
            del self._pending[event.client_order_id]

    # ---- the verdict --------------------------------------------------------------------------------------------

    def check(self, order: Order) -> Verdict:
        """Rule on one order. If it is allowed, it counts against every limit from this moment."""
        now = self._tick()
        self._evaluate(now)
        refusing, stopping = self._judge(order, now)
        if refusing:
            return Verdict(Outcome.REFUSE, False, (*refusing, *stopping))
        if stopping:
            verdict = Verdict(Outcome.EXITS_ONLY, self._reduces(order), tuple(stopping))
        else:
            verdict = Verdict(Outcome.ALLOW, True)
        if verdict.allowed:
            self._pending[order.client_order_id] = _Pending(order)
            self._admitted[order.client_order_id] = False
            self._sent.append((now, order.sleeve))
            self._messages.append((now, order.sleeve))
        return verdict

    def _judge(self, order: Order, now: int) -> tuple[list[Reason], list[Reason]]:
        """(the codes that refuse this order outright, the codes that stop entries), each in a fixed order."""
        refusing: list[Reason] = []
        stopping: list[Reason] = []
        if order.client_order_id in self._admitted:
            refusing.append(Reason.DUPLICATE_ORDER)
        if sleeve_of(order.client_order_id) != order.sleeve or order.sleeve == DESK:
            refusing.append(Reason.ORDER_ID)
        if self._state is not ConnectionState.READY:
            refusing.append(Reason.VENUE_NOT_READY)
        limits, instrument = self._config.pairs.get(order.pair), self._instruments.get(order.pair)
        if limits is None or instrument is None:
            return [*refusing, Reason.PAIR_NOT_CONFIGURED], stopping
        mine, desk, guards = self._config.sleeves.get(order.sleeve), self._config.desk, self._config.guards

        try:
            switched = frozenset(self._switches())
        except OSError:
            switched = frozenset()
            stopping.append(Reason.SWITCHES_UNREADABLE)
        if self._config.kill_switch in switched:
            stopping.append(Reason.KILL_SWITCH)
        if self._config.flatten_switch in switched:
            stopping.append(Reason.FLATTEN_SWITCH)

        if not self._calendar.is_open(now):
            refusing.append(Reason.MARKET_SHUT)
        quote = self._quotes.get(order.pair)
        if quote is None:
            refusing.append(Reason.NO_QUOTE)
        else:
            step = instrument.price_increment
            if order.pair not in self._quoted or now - self._fresh[order.pair] > limits.stale_quote_ns:
                refusing.append(Reason.STALE_QUOTE)
            if quote.is_crossed:
                refusing.append(Reason.CROSSED_QUOTE)
            else:
                touch = quote.ask if order.side is Side.BUY else quote.bid
                price = touch if order.limit_price is None else order.limit_price
                # An order that can only reduce what the sleeve holds has its own, wider collar: a spread that
                # has blown out must not block the orders that take risk off. Wider, and still a collar.
                collar = limits.exit_collar_ticks if self._reduces(order) else limits.price_collar_ticks
                if abs(2 * price - quote.mid_x2) > 2 * collar * step:
                    refusing.append(Reason.PRICE_COLLAR)
                if quote.spread > limits.max_spread_ticks * step:
                    stopping.append(Reason.SPREAD_TOO_WIDE)
        if self._calendar.in_blackout(now, order.pair):
            stopping.append(Reason.MACRO_BLACKOUT)
        if in_rollover_window(now, guards.rollover_before_ns, guards.rollover_after_ns):
            stopping.append(Reason.ROLLOVER_WINDOW)
        if next_close(now) - now <= guards.weekly_close_no_entry_ns:
            stopping.append(Reason.WEEKLY_CLOSE)

        if order.quantity > limits.max_order_size:
            refusing.append(Reason.ORDER_SIZE)
        caps = [(order.sleeve, min(limits.max_position, mine.max_position) if mine else limits.max_position)]
        caps.append((DESK, limits.max_position))
        if any(self._would_exceed(order, scope, cap) for scope, cap in caps):
            refusing.append(Reason.POSITION_LIMIT)
        pairs_open = {pair for _, pair in self._book.open_lines()} | {
            pending.order.pair for pending in self._pending.values() if pending.held
        }
        if order.pair not in pairs_open and len(pairs_open) >= desk.max_open_pairs:
            stopping.append(Reason.MAX_OPEN_PAIRS)

        self._forget(now)
        if mine is None:
            stopping.append(Reason.SLEEVE_NOT_CONFIGURED)
        elif sum(1 for _, sleeve in self._sent if sleeve == order.sleeve) >= mine.max_orders_per_minute:
            refusing.append(Reason.ORDER_RATE_SLEEVE)
        if len(self._sent) >= desk.max_orders_per_minute:
            refusing.append(Reason.ORDER_RATE_DESK)
        ratio = desk.max_order_to_trade_ratio
        for reason, sleeve in ((Reason.ORDER_TO_TRADE_SLEEVE, order.sleeve), (Reason.ORDER_TO_TRADE_DESK, None)):
            messages = sum(1 for _, owner in self._messages if sleeve in (None, owner))
            trades = sum(1 for _, owner in self._trades if sleeve in (None, owner))
            if messages + 1 > ratio * max(trades, 1):
                refusing.append(reason)

        for _, tripped in sorted(self._tripped.items()):
            if tripped.sleeve in (None, order.sleeve) and tripped.reason not in stopping:
                stopping.append(tripped.reason)
        if DESK in self._unlatched or order.sleeve in self._unlatched:
            stopping.append(Reason.UNLATCHED_CURRENCY)
        if self._unmarked:
            stopping.append(Reason.UNMARKED_POSITION)
        if self._broken:
            stopping.append(Reason.RECONCILIATION_BREAK)
        return refusing, stopping

    def _reserved(self, pair: str, sleeve: str) -> tuple[int, int]:
        """(buys, sells) still held for pending orders in a pair, for one sleeve or for the desk."""
        buys = sells = 0
        for pending in self._pending.values():
            if pending.order.pair == pair and sleeve in (DESK, pending.order.sleeve):
                if pending.order.side is Side.BUY:
                    buys += pending.held
                else:
                    sells += pending.held
        return buys, sells

    def _would_exceed(self, order: Order, scope: str, cap: int) -> bool:
        """Could this order, with every pending order on its side filling in full, take `scope` past `cap`?"""
        held = self._book.position(scope, order.pair).quantity
        buys, sells = self._reserved(order.pair, scope)
        if order.side is Side.BUY:
            return held + buys + order.quantity > cap
        return held - sells - order.quantity < -cap

    def _reduces(self, order: Order) -> bool:
        """Can this order only bring the sleeve's position towards zero, never through it, whatever fills?"""
        held = self._book.position(order.sleeve, order.pair).quantity
        buys, sells = self._reserved(order.pair, order.sleeve)
        if order.side is Side.SELL:
            return held - sells - order.quantity >= 0
        return held + buys + order.quantity <= 0

    def _forget(self, now: int) -> None:
        """Drop what has left its window: a minute for the order rate, the configured one for the trade ratio."""
        ratio_window = self._config.desk.order_to_trade_window_ns
        for window, ages in ((MINUTE, self._sent), (ratio_window, self._messages), (ratio_window, self._trades)):
            while ages and ages[0][0] <= now - window:
                ages.popleft()

    # ---- FLATTEN ------------------------------------------------------------------------------------------------

    def flatten_orders(self) -> tuple[Order, ...]:
        """While FLATTEN is set: one closing order for each line the book holds that has no order in flight.

        Each is a market order for what the book holds, never more, and no larger than the pair's order limit.
        They are proposals: each still has to pass `check`, like any order. A line with an order in flight
        gets none, so a close is never sent on top of an order that may still change the position.

        A line whose last close was not allowed (a stale quote, a book beyond even the exit collar) is proposed
        again only after the configured interval, so a book that stays bad costs one refusal per interval and
        not one per call. A close that was allowed needs no wait: when it has ended, what is left follows.
        """
        now = self._tick()
        try:
            if self._config.flatten_switch not in self._switches():
                return ()
        except OSError:
            return ()
        busy = {(pending.order.sleeve, pending.order.pair) for pending in self._pending.values()}
        out = []
        for (sleeve, pair), quantity in self._book.open_lines().items():
            limits = self._config.pairs.get(pair)
            if limits is None or (sleeve, pair) in busy:
                continue
            last = self._closing.get((sleeve, pair))
            if last is not None and last[0] not in self._admitted and now - last[1] < self._config.flatten_retry_ns:
                continue
            side = Side.SELL if quantity > 0 else Side.BUY
            self._flattened += 1
            order_id = client_order_id(
                sleeve=sleeve, pair=pair, side=side, decision_seq=now, leg=FLATTEN_LEG, attempt=self._flattened
            )
            size = min(abs(quantity), limits.max_order_size)
            self._closing[sleeve, pair] = (order_id, now)
            out.append(Order(order_id, sleeve, pair, side, size, OrderType.MARKET, TimeInForce.IOC, now))
        return tuple(out)

    # ---- loss control -------------------------------------------------------------------------------------------

    def _results(self, scope: str) -> tuple[dict[str, int], set[str]]:
        """(net result by currency in millionths, the currencies that cannot be valued) for a sleeve or the desk."""
        if self._fees is None:
            self._fees = {name: self._book.fees(name) for name in (DESK, *self._book.sleeves())}
        lines = self._book.open_lines()
        out: dict[str, int] = {}
        blind: set[str] = set()
        for pair, instrument in self._instruments.items():
            quote = self._quotes.get(pair)
            if quote is None and any(p == pair and scope in (DESK, s) for s, p in lines):
                blind.add(instrument.quote)
                continue
            bid, ask = (quote.bid, quote.ask) if quote else (1, 1)
            gross = self._book.gross(scope, pair, bid, ask) * int(10 ** (CASH_SCALE - instrument.price_scale))
            out[instrument.quote] = out.get(instrument.quote, 0) + gross
        for currency, amount in self._fees.get(scope, {}).items():
            out[currency] = out.get(currency, 0) - amount
        return out, blind

    def _evaluate(self, now: int) -> None:
        """Value every scope at the last quotes, move its marks, and trip what has been breached."""
        day = last_rollover(now)
        self._unlatched.clear()
        self._unmarked = False
        # Every sleeve that has limits is measured from the start, so its day opens on nothing, not on its first loss.
        for scope in (DESK, *sorted(set(self._book.sleeves()) | set(self._config.sleeves))):
            results, blind = self._results(scope)
            self._unmarked = self._unmarked or bool(blind)
            sleeve = None if scope == DESK else scope
            limits = self._config.desk if sleeve is None else self._config.sleeves.get(sleeve)
            if limits is None:
                continue  # a sleeve with no limits may not enter at all; the desk's latches see its result
            for currency in sorted(set(results) | set(limits.loss_latches)):
                result = results.get(currency, 0)
                if currency in blind:
                    continue
                latch = limits.loss_latches.get(currency)
                if latch is None:
                    if result < 0:
                        self._unlatched.add(scope)
                    continue
                self._measure(scope, sleeve, currency, result, latch, day, now)
        self._dirty = False

    def _measure(
        self, scope: str, sleeve: str | None, currency: str, result: int, latch: LossLatch, day: int, now: int
    ) -> None:
        mark = self._marks.get((scope, currency))
        if mark is None:
            mark = self._marks[scope, currency] = _Mark(day, result, result)
        if day > mark.day:
            mark.day, mark.day_open = day, result
        mark.peak = max(mark.peak, result)
        numbers = {"result": result, "day_open": mark.day_open, "peak": mark.peak}
        breached = False
        if result - mark.day_open <= -latch.daily_loss_micro:
            limit = latch.daily_loss_micro
            breached |= self._trip(_LOSS[sleeve is None, True], sleeve, currency, now, limit=limit, **numbers)
        if mark.peak - result >= latch.drawdown_micro:
            limit = latch.drawdown_micro
            breached |= self._trip(_LOSS[sleeve is None, False], sleeve, currency, now, limit=limit, **numbers)
        self._write_mark(scope, currency, mark, now, force=breached)

    def _write_mark(self, scope: str, currency: str, mark: _Mark, now: int, *, force: bool = False) -> None:
        """Keep the marks in the journal, so a restart measures from where this process did."""
        state = (mark.day, mark.day_open, mark.peak)
        if state == mark.written:
            return
        moved = mark.written is None or state[:2] != mark.written[:2]
        if not (force or moved or now - mark.written_ns >= MARK_EVERY_NS):
            return
        self._journal.write(
            "mark", scope=scope, currency=currency, day=mark.day, day_open=mark.day_open, peak=mark.peak, at_ns=now
        )
        self._journal.flush()
        mark.written, mark.written_ns = state, now

    # ---- latches ------------------------------------------------------------------------------------------------

    def latches(self) -> tuple[str, ...]:
        """The names of the latches that are tripped now."""
        return tuple(sorted(self._tripped))

    def _trip(self, reason: Reason, sleeve: str | None, currency: str | None, now: int, **numbers: int) -> bool:
        """Trip a latch and journal it. False if it was tripped already."""
        name = latch_name(reason, sleeve, currency)
        if name in self._tripped:
            return False
        self._tripped[name] = _Tripped(reason, sleeve, currency)
        self._journal.write(
            "trip", latch=name, reason=reason.value, sleeve=sleeve, currency=currency, at_ns=now, **numbers
        )
        self._journal.flush()
        return True

    def reset(self, name: str, *, operator: str) -> None:
        """Clear a tripped latch, on an operator's say-so, and journal who said so.

        A loss latch starts again from the result as it stands: the daily one from a new opening result, the
        drawdown one from a new high-water mark. Without that it would trip again at the next quote.
        """
        token("operator", operator)
        tripped = self._tripped.get(name)
        if tripped is None:
            raise LatchError(f"no latch named {name!r} is tripped: {', '.join(self.latches()) or 'none is'}")
        now = self._tick()
        if tripped.currency is not None:
            scope = DESK if tripped.sleeve is None else tripped.sleeve
            results, blind = self._results(scope)
            mark = self._marks.get((scope, tripped.currency))
            if tripped.currency in blind or mark is None:
                raise LatchError(f"{name} cannot start again while a position in {tripped.currency} has no quote")
            result = results.get(tripped.currency, 0)
            if tripped.reason in (Reason.DAILY_LOSS_DESK, Reason.DAILY_LOSS_SLEEVE):
                mark.day_open = result
            else:
                mark.peak = result
            self._write_mark(scope, tripped.currency, mark, now, force=True)
        elif tripped.reason is Reason.REJECT_STREAK:
            self._streak(0, now)
        del self._tripped[name]
        self._journal.write("reset", latch=name, operator=operator, at_ns=now)
        self._journal.flush()

    def _restore(self, entry: dict[str, object]) -> None:
        """One line of the journal as the gate was left: a latch tripped or reset, or a mark."""
        kind, name = entry.get("type"), entry.get("latch")
        if kind == "trip" and isinstance(name, str):
            sleeve, currency = entry.get("sleeve"), entry.get("currency")
            self._tripped[name] = _Tripped(
                Reason(entry.get("reason")),
                sleeve if isinstance(sleeve, str) else None,
                currency if isinstance(currency, str) else None,
            )
        elif kind == "reset" and isinstance(name, str):
            self._tripped.pop(name, None)
        elif kind == "streak":
            rejects = entry.get("rejects")
            self._rejects = rejects if type(rejects) is int and rejects >= 0 else self._rejects
        elif kind == "mark":
            scope, currency = entry.get("scope"), entry.get("currency")
            day, day_open, peak = entry.get("day"), entry.get("day_open"), entry.get("peak")
            whole = isinstance(day, int) and isinstance(day_open, int) and isinstance(peak, int)
            if isinstance(scope, str) and isinstance(currency, str) and whole:
                assert isinstance(day, int) and isinstance(day_open, int) and isinstance(peak, int)
                self._marks[scope, currency] = _Mark(day, day_open, peak, written=(day, day_open, peak))

    # ---- reading ------------------------------------------------------------------------------------------------

    @property
    def book(self) -> Book:
        """The gate's own book: what it has been shown was filled, and nothing else."""
        return self._book

    def _tick(self) -> int:
        self._now = max(self._now, self._clock.now_ns())  # a clock that is set back does not take the gate with it
        return self._now
