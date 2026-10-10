"""Shared pieces for the venue tests: instruments, orders, a ready fake broker, compact views of an event list."""
from __future__ import annotations

from hftlab.core import (
    Fill,
    Instrument,
    InstrumentKind,
    Order,
    OrderEvent,
    OrderType,
    Quote,
    Side,
    SimClock,
    TimeInForce,
    client_order_id,
)
from hftlab.safety import PaperGate
from hftlab.venues.fake import FakeBroker, FakeConfig, build
from hftlab.venues.port import CommissionEvent, ConnectionEvent, DepthSnapshot, VenueNotice
from hftlab.venues.supervisor import Backoff

MS = 1_000_000
SECOND = 1_000 * MS
T0 = 1_760_000_000 * SECOND  # an arbitrary instant in 2025, in UTC nanoseconds

EURUSD = Instrument.from_pair(
    "EUR.USD", price_scale=5, min_order_size=20_000, kind=InstrumentKind.CASH, size_increment=1_000, price_increment=5
)
EURUSD_CFD = Instrument.from_pair(
    "EUR.USD", price_scale=5, min_order_size=1_000, kind=InstrumentKind.CFD, size_increment=1, price_increment=5
)
USDJPY = Instrument.from_pair(
    "USD.JPY", price_scale=3, min_order_size=20_000, kind=InstrumentKind.CASH, size_increment=1_000, price_increment=5
)
INSTRUMENTS = (EURUSD, EURUSD_CFD, USDJPY)
BID, ASK = 108_500, 108_510


def an_order(
    number: int = 1,
    *,
    side: Side = Side.BUY,
    quantity: int = 20_000,
    limit: int | None = None,
    tif: TimeInForce = TimeInForce.DAY,
    pair: str = "EUR.USD",
    created_ns: int = T0,
) -> Order:
    """A market order, or a limit order when `limit` is given. `number` stands for the decision it came from."""
    order_id = client_order_id(sleeve="control", pair=pair, side=side, decision_seq=number)
    kind = OrderType.MARKET if limit is None else OrderType.LIMIT
    return Order(order_id, "control", pair, side, quantity, kind, tif, created_ns, limit)


def a_broker(
    config: FakeConfig | None = None,
    *,
    environ: dict[str, str] | None = None,
    ready: bool = True,
    quote: bool = True,
    backoff: Backoff | None = None,
    **options,
) -> tuple[SimClock, FakeBroker]:
    """A fake broker at T0. By default it is connected, reconciled, subscribed to EUR.USD and has one quote."""
    clock = SimClock(T0)
    config = config or FakeConfig()
    gate = PaperGate(port=config.port, env={"MODE": "paper"} if environ is None else environ)
    broker = build(clock, INSTRUMENTS, config, gate=gate, backoff=backoff, **options)
    if quote:
        broker.market.script_quote(T0, "EUR.USD", BID, ASK)
    if ready:
        broker.connect()
        broker.subscribe_quotes(broker.instrument("EUR.USD", InstrumentKind.CASH))
        broker.mark_reconciled()
        broker.poll()
    return clock, broker


def after(clock: SimClock, broker, duration_ns: int) -> list:
    """Let `duration_ns` pass, then everything the venue reports."""
    clock.advance(duration_ns)
    return broker.poll()


def brief(events) -> list[tuple]:
    """Each event as a short tuple, without times or sequence numbers, for exact comparisons."""
    out: list[tuple] = []
    for event in events:
        if isinstance(event, ConnectionEvent):
            out.append(("connection", event.state.name))
        elif isinstance(event, Quote):
            out.append(("quote", event.pair, event.bid, event.ask))
        elif isinstance(event, OrderEvent):
            out.append(("order", event.state.name, event.filled_quantity))
        elif isinstance(event, Fill):
            out.append(("fill", event.side.name, event.quantity, event.price, event.liquidity.name))
        elif isinstance(event, CommissionEvent):
            out.append(("commission", event.commission, event.currency))
        elif isinstance(event, DepthSnapshot):
            out.append(("depth", len(event.bids), len(event.asks)))
        elif isinstance(event, VenueNotice):
            out.append(("notice", event.code))
    return out


def of(events, kind: type) -> list:
    return [event for event in events if isinstance(event, kind)]


# ---- one scenario, two venues --------------------------------------------------------------------------------------


class Harness:
    """A venue over a fake market, either the fake broker or the broker adapter over the stub client."""

    def __init__(
        self,
        kind: str,
        config: FakeConfig | None = None,
        *,
        environ: dict[str, str] | None = None,
        backoff: Backoff | None = None,
        seed: int = 0,
        reject=None,
        port: int | None = None,
    ) -> None:
        from hftlab.venues.fake import FakeMarket
        from hftlab.venues.ibkr.adapter import IbkrSettings, IbkrVenue
        from tests.venues.stub_ib import StubIb

        self.kind = kind
        self.clock = SimClock(T0)
        config = config or FakeConfig()
        self.environ = {"MODE": "paper"} if environ is None else environ
        self.market = FakeMarket(INSTRUMENTS, config, seed=seed, reject=reject)
        self.gate = PaperGate(port=config.port if port is None else port, env=self.environ)
        self.stub = None
        if kind == "fake":
            self.venue = FakeBroker(self.clock, self.market, gate=self.gate, backoff=backoff)
        elif kind == "sim":
            from hftlab.venues.sim import SimVenue

            self.venue = SimVenue(self.clock, INSTRUMENTS, plain_costs(), seed=seed)
        else:
            self.stub = StubIb(self.clock, self.market)
            settings = IbkrSettings(port=config.port)
            options = {"settings": settings, "gate": self.gate, "backoff": backoff}
            self.venue = IbkrVenue(clock=self.clock, client=self.stub, **options)

    def ready(self, *, quote: bool = True) -> Instrument:
        """Connect, look EUR.USD up, subscribe, reconcile, and swallow the events of doing so."""
        if quote:
            self.market.script_quote(T0, "EUR.USD", BID, ASK)
        self.venue.connect()
        instrument = self.venue.instrument("EUR.USD", InstrumentKind.CASH)
        self._feed_shadow()  # the book as it stands, before anything is subscribed
        self.venue.subscribe_quotes(instrument)
        self.venue.mark_reconciled()
        self.venue.poll()
        return instrument

    def after(self, duration_ns: int) -> list:
        self.clock.advance(duration_ns)
        self._feed_shadow()
        return self.venue.poll()

    def _feed_shadow(self) -> None:
        """The shadow venue has no market of its own: hand it the quotes the fake market produced."""
        if self.kind != "sim":
            return
        from hftlab.venues.fake.market import QuoteTick

        for tick in self.market.advance(self.clock.now_ns()):
            if isinstance(tick, QuoteTick):
                self._fed = getattr(self, "_fed", 0) + 1
                book = (tick.bid, tick.ask, tick.bid_size, tick.ask_size)
                self.venue.feed(Quote(tick.pair, *book, tick.at_ns, self._fed))


KINDS = ("fake", "ibkr")


def plain_costs(**changes):
    """A cost model that behaves like the fake broker where the two can be compared: a fixed 5 ms, a flat fee."""
    from hftlab.venues.sim import CostModel

    values = {
        "effective_from": "test", "placeholder": True, "latency_min_ns": 5 * MS, "latency_max_ns": 5 * MS,
        "commission_rate_bps_x100": 0, "commission_minimum": {"USD": 2_000_000, "JPY": 2_000_000},
        "min_order_size": 0, "slippage_increments": 0, "stale_after_ns": 10**15, "no_fill_before_ns": 0,
        "no_fill_after_ns": 0, "widen_before_ns": 0, "widen_after_ns": 0, "spread_multiplier_x100": 100,
    }  # fmt: skip
    return CostModel(**{**values, **changes})
