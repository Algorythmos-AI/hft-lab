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
    instrument is an integer at that scale. `min_order_size` is in base-currency units and comes from the
    venue's capability file, never from a guess.
    """

    base: str
    quote: str
    price_scale: int
    min_order_size: int
    kind: InstrumentKind

    def __post_init__(self) -> None:
        _checks.currency("base", self.base)
        _checks.currency("quote", self.quote)
        if self.base == self.quote:
            raise ValueError(f"base and quote are both {self.base}")
        require_scale(self.price_scale)
        _checks.positive("min_order_size", self.min_order_size)
        _checks.member("kind", self.kind, InstrumentKind)

    @classmethod
    def from_pair(cls, pair: str, *, price_scale: int, min_order_size: int, kind: InstrumentKind) -> Instrument:
        """Build from the venue's name for the pair: `Instrument.from_pair("EUR.USD", ...)`."""
        _checks.pair(pair)
        return cls(pair[:3], pair[4:], price_scale, min_order_size, kind)

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
