"""Three small, strict parsers: a tick file's lines into rows of text fields, and nothing more (ADR 0028).

Each layout is described by its columns only.

  generic         any delimiter, any column order, with or without a header line, with or without sizes; all of
                  it stated in the description file, which also states the time format
  utc_ask_bid     comma-separated, five columns in this order: time, ask, bid, ask size, bid size. The time is
                  UTC, written day.month.year hour:minute:second.fraction
  local_bid_ask   comma-separated, three columns in this order, or four: time, bid, ask, and a fourth that is
                  not a size on display and is not read. The time is a wall clock in the stated zone, written
                  yearmonthday hourminutesecond and three digits of milliseconds, with one space in the middle

A parser splits and checks the shape. It reads no price and no time: those are read once, in one place, for
all three (`hftlab.history.rows`). A line that does not have the shape is not skipped: `Refused` ends the file,
because a description that is wrong for one line cannot be trusted for the others.

Files are plain ASCII, one row a line. A UTF-8 byte-order mark before the first line is stepped over; a quote
character, an empty line or any other byte outside ASCII is refused.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Final, NamedTuple

from hftlab.history.describe import GENERIC, LOCAL_BID_ASK, UTC_ASK_BID, Description

_BOM: Final = b"\xef\xbb\xbf"


class Refused(ValueError):
    """The file is not what its description says. Nothing of it is imported."""

    def __init__(self, line: int, why: str) -> None:
        super().__init__(f"line {line}: {why}")
        self.line = line


class Raw(NamedTuple):
    """One row as the file wrote it. The sizes are None for a layout that has none."""

    line: int  # counted from 1, the header included
    time: str
    bid: str
    ask: str
    bid_size: str | None = None
    ask_size: str | None = None


@dataclass(frozen=True, slots=True)
class Parser:
    name: str
    version: int
    time_format: str  # empty for the generic layout, whose format is the description's
    rows: Callable[[Iterable[bytes], Description], Iterator[Raw]]


def fields(lines: Iterable[bytes], delimiter: str, header: bool) -> Iterator[tuple[int, list[str]]]:
    """Each data line as (its number, its fields). The header line, when there is one, is number 1 and is first."""
    number = 0
    for number, data in enumerate(lines, 1):
        if number == 1 and data.startswith(_BOM):
            data = data[len(_BOM) :]
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            raise Refused(number, "a byte that is not ASCII") from None
        text = text.removesuffix("\n").removesuffix("\r")
        if not text or '"' in text or "\r" in text:
            raise Refused(number, "an empty line, a quote character or a stray carriage return")
        yield number, text.split(delimiter)
    if number == 0 or (header and number == 1):
        raise Refused(max(number, 1), "the file holds no row")


def generic(lines: Iterable[bytes], description: Description) -> Iterator[Raw]:
    """Rows of a file whose columns the description names, by header name or by position."""
    columns = dict(description.columns or {})
    stream = fields(lines, description.delimiter, description.header)
    width: int | None = None
    if description.header:
        _, names = next(stream)
        if len(set(names)) != len(names):
            raise Refused(1, "the header names one column twice")
        if absent := sorted(str(name) for name in columns.values() if name not in names):
            raise Refused(1, f"the header has no column {', '.join(absent)}")
        where = {key: names.index(str(name)) for key, name in columns.items()}
        width = len(names)
    else:
        where = {key: int(position) - 1 for key, position in columns.items()}
    sized, needed = "bid_size" in where, max(where.values()) + 1
    for number, row in stream:
        if len(row) < needed or (width is not None and len(row) != width):
            raise Refused(number, f"{len(row)} fields, where the description needs {width or needed}")
        sizes = (row[where["bid_size"]], row[where["ask_size"]]) if sized else (None, None)
        yield Raw(number, row[where["time"]], row[where["bid"]], row[where["ask"]], *sizes)


def utc_ask_bid(lines: Iterable[bytes], description: Description) -> Iterator[Raw]:
    """Rows of: time in UTC, ask, bid, ask size, bid size. Exactly five fields."""
    stream = fields(lines, ",", description.header)
    if description.header:
        number, names = next(stream)
        if len(names) != 5:
            raise Refused(number, f"a header of {len(names)} fields, where this layout has five")
    for number, row in stream:
        if len(row) != 5:
            raise Refused(number, f"{len(row)} fields, where this layout has five")
        when, ask, bid, ask_size, bid_size = row
        yield Raw(number, when, bid, ask, bid_size, ask_size)


def local_bid_ask(lines: Iterable[bytes], description: Description) -> Iterator[Raw]:
    """Rows of: wall-clock time, bid, ask, and perhaps a fourth field that is not read. Three fields or four."""
    stream = fields(lines, ",", description.header)
    width: int | None = None
    if description.header:
        number, names = next(stream)
        if len(names) not in (3, 4):
            raise Refused(number, f"a header of {len(names)} fields, where this layout has three or four")
    for number, row in stream:
        width = len(row) if width is None else width
        if width not in (3, 4) or len(row) != width:
            raise Refused(number, f"{len(row)} fields, where this layout has three or four, the same on every line")
        yield Raw(number, row[0], row[1], row[2])


PARSERS: Final = {
    GENERIC: Parser(GENERIC, 1, "", generic),
    UTC_ASK_BID: Parser(UTC_ASK_BID, 1, "%d.%m.%Y %H:%M:%S.%f", utc_ask_bid),
    LOCAL_BID_ASK: Parser(LOCAL_BID_ASK, 1, "%Y%m%d %H%M%S%f", local_bid_ask),
}
