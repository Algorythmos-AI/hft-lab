"""Cash, worked out from a book: what a set of fills did to each currency (ADR 0031).

A spot fill is a conversion. A buy of 20,000 EUR.USD at 1.08510 adds 20,000 EUR and takes 21,702 USD; a sell
does the reverse; a commission takes its amount in its own currency. So the cash a book's fills have moved is
a sum over its lines, and needs no order of events and no price:

    base currency    + the line's signed quantity
    quote currency   + the line's quote cash (what its fills paid or received)
    fee currency     - the commissions

Everything is in millionths of its currency, exact, and no currency is converted into another.

The float (the cash the account holds in each traded currency so that a cash-only account can go both ways)
is kept in the same book under a name of its own, `FLOAT`: the conversions that fund it are fills like any
other, so the venue's positions still equal the book's. What is a sleeve's and what is the float's is then a
matter of which lines are summed:

    effect(book)            everything the fills in the book moved: the account's cash, less where it began
    effect(book, FLOAT)     what the funding conversions moved: the baseline, less where it began
    effect(book, sleeve)    that sleeve's deviation from the baseline
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from hftlab.book.book import DESK, Book
from hftlab.core import CASH_SCALE

FLOAT: Final = "float"  # the book's name for the funding conversions; no sleeve may be called this
UNIT: Final = 10**CASH_SCALE  # one whole unit of a currency, in millionths


@dataclass(frozen=True, slots=True)
class CashDifference:
    """One currency in which the venue's cash is not what the anchor and the book's fills come to. Millionths."""

    currency: str
    ours: int
    venue: int


def effect(book: Book, scope: str = DESK) -> dict[str, int]:
    """What the fills of `scope` moved, by currency, in millionths: the whole book's for `DESK`."""
    out: dict[str, int] = {}
    for instrument in book.instruments:
        held = book.position(scope, instrument.pair)
        if held.quantity:
            out[instrument.base] = out.get(instrument.base, 0) + held.quantity * UNIT
        if held.quote_cash:
            scaled = held.quote_cash * int(10 ** (CASH_SCALE - instrument.price_scale))
            out[instrument.quote] = out.get(instrument.quote, 0) + scaled
    for currency, amount in book.fees(scope).items():
        out[currency] = out.get(currency, 0) - amount
    return {currency: amount for currency, amount in sorted(out.items()) if amount}


def anchor_of(seen: Mapping[str, int], book: Book) -> dict[str, int]:
    """Where the account's cash began, as far as this book is concerned: what the venue holds now, less everything
    the book's fills moved. From then on the venue's cash should be this plus `effect(book)`, whatever is booked."""
    moved = effect(book)
    return {code: seen.get(code, 0) - moved.get(code, 0) for code in sorted(set(seen) | set(moved))}


def expected(anchor: Mapping[str, int], book: Book, scope: str = DESK) -> dict[str, int]:
    """The cash the anchor and the fills of `scope` come to, by currency."""
    moved = effect(book, scope)
    return {code: anchor.get(code, 0) + moved.get(code, 0) for code in sorted(set(anchor) | set(moved))}


def cash_differences(
    anchor: Mapping[str, int], book: Book, seen: Mapping[str, int], tolerance: int
) -> tuple[CashDifference, ...]:
    """Every currency in which the venue's cash is further than `tolerance` from the anchor plus the book's fills."""
    ours = expected(anchor, book)
    out = []
    for code in sorted(set(ours) | set(seen)):
        mine, venue = ours.get(code, 0), seen.get(code, 0)
        if abs(mine - venue) > tolerance:
            out.append(CashDifference(code, mine, venue))
    return tuple(out)
