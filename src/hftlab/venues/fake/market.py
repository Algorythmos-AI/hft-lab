"""The broker's side of the fake: quotes over time, an order book of resting orders, executions.

Deterministic by construction. Everything happens at a scheduled time on one queue, ties break in the order
they were scheduled, random quotes come from a seeded generator, and there is no clock in here: time is whatever
`advance(to_ns)` is told. The same script and seed give the same happenings, to the nanosecond.

Fills come from the top of the book, like a broker's paper account: a marketable order takes the quoted price
at once. That is optimistic on purpose here. The conservative model is the shadow venue's job (ADR 0003), and
this one exists to exercise the plumbing: delays, partial fills, rejects, duplicates, reordering, outages.
"""
from __future__ import annotations

import heapq
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial

from hftlab.core import Instrument, InstrumentKind, Liquidity, Order, OrderType, Side, TimeInForce

MS = 1_000_000


@dataclass(frozen=True, slots=True)
class FakeConfig:
    accounts: tuple[str, ...] = ("DU1234567",)
    port: int = 4002
    ack_delay_ns: int = 2 * MS
    fill_delay_ns: int = 5 * MS  # from submission to the first look at the book; not before the acknowledgement
    cancel_delay_ns: int = 2 * MS
    fill_chunks: int = 1  # a fill is reported as this many executions
    chunk_gap_ns: int = 1 * MS
    respect_displayed_size: bool = False  # take at most the quoted size per quote, leaving the rest working
    commission: int = 2_000_000  # per execution, at CASH_SCALE
    commission_currency: str = "USD"
    duplicate_every: int = 0  # every Nth execution report is delivered twice
    reorder_every: int = 0  # every Nth execution report changes places with the one after it
    outages: tuple[tuple[int, int], ...] = ()  # [start, end) spans when the link to the broker is down
    depth_levels: int = 0  # 0: the venue offers no depth
    history: bool = True
    server_clock_offset_ns: int = 0


@dataclass(frozen=True, slots=True)
class QuoteTick:
    at_ns: int
    pair: str
    bid: int
    ask: int
    bid_size: int
    ask_size: int


@dataclass(frozen=True, slots=True)
class Acked:
    at_ns: int
    client_order_id: str
    broker_order_id: str


@dataclass(frozen=True, slots=True)
class Rejected:
    at_ns: int
    client_order_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Executed:
    at_ns: int
    exec_id: str
    client_order_id: str
    broker_order_id: str
    pair: str
    side: Side
    quantity: int
    price: int
    cumulative: int
    order_quantity: int
    liquidity: Liquidity


@dataclass(frozen=True, slots=True)
class Commissioned:
    at_ns: int
    exec_id: str
    commission: int
    currency: str


@dataclass(frozen=True, slots=True)
class Cancelled:
    at_ns: int
    client_order_id: str
    reason: str
    cumulative: int


@dataclass(frozen=True, slots=True)
class CancelTooLate:
    at_ns: int
    client_order_id: str


Happening = QuoteTick | Acked | Rejected | Executed | Commissioned | Cancelled | CancelTooLate


@dataclass(slots=True)
class BookedOrder:
    """An order as the broker holds it."""

    order: Order
    instrument: Instrument
    broker_order_id: str
    submitted_ns: int
    eligible_ns: int
    acked: bool = False
    open: bool = True
    executed: int = 0
    reserved: int = 0  # matched and waiting to be reported as executions

    @property
    def remaining(self) -> int:
        return self.order.quantity - self.executed


@dataclass(order=True, slots=True)
class _Scheduled:
    at_ns: int
    number: int
    run: Callable[[int], None] = field(compare=False)


class FakeMarket:
    def __init__(
        self,
        instruments: Iterable[Instrument],
        config: FakeConfig | None = None,
        *,
        seed: int = 0,
        reject: Callable[[Order], str | None] | None = None,
    ) -> None:
        self.config = config or FakeConfig()
        self.seed = seed
        self._instruments = {(i.pair, i.kind): i for i in instruments}
        self._reject = reject
        self._random = random.Random(seed)
        self._queue: list[_Scheduled] = []
        self._scheduled = 0
        self._now = 0
        self._out: list[Happening] = []
        self._ticks: dict[str, list[QuoteTick]] = {}
        self._top: dict[str, QuoteTick] = {}
        self._orders: dict[str, BookedOrder] = {}
        self._executions: list[Executed] = []
        self._commissions: dict[str, Commissioned] = {}
        self._reports = 0

    # ---- what is traded, and the link --------------------------------------------------------------------------

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument | None:
        return self._instruments.get((pair, kind))

    def instruments(self) -> list[Instrument]:
        return list(self._instruments.values())

    def link_up(self, at_ns: int) -> bool:
        return not any(start <= at_ns < end for start, end in self.config.outages)

    def outage_start(self, after_ns: int, upto_ns: int) -> int | None:
        """When the link first goes down in (after_ns, upto_ns], or None."""
        starts = [start for start, end in self.config.outages if after_ns < start <= upto_ns and end > start]
        return min(starts, default=None)

    def next_at(self) -> int | None:
        """The time of the next thing that will happen, for a driver that owns the clock."""
        return self._queue[0].at_ns if self._queue else None

    # ---- quotes ------------------------------------------------------------------------------------------------

    def script_quote(
        self, at_ns: int, pair: str, bid: int, ask: int, bid_size: int = 1_000_000, ask_size: int = 1_000_000
    ) -> None:
        if at_ns < self._now:
            raise ValueError(f"a quote at {at_ns} cannot be scripted once the market is at {self._now}")
        self._at(at_ns, partial(self._on_quote, QuoteTick(at_ns, pair, bid, ask, bid_size, ask_size)))

    def generate_quotes(
        self, pair: str, *, start_ns: int, count: int, interval_ns: int, mid: int, spread: int, step: int = 1
    ) -> None:
        """A seeded random walk: `count` quotes `interval_ns` apart, the mid moving by -step, 0 or +step each time."""
        for i in range(count):
            mid = max(spread + step, mid + self._random.choice((-step, 0, step)))
            half = spread // 2
            sizes = (self._random.randrange(1, 6) * 500_000, self._random.randrange(1, 6) * 500_000)
            self.script_quote(start_ns + i * interval_ns, pair, mid - half, mid - half + spread, *sizes)

    def top(self, pair: str) -> QuoteTick | None:
        return self._top.get(pair)

    def quotes_before(self, pair: str, end_ns: int, count: int) -> list[QuoteTick]:
        past = [tick for tick in self._ticks.get(pair, []) if tick.at_ns <= end_ns]
        return past[-count:] if count > 0 else []

    # ---- orders ------------------------------------------------------------------------------------------------

    def submit(self, order: Order, instrument: Instrument, at_ns: int) -> str:
        """The broker receives an order at `at_ns`. Returns the broker's own id for it."""
        if order.client_order_id in self._orders:
            raise ValueError(f"the broker already holds {order.client_order_id}")
        booked = BookedOrder(
            order, instrument, f"B{len(self._orders) + 1:06d}", at_ns, at_ns + max(self.config.fill_delay_ns, 0)
        )
        self._orders[order.client_order_id] = booked
        reason = self._reject(order) if self._reject else None
        if reason is None and self.instrument(instrument.pair, instrument.kind) != instrument:
            reason = "no such instrument"
        answered = at_ns + self.config.ack_delay_ns
        if reason is not None:
            booked.open = False
            self._at(answered, partial(self._on_reject, booked, reason))
            return booked.broker_order_id
        self._at(answered, partial(self._on_ack, booked))
        self._at(max(booked.eligible_ns, answered), partial(self._match, booked, arriving=True))
        return booked.broker_order_id

    def request_cancel(self, client_order_id: str, at_ns: int) -> None:
        booked = self._orders[client_order_id]
        self._at(at_ns + self.config.cancel_delay_ns, partial(self._on_cancel, booked, "cancelled on request"))

    def holds(self, client_order_id: str) -> bool:
        return client_order_id in self._orders

    def working(self) -> list[BookedOrder]:
        return [booked for booked in self._orders.values() if booked.open]

    def executions(self) -> list[Executed]:
        return list(self._executions)

    def commission_of(self, exec_id: str) -> Commissioned | None:
        return self._commissions.get(exec_id)

    def positions(self) -> dict[tuple[str, InstrumentKind], int]:
        out: dict[tuple[str, InstrumentKind], int] = {}
        for booked in self._orders.values():
            if booked.executed:
                key = (booked.instrument.pair, booked.instrument.kind)
                out[key] = out.get(key, 0) + booked.order.side.sign * booked.executed
        return {key: quantity for key, quantity in out.items() if quantity}

    # ---- time --------------------------------------------------------------------------------------------------

    def advance(self, to_ns: int) -> list[Happening]:
        """Everything that happens at the broker up to and including `to_ns`, in order, each exactly once."""
        if to_ns < self._now:
            raise ValueError(f"the market is at {self._now} and cannot go back to {to_ns}")
        while self._queue and self._queue[0].at_ns <= to_ns:
            item = heapq.heappop(self._queue)
            self._now = item.at_ns
            item.run(item.at_ns)
        self._now = to_ns
        out, self._out = self._faulted(self._out), []
        return out

    def _at(self, at_ns: int, run: Callable[[int], None]) -> None:
        self._scheduled += 1
        heapq.heappush(self._queue, _Scheduled(max(at_ns, self._now), self._scheduled, run))

    # ---- what happens ------------------------------------------------------------------------------------------

    def _on_quote(self, tick: QuoteTick, _at_ns: int) -> None:
        self._top[tick.pair] = tick
        self._ticks.setdefault(tick.pair, []).append(tick)
        self._out.append(tick)
        for booked in list(self._orders.values()):
            if booked.open and booked.order.pair == tick.pair and booked.eligible_ns <= tick.at_ns:
                self._match(booked, tick.at_ns, arriving=False)

    def _on_ack(self, booked: BookedOrder, at_ns: int) -> None:
        booked.acked = True
        self._out.append(Acked(at_ns, booked.order.client_order_id, booked.broker_order_id))

    def _on_reject(self, booked: BookedOrder, reason: str, at_ns: int) -> None:
        self._out.append(Rejected(at_ns, booked.order.client_order_id, reason))

    def _on_cancel(self, booked: BookedOrder, reason: str, at_ns: int) -> None:
        if not booked.open:
            self._out.append(CancelTooLate(at_ns, booked.order.client_order_id))
            return
        booked.open = False
        booked.reserved = 0
        self._out.append(Cancelled(at_ns, booked.order.client_order_id, reason, booked.executed))

    def _match(self, booked: BookedOrder, at_ns: int, *, arriving: bool) -> None:
        order = booked.order
        unmatched = booked.remaining - booked.reserved
        if not booked.open or unmatched <= 0:
            return
        immediate = order.tif in (TimeInForce.IOC, TimeInForce.FOK)
        tick = self._top.get(order.pair)
        if tick is None:
            if immediate and arriving:
                self._on_cancel(booked, "no quote to trade against", at_ns)
            return
        price, displayed = (tick.ask, tick.ask_size) if order.side is Side.BUY else (tick.bid, tick.bid_size)
        limit = order.limit_price
        marketable = (
            order.order_type is OrderType.MARKET
            or limit is None
            or (limit >= price if order.side is Side.BUY else limit <= price)
        )
        available = min(unmatched, displayed) if self.config.respect_displayed_size else unmatched
        if not marketable or available <= 0 or (order.tif is TimeInForce.FOK and available < unmatched):
            if immediate and arriving:
                self._on_cancel(booked, "not immediately executable", at_ns)
            return
        chunks = max(1, min(self.config.fill_chunks, available))
        sizes = [available // chunks] * (chunks - 1) + [available - (available // chunks) * (chunks - 1)]
        liquidity = Liquidity.TAKER if arriving else Liquidity.MAKER
        booked.reserved += available
        gap = self.config.chunk_gap_ns
        for i, size in enumerate(sizes):
            self._at(at_ns + i * gap, partial(self._execute, booked, size, price, liquidity))
        if immediate and available < unmatched:
            remainder = partial(self._on_cancel, booked, "unfilled remainder of an immediate order")
            self._at(at_ns + (len(sizes) - 1) * gap, remainder)

    def _execute(self, booked: BookedOrder, quantity: int, price: int, liquidity: Liquidity, at_ns: int) -> None:
        if not booked.open:
            return  # cancelled between the match and the report
        booked.reserved -= quantity
        booked.executed += quantity
        order = booked.order
        executed = Executed(
            at_ns, f"X{self.seed:04d}.{len(self._executions) + 1:06d}", order.client_order_id, booked.broker_order_id,
            order.pair, order.side, quantity, price, booked.executed, order.quantity, liquidity,
        )  # fmt: skip
        commission = Commissioned(at_ns, executed.exec_id, self.config.commission, self.config.commission_currency)
        self._executions.append(executed)
        self._commissions[executed.exec_id] = commission
        if booked.remaining == 0:
            booked.open = False
        self._out += [executed, commission]

    def _faulted(self, happenings: list[Happening]) -> list[Happening]:
        """Delivery faults: duplicate every Nth execution report, and swap every Nth with the one after it."""
        every_dup, every_swap = self.config.duplicate_every, self.config.reorder_every
        if not (every_dup or every_swap):
            return happenings
        units: list[list[Happening]] = []
        for happening in happenings:
            if isinstance(happening, Commissioned) and units and isinstance(units[-1][0], Executed):
                units[-1].append(happening)
            else:
                units.append([happening])
        reports = [i for i, unit in enumerate(units) if isinstance(unit[0], Executed)]
        for position, index in enumerate(reports):
            self._reports += 1
            if every_swap and self._reports % every_swap == 0 and position + 1 < len(reports):
                other = reports[position + 1]
                units[index], units[other] = units[other], units[index]
            if every_dup and self._reports % every_dup == 0:
                units[index] = [*units[index], units[index][0]]
        return [happening for unit in units for happening in unit]
