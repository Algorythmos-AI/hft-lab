"""Orders, what happens to them, and the position they add up to."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from hftlab.core import _checks
from hftlab.core.enums import Liquidity, OrderState, OrderType, Side, TimeInForce
from hftlab.core.money import require_int

# Commissions are integer millionths of their currency. A broker reports them as decimal text; convert with
# `to_scaled(text, CASH_SCALE)`.
CASH_SCALE: Final = 6


@dataclass(frozen=True, slots=True)
class Order:
    """An intent to trade. Immutable: an amended order is a new order with a new id."""

    client_order_id: str
    sleeve: str
    pair: str
    side: Side
    quantity: int
    order_type: OrderType
    tif: TimeInForce
    created_ts_ns: int
    limit_price: int | None = None

    def __post_init__(self) -> None:
        _checks.token("client_order_id", self.client_order_id)
        _checks.token("sleeve", self.sleeve)
        _checks.pair(self.pair)
        _checks.member("side", self.side, Side)
        _checks.positive("quantity", self.quantity)
        _checks.member("order_type", self.order_type, OrderType)
        _checks.member("tif", self.tif, TimeInForce)
        _checks.non_negative("created_ts_ns", self.created_ts_ns)
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("a limit order needs a limit price")
            _checks.positive("limit_price", self.limit_price)
        elif self.limit_price is not None:
            raise ValueError("a market order has no limit price")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """One step in an order's life, as the venue (or the order manager) reported it."""

    client_order_id: str
    state: OrderState
    ts_ns: int
    seq: int
    filled_quantity: int = 0  # cumulative, not this event's increment
    broker_order_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _checks.token("client_order_id", self.client_order_id)
        _checks.member("state", self.state, OrderState)
        _checks.non_negative("ts_ns", self.ts_ns)
        _checks.non_negative("seq", self.seq)
        _checks.non_negative("filled_quantity", self.filled_quantity)
        if self.broker_order_id is not None:
            _checks.token("broker_order_id", self.broker_order_id)
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be text")
        if self.state is OrderState.FILLED and self.filled_quantity == 0:
            raise ValueError("a FILLED event must carry the filled quantity")


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution. `fill_id` is the venue's execution id: the key that lets a book apply it exactly once.

    `ts_ns` is when this process received the report, like every other event. `venue_ts_ns` is the venue's own
    time for the execution when it gives one; it is coarser and is what "executions since" is asked in. A venue
    may report the same execution more than once, live and again on a reconciliation query.
    """

    fill_id: str
    client_order_id: str
    pair: str
    side: Side
    quantity: int
    price: int
    ts_ns: int
    liquidity: Liquidity = Liquidity.UNKNOWN
    commission: int = 0  # at CASH_SCALE, in commission_currency; negative for a rebate
    commission_currency: str | None = None
    venue_ts_ns: int | None = None
    seq: int = 0

    def __post_init__(self) -> None:
        _checks.token("fill_id", self.fill_id)
        _checks.token("client_order_id", self.client_order_id)
        _checks.pair(self.pair)
        _checks.member("side", self.side, Side)
        _checks.positive("quantity", self.quantity)
        _checks.positive("price", self.price)
        _checks.non_negative("ts_ns", self.ts_ns)
        _checks.member("liquidity", self.liquidity, Liquidity)
        require_int("commission", self.commission)
        if self.venue_ts_ns is not None:
            _checks.non_negative("venue_ts_ns", self.venue_ts_ns)
        _checks.non_negative("seq", self.seq)
        if self.commission_currency is not None:
            _checks.currency("commission_currency", self.commission_currency)
        elif self.commission != 0:
            raise ValueError("a commission needs its currency")

    @property
    def signed_quantity(self) -> int:
        """Base-currency units this fill adds to the position: positive for a buy."""
        return self.side.sign * self.quantity


@dataclass(frozen=True, slots=True)
class Position:
    """The net of a set of fills in one pair, as two exact sums.

    `quantity` is the signed base-currency amount: positive is long. `quote_cash` is the signed quote-currency
    amount those fills paid (negative) or received (positive), at the pair's price scale. There is no average
    price here because an average has to round. Bought 100 at 5 and sold 100 at 7 is quantity 0 and
    quote_cash +200: the profit. With a quantity still open, `marked(price)` values it at that price.
    """

    pair: str
    quantity: int = 0
    quote_cash: int = 0

    def __post_init__(self) -> None:
        _checks.pair(self.pair)
        require_int("quantity", self.quantity)
        require_int("quote_cash", self.quote_cash)

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def apply(self, fill: Fill) -> Position:
        """This position after `fill`. Applying each fill once is the caller's job (see `Fill.fill_id`)."""
        if fill.pair != self.pair:
            raise ValueError(f"a {fill.pair} fill cannot be applied to a {self.pair} position")
        signed = fill.signed_quantity
        return replace(self, quantity=self.quantity + signed, quote_cash=self.quote_cash - signed * fill.price)

    def marked(self, price: int) -> int:
        """Profit and loss since flat, with the open quantity valued at `price`. Commissions are not included."""
        return self.quote_cash + self.quantity * _checks.positive("price", price)
