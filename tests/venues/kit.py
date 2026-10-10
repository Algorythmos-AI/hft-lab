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
