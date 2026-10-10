"""The book: positions, cash and profit per sleeve and for the desk, built from fills and nothing else.

A strategy's belief about its own position never enters here. The only inputs are `Fill` and `CommissionEvent`,
and each is applied once: a fill that arrives again, as a venue may send it, changes nothing.

Everything is an exact integer.

  position     core `Position`: the signed base-currency quantity and the quote-currency cash those fills paid
               or received, at the pair's price scale. Both are sums, so the order of booking cannot matter.
  realised     profit on quantity that has been closed, matching the oldest open quantity first (FIFO).
  unrealised   what closing the rest now would add: a long is valued at the bid, a short at the ask.
  fees         commissions, in millionths of their currency, kept per fill so none is counted twice or lost.

The identity that ties them together, and that the tests hold for any sequence of fills: at any single mark
price, realised + unrealised equals the position's cash plus its quantity valued at that price. Amounts in
different currencies are never added; a desk total is per pair and per currency.

No currency is converted here. A net result is therefore net of the fees charged in the pair's quote currency
and of no others: a fee in any other currency is carried beside the net, by currency, for whoever has a rate.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from hftlab.core import CASH_SCALE, Fill, Instrument, Position, sleeve_of
from hftlab.venues.port import CommissionEvent

UNATTRIBUTED: Final = "unattributed"
DESK: Final = "desk"


class BookError(ValueError):
    """Two different fills under one id, or a fill in a pair the book was not given. Never papered over."""


@dataclass(frozen=True, slots=True)
class PnL:
    """Profit and loss of one sleeve (or the desk) in one pair.

    `realised` and `unrealised` are at the pair's price scale. `fees_micro` and `net_micro` are in millionths of
    the pair's quote currency, and `net_micro` is net of quote-currency fees only. A fee charged in any other
    currency, as a broker charges forex commission in the account's base currency, is not converted and is NOT
    in `net_micro`: it is in `fees_not_netted_micro`, in millionths of each currency. While that is not empty,
    `net_micro` is too high by those amounts.
    """

    realised: int
    unrealised: int
    fees_micro: int
    net_micro: int
    fees_not_netted_micro: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SleeveView:
    """What a sleeve is allowed to know about itself. Read-only, and made from fills."""

    sleeve: str
    positions: Mapping[str, Position]
    realised: Mapping[str, int]
    fees: Mapping[str, int]

    def quantity(self, pair: str) -> int:
        found = self.positions.get(pair)
        return found.quantity if found else 0


class _Line:
    """One sleeve in one pair."""

    __slots__ = ("lots", "position", "realised")

    def __init__(self, pair: str) -> None:
        self.position = Position(pair)
        self.lots: deque[tuple[int, int]] = deque()  # (signed quantity, price), oldest first
        self.realised = 0

    def apply(self, fill: Fill) -> None:
        self.position = self.position.apply(fill)
        signed = fill.signed_quantity
        while signed and self.lots and (self.lots[0][0] > 0) != (signed > 0):
            quantity, price = self.lots[0]
            matched = min(abs(signed), abs(quantity))
            direction = 1 if quantity > 0 else -1
            self.realised += direction * matched * (fill.price - price)
            signed += direction * matched
            if matched == abs(quantity):
                self.lots.popleft()
            else:
                self.lots[0] = (quantity - direction * matched, price)
        if signed:
            self.lots.append((signed, fill.price))

    def unrealised(self, bid: int, ask: int) -> int:
        return sum(quantity * ((bid if quantity > 0 else ask) - price) for quantity, price in self.lots)


class Book:
    def __init__(self, instruments: Iterable[Instrument]) -> None:
        self._instruments = {instrument.pair: instrument for instrument in instruments}
        self._lines: dict[tuple[str, str], _Line] = {}
        self._fills: dict[str, Fill] = {}
        self._fees: dict[str, tuple[int, str]] = {}

    # ---- booking: each fill once, each fee once -----------------------------------------------------------------

    def apply(self, fill: Fill) -> bool:
        """Book a fill. Returns False, and no position changes, if this fill was booked before.

        A fill reported live carries no commission; the same fill listed later by the venue does. So a repeat
        that is the first to say what the fill cost still has that recorded, once.
        """
        known = self._fills.get(fill.fill_id)
        if known is not None:
            same = (known.pair, known.side, known.quantity, known.price, known.client_order_id)
            if same != (fill.pair, fill.side, fill.quantity, fill.price, fill.client_order_id):
                raise BookError(f"two different fills carry the id {fill.fill_id}")
        elif fill.pair not in self._instruments:
            raise BookError(f"the book was not given an instrument for {fill.pair}")
        else:
            self._fills[fill.fill_id] = fill
            sleeve = sleeve_of(fill.client_order_id) or UNATTRIBUTED
            self._lines.setdefault((sleeve, fill.pair), _Line(fill.pair)).apply(fill)
        if fill.commission_currency is not None and fill.fill_id not in self._fees:
            self._fees[fill.fill_id] = (fill.commission, fill.commission_currency)
        return known is None

    def charge(self, event: CommissionEvent) -> None:
        """Record the commission of a fill. The latest report for a fill is its commission; none is added twice."""
        self._fees[event.fill_id] = (event.commission, event.currency)

    def take(self, event: object) -> None:
        """Book a fill or a commission; anything else is not the book's business."""
        if isinstance(event, Fill):
            self.apply(event)
        elif isinstance(event, CommissionEvent):
            self.charge(event)

    # ---- reading ------------------------------------------------------------------------------------------------

    @property
    def instruments(self) -> tuple[Instrument, ...]:
        """What the book was given to book in, one instrument per pair."""
        return tuple(self._instruments.values())

    def has(self, fill_id: str) -> bool:
        """Was a fill with this id booked?"""
        return fill_id in self._fills

    def sleeves(self) -> list[str]:
        return sorted({sleeve for sleeve, _ in self._lines})

    def open_lines(self) -> dict[tuple[str, str], int]:
        """(sleeve, pair) -> signed quantity, for every line that is not flat."""
        return {key: line.position.quantity for key, line in sorted(self._lines.items()) if line.position.quantity}

    def gross(self, sleeve: str, pair: str, bid: int, ask: int) -> int:
        """Realised plus unrealised (long at the bid, short at the ask) for one pair, at its price scale. No fees."""
        return sum(line.realised + line.unrealised(bid, ask) for line in self._selected(sleeve, pair))

    def position(self, sleeve: str, pair: str) -> Position:
        """A sleeve's position in a pair; for `DESK`, the sum over sleeves."""
        if sleeve != DESK:
            line = self._lines.get((sleeve, pair))
            return line.position if line else Position(pair)
        mine = [line.position for (_, p), line in self._lines.items() if p == pair]
        return Position(pair, sum(p.quantity for p in mine), sum(p.quote_cash for p in mine))

    def _selected(self, sleeve: str, pair: str) -> list[_Line]:
        return [line for (s, p), line in self._lines.items() if p == pair and sleeve in (DESK, s)]

    def fees(self, sleeve: str = DESK, pair: str | None = None) -> dict[str, int]:
        """Commissions by currency, in millionths. A fee whose fill has not arrived yet counts for the desk only."""
        out: dict[str, int] = {}
        for fill_id, (amount, currency) in self._fees.items():
            fill = self._fills.get(fill_id)
            owner = (sleeve_of(fill.client_order_id) or UNATTRIBUTED) if fill else None
            if (sleeve == DESK or owner == sleeve) and (pair is None or (fill is not None and fill.pair == pair)):
                out[currency] = out.get(currency, 0) + amount
        return out

    def pnl(self, sleeve: str, pair: str, bid: int, ask: int) -> PnL:
        """Realised, unrealised (long at the bid, short at the ask), fees and the net, for one pair.

        The net takes off the fees charged in the pair's quote currency. Fees in other currencies are returned
        beside it, not converted and not taken off (see `PnL`).
        """
        lines = self._selected(sleeve, pair)
        instrument = self._instruments[pair]
        realised = sum(line.realised for line in lines)
        unrealised = sum(line.unrealised(bid, ask) for line in lines)
        charged = self.fees(sleeve, pair)
        fees = charged.pop(instrument.quote, 0)
        others = MappingProxyType({currency: amount for currency, amount in sorted(charged.items()) if amount})
        to_micro = 10 ** (CASH_SCALE - instrument.price_scale)
        return PnL(realised, unrealised, fees, (realised + unrealised) * to_micro - fees, others)

    def view(self, sleeve: str) -> SleeveView:
        mine = {pair: line for (owner, pair), line in self._lines.items() if owner == sleeve}
        return SleeveView(
            sleeve,
            MappingProxyType({pair: line.position for pair, line in mine.items()}),
            MappingProxyType({pair: line.realised for pair, line in mine.items()}),
            MappingProxyType(self.fees(sleeve)),
        )

    def snapshot(self) -> dict[str, object]:
        """Everything the book holds, as plain values: equal snapshots mean equal books."""
        return {
            "lines": {
                f"{sleeve}/{pair}": (line.position.quantity, line.position.quote_cash, line.realised, tuple(line.lots))
                for (sleeve, pair), line in sorted(self._lines.items())
            },
            "fees": dict(sorted(self._fees.items())),
            "fills": sorted(self._fills),
        }
