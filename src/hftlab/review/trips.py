"""Round trips, read out of a shadow journal.

A round trip of a sleeve in a pair is two fills: one that takes a position from flat, and the next one, of the
same quantity the other way, that brings it back to flat. Its result is what the two fills made, less the
commissions charged on them, in millionths of the pair's quote currency, exactly.

Anything else is not measured and is counted instead: a position that came back to flat in more than two
fills, or by another quantity, is `irregular`, and a position still open when the journal ends is `open`. The
sleeves there are trade one fill-or-kill order in and one out, so both are expected to be zero; a number
above zero says the journal holds something this module does not claim to understand.

Nothing here reads a file or prices anything. It is handed the journal's entries, as `hftlab.journal.read`
returns them once the chain has verified.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from hftlab.core import CASH_SCALE, Instrument, Side

FILL, COMMISSION = "fill", "commission"


class TripsError(ValueError):
    """A journal entry that is not what the shadow runner writes, or a fee that cannot be netted."""


@dataclass(frozen=True, slots=True)
class Leg:
    """One fill of a round trip, as the journal holds it."""

    fill_id: str
    side: Side
    quantity: int
    price: int
    venue_ts_ns: int  # the shadow venue's time of the execution: when the order met the book
    n: int  # the number of the event the fill was journalled on


@dataclass(frozen=True, slots=True)
class Trip:
    pair: str
    entry: Leg
    exit: Leg
    fees_micro: int  # both commissions, in the pair's quote currency

    @property
    def direction(self) -> int:
        """+1 for a trip that bought first, -1 for one that sold first."""
        return self.entry.side.sign

    @property
    def quantity(self) -> int:
        return self.entry.quantity

    def result_micro(self, instrument: Instrument) -> int:
        """What the two fills made, less both commissions, in millionths of the quote currency."""
        scale = int(10 ** (CASH_SCALE - instrument.price_scale))
        return self.direction * (self.exit.price - self.entry.price) * self.quantity * scale - self.fees_micro


@dataclass(slots=True)
class Found:
    """What one sleeve did in one pair."""

    trips: list[Trip] = field(default_factory=list)
    irregular: int = 0  # times the position came back to flat otherwise than by two matching fills
    open_quantity: int = 0  # what is still held when the journal ends: reported, and not a round trip


def _whole(entry: Mapping[str, object], key: str) -> int:
    value = entry.get(key)
    if type(value) is not int:
        raise TripsError(f"journal line {entry.get('i')!r} has no whole number for {key}")
    return value


def _leg(entry: Mapping[str, object]) -> Leg:
    try:
        side = Side(entry.get("side"))
    except ValueError as exc:
        raise TripsError(f"journal line {entry.get('i')!r} is a fill without a side") from exc
    return Leg(
        str(entry.get("fill_id")), side, _whole(entry, "quantity"), _whole(entry, "price"),
        _whole(entry, "venue_ts_ns"), _whole(entry, "n"),
    )  # fmt: skip


def round_trips(
    entries: Iterable[Mapping[str, object]], sleeve: str, instruments: Mapping[str, Instrument]
) -> dict[str, Found]:
    """Every round trip of `sleeve`, by pair, in the order they closed. A pair it never traded is not listed."""
    fees: dict[str, int] = {}
    charged_in: dict[str, str] = {}
    fills: list[tuple[str, Leg]] = []
    for entry in entries:
        if entry.get("type") == COMMISSION:
            fill_id, currency = str(entry.get("fill_id")), str(entry.get("currency"))
            if charged_in.setdefault(fill_id, currency) != currency:
                raise TripsError(f"fill {fill_id} was charged in two currencies")
            fees[fill_id] = fees.get(fill_id, 0) + _whole(entry, "commission")
        elif entry.get("type") == FILL and entry.get("sleeve") == sleeve:
            fills.append((str(entry.get("pair")), _leg(entry)))
    found: dict[str, Found] = {}
    since_flat: dict[str, list[Leg]] = {}
    for pair, leg in fills:
        if pair not in instruments:
            raise TripsError(f"the journal holds a fill in {pair}, which it does not describe")
        mine, legs = found.setdefault(pair, Found()), since_flat.setdefault(pair, [])
        legs.append(leg)
        mine.open_quantity += leg.side.sign * leg.quantity
        if mine.open_quantity != 0:
            continue
        since_flat[pair] = []
        if len(legs) != 2 or legs[0].quantity != legs[1].quantity:
            mine.irregular += 1
            continue
        for one in legs:
            if charged_in.get(one.fill_id, instruments[pair].quote) != instruments[pair].quote:
                raise TripsError(f"fill {one.fill_id} was charged in a currency that cannot be netted in {pair}")
        mine.trips.append(Trip(pair, legs[0], legs[1], sum(fees.get(one.fill_id, 0) for one in legs)))
    return found
