"""Reconciliation: what the venue says it holds and executed, against the book (ADR 0003, ADR 0016).

The book is built from the fills this process was shown. The venue knows the fills that happened. On a
schedule the caller chooses, `Reconciler.check()` asks the venue two questions and compares:

  * executions since the last ones seen: any the book does not hold is handed on to be booked, once;
  * positions: a difference that is still there after a grace time is a BREAK.

The grace time exists because the two answers are not taken at one instant: a fill can be in the venue's
position and not yet in its execution list, or the other way round, for a moment. A difference that outlives
it has no innocent explanation left.

Where the account is a cash account with a float (ADR 0031) there is a third question, when the reconciler is
given a `CashCheck`: the cash the venue holds, by currency, against the anchor plus everything the book's
fills moved (`hftlab.book.cash`). A currency further off than the tolerance is a difference like a position's:
it has the same grace time and opens the same break. Until an anchor stands the venue is asked and nothing is
compared.

A break is a flag (`broken`), a `BreakEvent` for whoever must stop entries, and a line in the journal. It stays
set until a later check finds the positions agreeing AND that agreement has been written to the journal, which
is the record an operator reads. It survives a restart: the journal is read when the reconciler is built.

Nothing here can trade: the venue is seen through `Reported`, which has the two questions and nothing else.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from hftlab.book.book import DESK, Book, BookError
from hftlab.book.cash import CashDifference, cash_differences
from hftlab.core import Clock, Fill, InstrumentKind
from hftlab.journal import Journal, read
from hftlab.venues.port import VenueCash, VenueError, VenuePosition

MINUTE: Final = 60 * 1_000_000_000


@dataclass(frozen=True, slots=True)
class Difference:
    """One pair, in one form, where the book's desk position is not the venue's. Signed base-currency units."""

    pair: str
    kind: InstrumentKind
    ours: int
    venue: int


@dataclass(frozen=True, slots=True)
class BreakEvent:
    """The books and the venue disagree about positions (`broken`), or agree again."""

    broken: bool
    ts_ns: int
    differences: tuple[Difference, ...] = ()
    cash: tuple[CashDifference, ...] = ()


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What one check found. `asked` is False when the venue could not be asked; nothing was compared then."""

    at_ns: int
    asked: bool
    recovered: tuple[Fill, ...] = ()
    differences: tuple[Difference, ...] = ()
    broken: bool = False
    event: BreakEvent | None = None
    cash: tuple[CashDifference, ...] = ()  # currencies in which the venue's cash is not the anchor plus the fills
    seen_cash: Mapping[str, int] | None = None  # what the venue said it holds; None when it was not asked


@dataclass(frozen=True, slots=True)
class CashCheck:
    """How the reconciler compares cash: whom to ask, where the account began, and how far off is still agreement.

    `anchor` returns None until an anchor stands (nothing has been funded): the venue is then not asked.
    """

    ask: Callable[[], tuple[VenueCash, ...]]
    anchor: Callable[[], Mapping[str, int] | None]
    tolerance: int  # millionths of each currency


class Reported(Protocol):
    """The two questions reconciliation asks of a venue. There is nothing here that trades."""

    def positions(self) -> tuple[VenuePosition, ...]: ...

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]: ...


def differences(book: Book, reported: Iterable[VenuePosition]) -> tuple[Difference, ...]:
    """Every pair and form in which the venue's position is not the book's desk position; empty when they agree.

    A venue position in a pair the book has no instrument for, or in another form than the book trades the pair
    in, is a difference: the book holds none of it.
    """
    theirs: dict[tuple[str, str], int] = {}
    for position in reported:
        key = (position.pair, position.kind.value)
        theirs[key] = theirs.get(key, 0) + position.quantity
    ours = {(i.pair, i.kind.value): book.position(DESK, i.pair).quantity for i in book.instruments}
    out = []
    for pair, kind in sorted(set(ours) | set(theirs)):
        mine, venue = ours.get((pair, kind), 0), theirs.get((pair, kind), 0)
        if mine != venue:
            out.append(Difference(pair, InstrumentKind(kind), mine, venue))
    return tuple(out)


def _plain(found: Iterable[Difference]) -> list[list[object]]:
    return [[d.pair, d.kind.value, d.ours, d.venue] for d in found]


def _cash(found: Iterable[CashDifference]) -> list[list[object]]:
    return [[d.currency, d.ours, d.venue] for d in found]


class Reconciler:
    """Compares the venue with a book, books what the book missed, and holds the break flag.

    `sink` is where everything it finds goes: a fill the book has not seen, and a `BreakEvent` when the flag
    changes. Left out, fills are booked straight into `book`. Wired to an order manager it is the manager's
    `handle`, so a recovered fill takes the same path as a live one.
    """

    def __init__(
        self,
        source: Reported,
        book: Book,
        journal: Journal,
        *,
        clock: Clock,
        grace_ns: int,
        sink: Callable[[object], object] | None = None,
        overlap_ns: int = 5 * MINUTE,
        cash: CashCheck | None = None,
    ) -> None:
        if grace_ns < 0 or overlap_ns < 0:
            raise ValueError("the grace time and the overlap cannot be negative")
        self._source, self._book, self._journal, self._clock = source, book, journal, clock
        self._grace_ns, self._overlap_ns, self._cash = grace_ns, overlap_ns, cash
        self._last_cash: tuple[CashDifference, ...] = ()
        self._sink = sink if sink is not None else book.take
        self._now = 0
        self._since = 0
        self._broken = False
        self._differing_since: int | None = None
        self._last: tuple[Difference, ...] = ()
        for entry in read(journal.path)[1]:
            kind, at = entry.get("type"), entry.get("at_ns")
            if kind == "break_opened":
                self._broken = True
            elif kind in ("break_cleared", "explained"):
                self._broken, self._differing_since = False, None
            elif kind == "differing" and isinstance(at, int):
                self._differing_since = at
        journal.open()
        if self._broken:
            self._sink(BreakEvent(True, self._tick()))

    @property
    def broken(self) -> bool:
        """The flag the risk gate stops entries on. Set by a break; cleared only by a recorded agreement."""
        return self._broken

    def _tick(self) -> int:
        self._now = max(self._now, self._clock.now_ns())  # a clock set back does not shorten the grace time
        return self._now

    def check(self) -> Reconciliation:
        """Ask the venue, book what was missed, compare positions, and move the break flag if it must move."""
        now = self._tick()
        anchor = self._cash.anchor() if self._cash is not None else None
        seen: dict[str, int] | None = None
        try:
            executed = self._source.executions_since(self._since)
            reported = self._source.positions()
            if self._cash is not None:  # asked even before an anchor stands: it is what a funding is planned from
                seen = {held.currency: held.amount for held in self._cash.ask()}
        except VenueError:
            return Reconciliation(now, False, differences=self._last, broken=self._broken, cash=self._last_cash)
        recovered = tuple(fill for fill in executed if not self._book.has(fill.fill_id) and self._recover(fill, now))
        latest = max((fill.venue_ts_ns or 0 for fill in executed), default=0)
        self._since = max(self._since, latest - self._overlap_ns)
        found = differences(self._book, reported)
        short: tuple[CashDifference, ...] = ()
        if self._cash is not None and anchor is not None and seen is not None:
            short = cash_differences(anchor, self._book, seen, self._cash.tolerance)
        event: BreakEvent | None = None
        if not (found or short):
            if self._broken:
                self._journal.write("break_cleared", at_ns=now)
                self._journal.flush()  # the record an operator reads comes first; only then does the flag clear
                self._broken, event = False, BreakEvent(False, now)
            elif self._differing_since is not None:
                self._journal.write("explained", at_ns=now, since_ns=self._differing_since)
            self._differing_since = None
        else:
            if self._differing_since is None:
                self._differing_since = now
                self._journal.write("differing", at_ns=now, differences=_plain(found), cash=_cash(short))
            if not self._broken and now - self._differing_since >= self._grace_ns:
                since = self._differing_since
                self._journal.write(
                    "break_opened", at_ns=now, since_ns=since, differences=_plain(found), cash=_cash(short)
                )
                self._broken, event = True, BreakEvent(True, now, found, short)
        self._last, self._last_cash = found, short
        if event is not None:
            self._sink(event)  # before the flush: a journal that cannot be written must not keep a break from the gate
        self._journal.flush()
        return Reconciliation(now, True, recovered, found, self._broken, event, short, seen)

    def _recover(self, fill: Fill, now: int) -> bool:
        """Hand on a fill the book has not seen. True when the book holds it afterwards."""
        with contextlib.suppress(BookError):  # recorded below as unbookable; the positions will say the rest
            self._sink(fill)
        booked = self._book.has(fill.fill_id)
        self._journal.write(
            "recovered" if booked else "unbookable", at_ns=now, fill_id=fill.fill_id,
            client_order_id=fill.client_order_id, pair=fill.pair, side=fill.side.value, quantity=fill.quantity,
            price=fill.price,
        )  # fmt: skip
        return booked
