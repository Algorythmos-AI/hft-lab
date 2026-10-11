"""From a file's rows to quotes, under one stated rule for everything that can be wrong with a row (ADR 0028).

Two answers, and which one a fault gets depends on what the fault says about the rest of the file:

  the file is refused    the row is not written the way the description says: the wrong number of fields, a time
                         that does not match the format, a price or a size that is not a plain decimal number.
                         A description that is wrong for one line cannot be trusted for the others, and guessing
                         which lines it fits is not this tool's to do. Nothing of the file is imported.
  the row is dropped     the row is written correctly and cannot be used. It is counted by reason, the count is
                         in the day's journal and in the report, and the import goes on. A row is never repaired.

The reasons a row is dropped, in the order they are tried:

  time_not_one_instant   its wall-clock time is in the hour a clock change repeats or skips: it names two
                         instants or none, and the file does not say which
  time_goes_back         it is earlier than a row before it in the same file. Where it belongs in the stream is
                         not known, and a replay would hand it over at a time it does not carry
  market_closed          the market was shut at its time, by the calendar: a weekend or a holiday. Nothing
                         trades on such a quote, and many of them mean the stated time zone is wrong
  price_unfit            its bid or its ask has more decimals than the instrument's price scale, or is not above
                         zero. Rounding it would invent a price
  crossed                its bid is above its ask. The platform's own recorder keeps such a quote, because it is
                         what its venue sent; here it would let a label buy below the bid of somebody else's feed
  size_unfit             a size times the stated unit is not a whole number of base-currency units, or does not
                         fit the record
  duplicate              it says exactly what the row kept before it said, at the same time

Counted and kept: `locked` (bid equals ask) and `off_increment` (a price that is not a multiple of the
instrument's price increment: the increment is this venue's order grid, not the other feed's).

A silence is not a fault of a row. It is found by the gap tracker the recorder uses, with the same threshold,
and journalled as the recorder journals it.

Where a file has no sizes, every quote is given `PLACEHOLDER_SIZE` on both sides: one base-currency unit. It
is the smallest size that still says "there is a book" (a book showing nothing on either side gives no
feature row), and it is far below any order: the shadow venue fills no more than the size on display, so
against it an order of the minimum size does not fill and a label is "no entry". The day says its sizes are a
placeholder, and a dataset holding such a day says it is not a result.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final, NamedTuple

from hftlab.calendar import Calendar
from hftlab.core import InexactError, Instrument, to_scaled
from hftlab.history.describe import SIZE_SCALE, Description
from hftlab.history.parsers import Parser, Raw, Refused
from hftlab.history.times import TimeError, reader

PLACEHOLDER_SIZE: Final = 1
NOT_ONE_INSTANT, GOES_BACK, CLOSED = "time_not_one_instant", "time_goes_back", "market_closed"
PRICE_UNFIT, CROSSED, SIZE_UNFIT, DUPLICATE = "price_unfit", "crossed", "size_unfit", "duplicate"
DROPPED: Final = (NOT_ONE_INSTANT, GOES_BACK, CLOSED, PRICE_UNFIT, CROSSED, SIZE_UNFIT, DUPLICATE)
LOCKED, OFF_INCREMENT = "locked", "off_increment"
_SIZE_ONE: Final = 10**SIZE_SCALE
_U64: Final = 1 << 64


class Tick(NamedTuple):
    ts_ns: int
    bid: int
    ask: int
    bid_size: int
    ask_size: int
    line: int


@dataclass(slots=True)
class Tally:
    """What reading one file found. `dropped` is by reason, then by UTC day; `counted` is by what was noticed."""

    read: int = 0
    kept: int = 0
    dropped: dict[str, Counter[str]] = field(default_factory=dict)
    counted: Counter[str] = field(default_factory=Counter)
    seen: Counter[str] = field(default_factory=Counter)  # rows read, by UTC day; a row with no instant has no day

    def drop(self, reason: str, day: str) -> None:
        self.dropped.setdefault(reason, Counter())[day] += 1

    def dropped_by_reason(self) -> dict[str, int]:
        return {reason: sum(self.dropped[reason].values()) for reason in DROPPED if reason in self.dropped}


def _price(text: str, instrument: Instrument, line: int) -> int | None:
    """The price as an integer at the instrument's scale; None when it does not fit; `Refused` when it is no number."""
    try:
        price = to_scaled(text, instrument.price_scale)
    except InexactError:
        return None
    except ValueError:
        raise Refused(line, f"{text!r} is not a plain decimal number") from None
    return price if 0 < price < _U64 else None


def _size(text: str, unit_scaled: int, line: int) -> int | None:
    try:
        product = to_scaled(text, SIZE_SCALE) * unit_scaled
    except InexactError:
        return None
    except ValueError:
        raise Refused(line, f"{text!r} is not a plain decimal number") from None
    units, rest = divmod(product, _SIZE_ONE * _SIZE_ONE)
    return units if rest == 0 and 0 <= units < _U64 else None


def ticks(
    raws: Iterable[Raw], instrument: Instrument, description: Description, read_time: Callable[[str], int | None],
    calendar: Calendar, day_of: Callable[[int], str], tally: Tally,
) -> Iterator[Tick]:  # fmt: skip
    """The rows of one file that are kept, as quotes in the file's order. Everything else is counted in `tally`."""
    latest = -1
    before: tuple[int, int, int, int, int] | None = None
    step = instrument.price_increment
    for raw in raws:
        tally.read += 1
        try:
            at = read_time(raw.time)
        except TimeError as exc:
            raise Refused(raw.line, str(exc)) from None
        bid, ask = _price(raw.bid, instrument, raw.line), _price(raw.ask, instrument, raw.line)
        sizes: tuple[int | None, int | None] = (PLACEHOLDER_SIZE, PLACEHOLDER_SIZE)
        if raw.bid_size is not None and raw.ask_size is not None:
            unit = description.size_unit_scaled
            sizes = (_size(raw.bid_size, unit, raw.line), _size(raw.ask_size, unit, raw.line))
        if at is None:
            tally.drop(NOT_ONE_INSTANT, "")
            continue
        day = day_of(at)
        tally.seen[day] += 1
        if at < latest:
            tally.drop(GOES_BACK, day)
            continue
        latest = at
        if not calendar.is_open(at):
            tally.drop(CLOSED, day)
        elif bid is None or ask is None:
            tally.drop(PRICE_UNFIT, day)
        elif bid > ask:
            tally.drop(CROSSED, day)
        elif sizes[0] is None or sizes[1] is None:
            tally.drop(SIZE_UNFIT, day)
        elif (at, bid, ask, *sizes) == before:
            tally.drop(DUPLICATE, day)
        else:
            before = (at, bid, ask, sizes[0], sizes[1])
            tally.kept += 1
            tally.counted[LOCKED] += bid == ask
            tally.counted[OFF_INCREMENT] += bool(bid % step or ask % step)
            yield Tick(at, bid, ask, sizes[0], sizes[1], raw.line)


def read_file(
    lines: Iterable[bytes], parser: Parser, instrument: Instrument, description: Description, calendar: Calendar,
    day_of: Callable[[int], str], tally: Tally,
) -> Iterator[Tick]:  # fmt: skip
    """One file, from its lines to its kept quotes, with the parser and the time format its layout names."""
    read_time = reader(parser.time_format or description.time_format, description.time_zone)
    return ticks(parser.rows(lines, description), instrument, description, read_time, calendar, day_of, tally)
