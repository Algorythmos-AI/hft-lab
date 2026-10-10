"""What is traded: a currency pair, in one form, at one price precision."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hftlab.core import _checks
from hftlab.core.enums import InstrumentKind
from hftlab.core.money import from_scaled, require_scale, to_scaled


@dataclass(frozen=True, slots=True)
class Instrument:
    """A forex pair as one venue quotes it.

    `price_scale` is the number of decimals in a quote: 5 for EUR.USD, 3 for USD.JPY. Every price of this
    instrument is an integer at that scale. `price_increment` is the smallest step between two prices, in those
    integer units: a venue that quotes EUR.USD in half pips has scale 5 and increment 5. `min_order_size` and
    `size_increment` are in base-currency units. All four come from the venue, never from a guess.
    """

    base: str
    quote: str
    price_scale: int
    min_order_size: int
    kind: InstrumentKind
    size_increment: int = 1
    price_increment: int = 1

    def __post_init__(self) -> None:
        _checks.currency("base", self.base)
        _checks.currency("quote", self.quote)
        if self.base == self.quote:
            raise ValueError(f"base and quote are both {self.base}")
        require_scale(self.price_scale)
        _checks.positive("min_order_size", self.min_order_size)
        _checks.member("kind", self.kind, InstrumentKind)
        _checks.positive("size_increment", self.size_increment)
        _checks.positive("price_increment", self.price_increment)

    @classmethod
    def from_pair(
        cls,
        pair: str,
        *,
        price_scale: int,
        min_order_size: int,
        kind: InstrumentKind,
        size_increment: int = 1,
        price_increment: int = 1,
    ) -> Instrument:
        """Build from the venue's name for the pair: `Instrument.from_pair("EUR.USD", ...)`."""
        _checks.pair(pair)
        return cls(pair[:3], pair[4:], price_scale, min_order_size, kind, size_increment, price_increment)

    def refusals(self, quantity: int, limit_price: int | None) -> list[str]:
        """Why an order of this size and price cannot be sent for this instrument; empty when it can."""
        out = []
        if quantity < self.min_order_size:
            out.append(f"quantity {quantity} is below the minimum order size {self.min_order_size}")
        if quantity % self.size_increment:
            out.append(f"quantity {quantity} is not a multiple of the size increment {self.size_increment}")
        if limit_price is not None and limit_price % self.price_increment:
            out.append(f"limit price {limit_price} is not a multiple of the price increment {self.price_increment}")
        return out

    @property
    def pair(self) -> str:
        return f"{self.base}.{self.quote}"

    def price(self, value: str | Decimal) -> int:
        """A decimal price as this instrument's integer price; refuses one finer than the instrument quotes."""
        if isinstance(value, int):
            raise TypeError("an int is already a scaled price: pass the decimal string the venue sent")
        return to_scaled(value, self.price_scale)

    def format_price(self, price: int) -> str:
        return from_scaled(price, self.price_scale)
