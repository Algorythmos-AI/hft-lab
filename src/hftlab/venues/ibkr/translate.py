"""Translation between the broker library's objects and core types. Pure: no I/O, no clock, no state.

The library hands prices and sizes over as Python floats. The gateway sent them as decimal text, the library
parsed that text to the nearest float, and `repr` of a float is the shortest decimal that parses back to the
same float: for any value of at most 15 significant digits that is the text that was sent. So `exact()` goes
float -> repr -> Decimal -> scaled integer and loses nothing; a value the scale cannot hold raises instead of
being rounded (ADR 0002). On the way out nothing is a float at all: a limit price is sent as a `Decimal`, which
the library writes to the wire as its own text, and a quantity as an `int`.

Everything marked UNVERIFIED is a statement about what the gateway sends that could not be checked without
one. The capability probe is how each is confirmed (docs/runbooks/ibkr-probe.md).
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from ib_async import CFD, AccountValue, CommissionReport, Contract, ContractDetails, Execution, Forex, Ticker, Trade
from ib_async import Order as IbOrder
from ib_async import Position as IbPosition
from ib_async.objects import HistoricalTickBidAsk

from hftlab.core import (
    CASH_SCALE,
    Instrument,
    InstrumentKind,
    Liquidity,
    Order,
    OrderState,
    OrderType,
    Quote,
    Side,
    TimeInForce,
    is_ours,
    to_decimal,
    to_scaled,
)
from hftlab.core.money import MAX_SCALE, InexactError
from hftlab.venues.port import OpenOrder, VenueCash, VenuePosition

# UNVERIFIED: forex spot is secType CASH routed to IDEALPRO, and a forex CFD is secType CFD with the base
# currency as symbol, the quote currency as currency, routed to SMART.
SPOT_EXCHANGE = "IDEALPRO"
CFD_EXCHANGE = "SMART"
SEC_TYPES = {InstrumentKind.CASH: "CASH", InstrumentKind.CFD: "CFD"}
KINDS = {sec_type: kind for kind, sec_type in SEC_TYPES.items()}

ACTIONS = {Side.BUY: "BUY", Side.SELL: "SELL"}
SIDES = {action: side for side, action in ACTIONS.items()}
EXECUTION_SIDES = {"BOT": Side.BUY, "SLD": Side.SELL}
ORDER_TYPES = {OrderType.MARKET: "MKT", OrderType.LIMIT: "LMT"}
CORE_ORDER_TYPES = {name: kind for kind, name in ORDER_TYPES.items()}
TIFS = {TimeInForce.DAY: "DAY", TimeInForce.GTC: "GTC", TimeInForce.IOC: "IOC", TimeInForce.FOK: "FOK"}
CORE_TIFS = {name: tif for tif, name in TIFS.items()}
# UNVERIFIED: lastLiquidity is 1 for added liquidity and 2 for removed; anything else is not known.
LIQUIDITY = {1: Liquidity.MAKER, 2: Liquidity.TAKER}

_SENT_NOT_ACKNOWLEDGED = frozenset({"PendingSubmit", "ApiPending"})
_LIVE_AT_THE_VENUE = frozenset({"PreSubmitted", "Submitted", "ApiUpdate", "ValidationError"})
_CANCELLED = frozenset({"Cancelled", "ApiCancelled"})
_ORDER_CANCELED_CODE = 202
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNSET = 1e300  # the wire protocol's "no value" is the largest double; nothing real is this big


class TranslationError(ValueError):
    """The broker sent something that has no exact place in the core types. Reported, never repaired."""


# ---- numbers ---------------------------------------------------------------------------------------------------


def exact(value: object, scale: int, what: str) -> int:
    """A number the library decoded, as the exact integer at `scale`. See the module docstring for why it is exact."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TranslationError(f"{what} is not a number: {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise TranslationError(f"{what} is not finite")
    try:
        return to_scaled(Decimal(repr(value)), scale)
    except InexactError as exc:
        raise TranslationError(f"{what} {value!r} cannot be held at {scale} decimals") from exc


def whole(value: object, what: str) -> int:
    """A size or quantity as whole base-currency units."""
    return exact(value, 0, what)


def size_units(value: object, what: str) -> int:
    """A minimum size or size step as whole base-currency units, the only sizes this platform trades in.

    The gateway reports some pairs in whole units (1, 1000, 20000) and some finer than one unit (0.01 for
    EUR.USD, seen on the paper gateway). A whole number is taken as it is. A fraction of a unit is taken as one
    unit, but only when one unit is an exact multiple of it, so that every whole-unit size is one the gateway
    accepts. Anything else (1.5, 0.3, zero, not a number) is refused rather than rounded.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TranslationError(f"{what} is not a number: {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise TranslationError(f"{what} is not finite")
    stated = Decimal(repr(value))
    if stated <= 0:
        raise TranslationError(f"{what} {value!r} is not a positive size")
    if stated < 1:
        if Decimal(1) % stated != 0:
            raise TranslationError(f"{what} {value!r} is a fraction that one whole unit is not a multiple of")
        return 1
    return whole(value, what)


def increment_of(tick: object, what: str) -> tuple[int, int]:
    """(decimals, increment in units of those decimals) of a price step: 0.00005 is (5, 5), 0.005 is (3, 5)."""
    if isinstance(tick, bool) or not isinstance(tick, int | float) or not math.isfinite(tick) or tick <= 0:
        raise TranslationError(f"{what} is not a positive number: {tick!r}")
    exponent = Decimal(repr(tick)).normalize().as_tuple().exponent
    assert isinstance(exponent, int)
    scale = max(0, -exponent)
    if scale > MAX_SCALE:
        raise TranslationError(f"{what} {tick!r} is finer than {MAX_SCALE} decimals")
    return scale, exact(tick, scale, what)


def present(value: object) -> bool:
    """Did the gateway send this price or size? It sends nan, -1 or the largest double when it has none."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and 0 < value < _UNSET


# ---- time ------------------------------------------------------------------------------------------------------


def ns_of(moment: object) -> int | None:
    """A time zone aware datetime as UTC nanoseconds; None for one without a zone, which cannot be placed."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        return None
    since = moment - _EPOCH
    if since < timedelta(0):
        return None
    return (since.days * 86_400 + since.seconds) * 1_000_000_000 + since.microseconds * 1_000


def utc(ts_ns: int) -> datetime:
    """UTC nanoseconds as the whole-second datetime the gateway's requests take."""
    return datetime.fromtimestamp(ts_ns // 1_000_000_000, tz=UTC)


# ---- instruments -----------------------------------------------------------------------------------------------


def contract_for(pair: str, kind: InstrumentKind) -> Contract:
    """The contract to ask the gateway about. It is not usable for orders until the gateway has qualified it."""
    base, _, quote = pair.partition(".")
    if kind is InstrumentKind.CASH:
        return Forex(symbol=base, currency=quote, exchange=SPOT_EXCHANGE)
    return CFD(symbol=base, currency=quote, exchange=CFD_EXCHANGE)


def pair_of(contract: Contract | None) -> str | None:
    """The pair a forex contract stands for, or None for anything that is not forex spot or a forex CFD."""
    if contract is None or contract.secType not in KINDS:
        return None
    base, quote = contract.symbol, contract.currency
    if len(base) != 3 or len(quote) != 3 or not (base + quote).isalpha() or not (base + quote).isupper():
        return None
    return f"{base}.{quote}"


def instrument_from(details: ContractDetails, pair: str, kind: InstrumentKind) -> Instrument:
    """The instrument the gateway described. Every number comes from the gateway; none is assumed.

    For forex, `minTick` is the price increment (0.00005 for EUR.USD). `minSize` and `sizeIncrement` are in
    base-currency units and may be finer than one unit (`size_units`). `minSize` is the smallest order the
    gateway takes, not the size at which its pricing is good: that is the cost model's business. A gateway that
    reports none of the size fields raises here rather than be given a default.
    """
    contract = details.contract
    if pair_of(contract) != pair or contract is None or KINDS.get(contract.secType) is not kind:
        raise TranslationError(f"the gateway answered a request for {pair} {kind.value} with another contract")
    scale, price_increment = increment_of(details.minTick, f"{pair} minTick")
    if not present(details.minSize) or not present(details.sizeIncrement):
        raise TranslationError(f"the gateway reported no minimum size or size increment for {pair} {kind.value}")
    return Instrument.from_pair(
        pair,
        price_scale=scale,
        min_order_size=size_units(details.minSize, f"{pair} minSize"),
        kind=kind,
        size_increment=size_units(details.sizeIncrement, f"{pair} sizeIncrement"),
        price_increment=price_increment,
    )


# ---- market data -----------------------------------------------------------------------------------------------


def quote_from(ticker: Ticker, instrument: Instrument) -> tuple[int, int, int, int] | None:
    """(bid, ask, bid size, ask size) from a ticker, or None while either side of the book is missing."""
    if not (present(ticker.bid) and present(ticker.ask)):
        return None
    scale, name = instrument.price_scale, instrument.pair
    bid_size = whole(ticker.bidSize, f"{name} bid size") if present(ticker.bidSize) else 0
    ask_size = whole(ticker.askSize, f"{name} ask size") if present(ticker.askSize) else 0
    return exact(ticker.bid, scale, f"{name} bid"), exact(ticker.ask, scale, f"{name} ask"), bid_size, ask_size


Levels = tuple[tuple[int, int], ...]


def depth_from(ticker: Ticker, instrument: Instrument) -> tuple[Levels, Levels]:
    """(bids, asks) as (price, size) levels, best first, from the ticker's book."""
    scale, name = instrument.price_scale, instrument.pair

    def side(levels: object, label: str) -> Levels:
        assert isinstance(levels, list)
        return tuple(
            (exact(level.price, scale, f"{name} {label} price"), whole(level.size, f"{name} {label} size"))
            for level in levels
            if present(level.price)
        )

    return side(ticker.domBids, "bid depth"), side(ticker.domAsks, "ask depth")


def history_quote(tick: HistoricalTickBidAsk, instrument: Instrument, seq: int) -> Quote | None:
    """A historical tick as a `Quote` whose receive time is the venue's tick time (one second resolution)."""
    at_ns = ns_of(tick.time)
    if at_ns is None or not (present(tick.priceBid) and present(tick.priceAsk)):
        return None
    scale, name = instrument.price_scale, instrument.pair
    return Quote(
        name,
        exact(tick.priceBid, scale, f"{name} historical bid"),
        exact(tick.priceAsk, scale, f"{name} historical ask"),
        whole(tick.sizeBid, f"{name} historical bid size") if present(tick.sizeBid) else 0,
        whole(tick.sizeAsk, f"{name} historical ask size") if present(tick.sizeAsk) else 0,
        at_ns,
        seq,
    )


# ---- orders ----------------------------------------------------------------------------------------------------


def to_ib_order(order: Order, instrument: Instrument, account: str) -> IbOrder:
    """The broker's order for one of ours. `orderRef` carries the client order id, one to one."""
    ib_order = IbOrder(
        action=ACTIONS[order.side],
        totalQuantity=order.quantity,
        orderType=ORDER_TYPES[order.order_type],
        tif=TIFS[order.tif],
        orderRef=order.client_order_id,
        account=account,
        transmit=True,
    )
    if order.limit_price is not None:
        ib_order.lmtPrice = to_decimal(order.limit_price, instrument.price_scale)
    return ib_order


def order_terms(ib_order: IbOrder, instrument: Instrument) -> tuple[Side, int, OrderType, int | None, TimeInForce]:
    """(side, quantity, type, limit price, time in force) of a broker order, in core terms."""
    try:
        side, kind = SIDES[ib_order.action], CORE_ORDER_TYPES[ib_order.orderType]
        tif = CORE_TIFS[ib_order.tif or "DAY"]
    except KeyError as exc:
        raise TranslationError(f"an order field this platform does not use: {exc.args[0]!r}") from exc
    limit = None
    if kind is OrderType.LIMIT:
        price = ib_order.lmtPrice
        limit = (
            to_scaled(price, instrument.price_scale)
            if isinstance(price, Decimal)
            else exact(price, instrument.price_scale, "limit price")
        )
    return side, whole(ib_order.totalQuantity, "order quantity"), kind, limit, tif


def state_from(status: str, filled: int, *, acknowledged: bool, error_code: int) -> OrderState | None:
    """The core state for a broker order status; None for one that changes nothing (a cancel in flight).

    The library reports a rejected new order as Cancelled with the error in the trade's log, so a cancel that
    carries an error other than "order cancelled" on an order the venue never acknowledged is a rejection.
    UNVERIFIED: the unfilled remainder of an IOC order and an expired DAY order both arrive as Cancelled, so
    EXPIRED is never reported by this adapter.
    """
    if status in _SENT_NOT_ACKNOWLEDGED:
        return OrderState.SUBMITTED
    if status in _LIVE_AT_THE_VENUE:
        return OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.WORKING
    if status == "Filled":
        return OrderState.FILLED
    if status in _CANCELLED:
        rejected = not acknowledged and filled == 0 and error_code not in (0, _ORDER_CANCELED_CODE)
        return OrderState.REJECTED if rejected else OrderState.CANCELLED
    if status == "Inactive":
        return OrderState.REJECTED
    return None


def open_order_from(trade: Trade, instrument: Instrument) -> OpenOrder:
    """A working order as the venue holds it. One this platform did not send has no client order id."""
    side, quantity, kind, limit, tif = order_terms(trade.order, instrument)
    status = trade.orderStatus
    filled = whole(status.filled, "filled quantity")
    reference = trade.order.orderRef
    state = state_from(status.status, filled, acknowledged=True, error_code=0) or OrderState.WORKING
    return OpenOrder(
        client_order_id=reference if is_ours(reference) else None,
        broker_order_id=str(trade.order.permId or status.permId or trade.order.orderId),
        pair=instrument.pair,
        side=side,
        quantity=quantity,
        filled_quantity=filled,
        order_type=kind,
        limit_price=limit,
        tif=tif,
        state=state,
    )


# ---- executions ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionTerms:
    """An execution in core terms: a `Fill` without the receive time and sequence number the venue adds."""

    fill_id: str
    client_order_id: str
    pair: str
    side: Side
    quantity: int
    price: int
    liquidity: Liquidity
    venue_ts_ns: int | None


def execution_terms(execution: Execution, instrument: Instrument) -> ExecutionTerms:
    """One execution report, translated.

    UNVERIFIED: `side` is BOT or SLD, `shares` is a whole number of base-currency units, `price` has no more
    decimals than the instrument's quotes, and `time` carries a time zone.
    """
    side = EXECUTION_SIDES.get(execution.side)
    if side is None:
        raise TranslationError(f"execution side {execution.side!r} is neither BOT nor SLD")
    if not execution.execId:
        raise TranslationError("an execution without an execution id cannot be booked exactly once")
    return ExecutionTerms(
        fill_id=execution.execId,
        client_order_id=execution.orderRef,
        pair=instrument.pair,
        side=side,
        quantity=whole(execution.shares, "executed quantity"),
        price=exact(execution.price, instrument.price_scale, "execution price"),
        liquidity=LIQUIDITY.get(execution.lastLiquidity, Liquidity.UNKNOWN),
        venue_ts_ns=ns_of(execution.time),
    )


def commission_from(report: CommissionReport) -> tuple[str, int, str] | None:
    """(execution id, commission at CASH_SCALE, currency), or None while the report is empty.

    A commission is the one number that is rounded: the broker computes it as a fraction of the trade value and
    may report more decimals than a millionth. It is rounded half-even to CASH_SCALE; a price never is.
    """
    amount = report.commission
    if not report.execId or not report.currency or not isinstance(amount, int | float) or isinstance(amount, bool):
        return None
    if not math.isfinite(amount) or abs(amount) >= _UNSET:
        return None
    quantum = Decimal(1).scaleb(-CASH_SCALE)
    rounded = Decimal(repr(amount)).quantize(quantum, rounding=ROUND_HALF_EVEN)
    return report.execId, to_scaled(rounded, CASH_SCALE), report.currency


def position_from(position: IbPosition) -> VenuePosition | None:
    """A forex position as the venue reports it; None for any other instrument or a flat one."""
    pair = pair_of(position.contract)
    if pair is None:
        return None
    quantity = whole(position.position, f"{pair} position")
    return VenuePosition(pair, KINDS[position.contract.secType], quantity) if quantity else None


CASH_TAG: Final = "CashBalance"
_ALL_CURRENCIES: Final = "BASE"  # the gateway's row for everything valued in the account's own currency: not a holding


def cash_from(values: Iterable[AccountValue]) -> tuple[VenueCash, ...]:
    """The cash an account holds, by currency, from the gateway's account values.

    A balance is the broker's own decimal text. One with more decimals than a millionth is rounded half-even to
    CASH_SCALE, like a commission; text that is not a number is a `TranslationError`, never a guess.
    """
    out: dict[str, int] = {}
    quantum = Decimal(1).scaleb(-CASH_SCALE)
    for value in values:
        if value.tag != CASH_TAG or value.currency == _ALL_CURRENCIES:
            continue
        try:
            amount = Decimal(value.value)
        except ArithmeticError as exc:
            raise TranslationError(f"the cash balance in {value.currency} is not a number") from exc
        if not amount.is_finite() or len(value.currency) != 3:
            raise TranslationError(f"the cash balance in {value.currency!r} cannot be read")
        out[value.currency] = to_scaled(amount.quantize(quantum, rounding=ROUND_HALF_EVEN), CASH_SCALE)
    return tuple(VenueCash(currency, amount) for currency, amount in sorted(out.items()))
