"""The tape: everything the trader was shown from outside, in the order it was shown it (ADR 0005, ADR 0019).

The trader's decisions are a function of its inputs and the clock. The inputs that cannot be computed again
are recorded here, one JSON line each, at the edge where they enter:

    ["open", at, venue, [instrument, ...]]        the connection, and what the venue says its instruments are
    ["marks", {...}]                              where each journal stood when the session began
    ["step", at, state, [event, ...], beat]       one turn of the loop: the venue's events, and at most one beat;
                                                  with a sixth value, the room on the tapes, on a session's first
                                                  turn and on the turn it changes, where the tapes have a quota
    ["ask", question, state, answer, error]       a question to the venue, and what it said or how it failed
                                                  (open orders, positions, executions, and the account's cash)
    ["sent", verb, client order id, how, state]   how a send or a cancel went, as the order manager saw it
    ["stop", at]                                  the session ended on purpose
    ["dark"]                                      the reserve is used up: the session went on, and wrote no more here

`at` is the time of the turn, taken from the clock once and never read again inside the turn: it is the only
time anything decided on that turn knows. A beat is the part of a turn that happens on a timer: the switches
as they were read, the venue's answer to "are you there", and what an operator asked for.

Live, `LiveEdge` writes the tape. `ReplayEdge` reads one back and stands in for the venue, so the same code
makes the same decisions (hftlab.trader.replay). A tape is a recording of a private account's day: it stays
in the state directory, which git ignores.

On disk a session's tape is a small head and a run of parts, in the folder of its trading day:

    session-001.tape.jsonl                      the head: the `open` and `marks` lines, written once, never again
    session-001.part-0001.2026-10-14.jsonl      the turns, until the UTC day ends or the part is full
    session-001.part-0002.2026-10-15.jsonl.gz   a closed part, compressed by the housekeeping job

  * A part ends, and the next begins, only between two turns: when the UTC day of a turn is not the day of
    the part, or the part has reached its size. So a part holds whole turns, and the lines of all the parts,
    read in order after the head, are the tape exactly as one file would have held it. A replay does not know
    where the parts were cut.
  * A kill at the moment a part is changed loses nothing and writes nothing twice: the old part is closed
    before the new one is made, and the turn's line is written into the new one only then. What a kill can
    leave is an empty last part, which reads as no lines.
  * The tapes are counted against a quota (`tape.quota_bytes` in the trader's settings), like the recorder's
    store: a full disk would take the other desks on the host down with it. The quota is never a reason to
    stop watching a position (ADR 0027, addendum). There are two limits under it, and each turn's line says
    when the room changes (`hftlab.risk.room`), so a replay sees the same room at the same turn:
      - at the SOFT limit the trader opens nothing more, and goes on recording and closing;
      - at the HARD limit, the quota less a reserve that no entry can reach, it closes what it holds. From
        there the tape is thinned so that the reserve lasts: a turn is taken on a beat and on nothing else,
        and of a pair's quotes since the last turn only the newest is kept. What was dropped was never shown
        to the trader, so the tape is still everything it decided on;
      - a line that does not fit under the quota is not written, and nothing after it: the tape ends with
        `["dark"]`, whose room is held back from every line before it. The session goes on, unrecorded, and
        its replay is the same as far as the tape goes. A new session's head is written whatever the tapes
        hold: a start is never refused for want of room, and the head and that mark are the only lines that
        can pass the quota.
  * Closed parts are compressed by `python -m hftlab.trader --housekeep`, which the hourly housekeeping unit
    runs before its own work. A part is closed when it is not the last part of the last session of the
    newest day, or ends with its session's `stop` or `dark`. Nothing is ever deleted here.

What a kill and what a power cut can lose. Every line is handed to the operating system as it is written, so
a kill of the process loses no line. The disk is another matter:

  * the head, and every part that has been closed, are synced before the next file is opened, and with
    `fsync` on the name of each new file is synced into its folder: a power cut loses none of them;
  * with `fsync` on, the part being written is synced on every beat. A power cut can lose the lines since
    the last beat, at most one beat's worth of turns (a second as shipped) and the turn in progress, and
    nothing before them. With `fsync` off it can lose everything in the part being written;
  * recovery never reads the tape: a restart goes from the journals and asks the broker. What the lost lines
    cost is the replay of that one session, which then ends up to a beat before the decision journal does.

Neither edge can send an order. The live one holds the venue's questions and its market data; the venue itself
goes to the order manager, which is the only caller of its order methods.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final, Protocol

from hftlab.core import (
    Clock,
    Fill,
    Instrument,
    InstrumentKind,
    Liquidity,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Quote,
    Side,
    TimeInForce,
)
from hftlab.exec.manager import FAILED, LOCKED, REFUSED, SENT, UNCERTAIN, Asked
from hftlab.record.format import describe, instrument_of
from hftlab.record.store import compress, day_of
from hftlab.risk.room import HARD, ROOM, SOFT
from hftlab.safety.lock import PaperOnlyError
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.port import (
    Capabilities,
    CommissionEvent,
    ConnectionEvent,
    ConnectionState,
    NotReady,
    OpenOrder,
    OrderRefused,
    Unsupported,
    Venue,
    VenueCash,
    VenueError,
    VenueEvent,
    VenueNotice,
    VenuePosition,
    VenueUnavailable,
)
from hftlab.venues.view import QuoteSource

MS: Final = 1_000_000
URGENT_NS: Final = 250 * MS  # while a connection is being made safe, a turn is taken at least this often
UP: Final = (ConnectionState.RECONCILING, ConnectionState.READY)
REQUESTS: Final = "requests"  # in the trader's state directory: one small file per thing an operator asked for
HEAD: Final = ".tape.jsonl"  # what a session's head is called after its number; the session counts these
PART_BYTES: Final = 64_000_000  # ENGINEERING DEFAULT: a part is closed at the first turn after it has grown to this
_PART: Final = re.compile(r"\.part-(?P<number>[0-9]{4,})\.(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl(?P<gz>\.gz)?")
_TAIL: Final = 1 << 12  # more than a `stop` line: where the end of a part is looked at
_CHUNK: Final = 1 << 20  # a tape is read for its hash this much at a time, never whole
DARK: Final = b'["dark"]\n'  # the last line of a tape that ran out of reserve; its room is held back for it
ENDS: Final = (b'["stop",', b'["dark"]')  # how the last line of a session that will write no more begins

Row = list[Any]


class TapeError(Exception):
    """The tape cannot be read, or a replay asked for something the recording does not hold at that point."""


class TapeEnded(BaseException):
    """The recording stops here: the process that wrote it was gone. Nothing may catch this and carry on."""


@dataclass(frozen=True, slots=True)
class Request:
    """Something an operator asked the running trader to do: `reset` a latch, or set a sleeve's `stage` lower."""

    what: str
    subject: str
    value: str
    operator: str


@dataclass(frozen=True, slots=True)
class Beat:
    switches: frozenset[str] | None  # None: the switches could not be read
    server_time_ns: int | None  # None: the venue did not answer, or there is no connection to ask
    requests: tuple[Request, ...] = ()


@dataclass(frozen=True, slots=True)
class Step:
    at_ns: int
    state: ConnectionState
    events: tuple[VenueEvent, ...]
    beat: Beat | None
    room: int | None = None  # the room on the tapes (hftlab.risk.room), when the turn's line says it; None: as it was


@dataclass(frozen=True, slots=True)
class Opened:
    at_ns: int
    venue: str
    instruments: tuple[Instrument, ...]
    state: ConnectionState  # RECONCILING on a new connection; READY when the connection outlived a session


class Edge(Protocol):
    """Where the outside comes in. The trader reads nothing of the world but through one of these."""

    @property
    def venue(self) -> Venue | None:
        """What the order manager sends on; None when this run can send no order at all."""
        ...

    @property
    def asks(self) -> Asked: ...

    def open(self) -> Opened: ...

    def begin(self, path: Path, marks: Mapping[str, object]) -> None: ...

    def next(self, *, urgent: bool) -> Step | None:
        """The next turn; None when the session is to end."""
        ...

    def witness(self, verb: str, client_order_id: str, outcome: str) -> None: ...

    def stop(self, at_ns: int) -> None: ...

    def room(self) -> Mapping[str, int | None] | None:
        """What the tapes hold against their limits, for the status file; None where nothing is counted."""
        ...


# ---- events as rows, and back ------------------------------------------------------------------------------------


def encode(event: VenueEvent) -> Row | None:
    """One venue event as a row of integers and text; None for an event the trader makes no use of."""
    if isinstance(event, Quote):
        return ["q", event.pair, event.bid, event.ask, event.bid_size, event.ask_size, event.recv_ts_ns, event.seq]
    if isinstance(event, ConnectionEvent):
        return ["c", event.state.value, event.ts_ns, event.seq, event.reason]
    if isinstance(event, OrderEvent):
        return [
            "o", event.client_order_id, event.state.value, event.ts_ns, event.seq, event.filled_quantity,
            event.broker_order_id, event.reason,
        ]  # fmt: skip
    if isinstance(event, Fill):
        return ["f", *_fill_row(event)]
    if isinstance(event, CommissionEvent):
        return ["m", event.fill_id, event.commission, event.currency, event.ts_ns, event.seq]
    if isinstance(event, VenueNotice):
        return ["n", event.code, event.message, event.ts_ns, event.seq, event.client_order_id]
    return None


def decode(row: Row) -> VenueEvent:
    kind, rest = row[0], row[1:]
    if kind == "q":
        return Quote(*rest)
    if kind == "c":
        return ConnectionEvent(ConnectionState(rest[0]), rest[1], rest[2], rest[3])
    if kind == "o":
        return OrderEvent(rest[0], OrderState(rest[1]), rest[2], rest[3], rest[4], rest[5], rest[6])
    if kind == "f":
        return _fill_of(rest)
    if kind == "m":
        return CommissionEvent(*rest)
    if kind == "n":
        return VenueNotice(*rest)
    raise TapeError(f"the tape holds an event of a kind nobody wrote: {kind!r}")


def _fill_row(fill: Fill) -> Row:
    return [
        fill.fill_id, fill.client_order_id, fill.pair, fill.side.value, fill.quantity, fill.price, fill.ts_ns,
        fill.liquidity.value, fill.commission, fill.commission_currency, fill.venue_ts_ns, fill.seq,
    ]  # fmt: skip


def _fill_of(row: Row) -> Fill:
    return Fill(
        row[0], row[1], row[2], Side(row[3]), row[4], row[5], row[6], Liquidity(row[7]), row[8], row[9], row[10],
        row[11],
    )  # fmt: skip


def _open_order_row(found: OpenOrder) -> Row:
    return [
        found.client_order_id, found.broker_order_id, found.pair, found.side.value, found.quantity,
        found.filled_quantity, found.order_type.value, found.limit_price, found.tif.value, found.state.value,
    ]  # fmt: skip


def _open_order_of(row: Row) -> OpenOrder:
    return OpenOrder(
        row[0], row[1], row[2], Side(row[3]), row[4], row[5], OrderType(row[6]), row[7], TimeInForce(row[8]),
        OrderState(row[9]),
    )  # fmt: skip


def _beat_row(beat: Beat | None) -> Row | None:
    if beat is None:
        return None
    switches = None if beat.switches is None else sorted(beat.switches)
    return [switches, beat.server_time_ns, [[r.what, r.subject, r.value, r.operator] for r in beat.requests]]


def _beat_of(row: Row | None) -> Beat | None:
    if row is None:
        return None
    switches = None if row[0] is None else frozenset(row[0])
    return Beat(switches, row[1], tuple(Request(*request) for request in row[2]))


_ANSWERS: Final[dict[str, tuple[Callable[[Any], Row], Callable[[Row], Any]]]] = {
    "open_orders": (_open_order_row, _open_order_of),
    "positions": (
        lambda p: [p.pair, p.kind.value, p.quantity],
        lambda row: VenuePosition(row[0], InstrumentKind(row[1]), row[2]),
    ),
    "executions_since": (_fill_row, _fill_of),
    "cash": (lambda held: [held.currency, held.amount], lambda row: VenueCash(row[0], row[1])),
}
_ERRORS: Final[dict[str, type[VenueError]]] = {
    "VenueUnavailable": VenueUnavailable, "NotReady": NotReady, "Unsupported": Unsupported,
}  # fmt: skip


# ---- the files of a tape: a head, and parts by UTC day and by size ---------------------------------------------------


def _stem(head: Path) -> str:
    return head.name.removesuffix(HEAD)


def part_path(head: Path, number: int, day: str) -> Path:
    """Where part `number` of the session with this head goes: numbered through the session, named by its UTC day."""
    return head.with_name(f"{_stem(head)}.part-{number:04d}.{day}.jsonl")


def _listed(head: Path) -> dict[int, Path]:
    """The parts beside a head, by number. Where a part is there both plain and compressed, the plain one is taken:
    the housekeeping job was stopped between writing the one and removing the other, and they hold the same."""
    found: dict[int, Path] = {}
    stem = _stem(head)
    for path in sorted(head.parent.glob(f"{stem}.part-*")):
        match = _PART.fullmatch(path.name.removeprefix(stem))
        if match and (int(match["number"]) not in found or not match["gz"]):
            found[int(match["number"])] = path
    return found


def parts(head: Path) -> list[Path]:
    """The parts of a session in the order they were written. A part that is missing from the run is an error."""
    found = _listed(head)
    if sorted(found) != list(range(1, len(found) + 1)):
        raise TapeError(f"{head.name}: its parts are numbered {sorted(found)}; one is missing, and the tape has a hole")
    return [found[number] for number in sorted(found)]


def heads(day: Path) -> list[Path]:
    """The heads of a trading day's sessions, oldest first."""
    return sorted(day.glob(f"session-*{HEAD}"))


def last_head(days: Path) -> Path | None:
    """The head of the session that began last: the highest number in the newest day folder that holds one."""
    folders = sorted((path for path in days.iterdir() if path.is_dir()), reverse=True) if days.is_dir() else []
    for folder in folders:
        found = heads(folder)
        if found:
            return found[-1]
    return None


def ending(head: Path) -> list[object]:
    """Where a session's tape ends: its parts, its bytes, and the SHA-256 of them. `[.., .., ""]`: nowhere sure.

    The bytes are the head's and then each part's in order, as one file would have held them, so the answer
    is the same before and after the housekeeping job compresses a part, and wherever the parts were cut.
    Nothing is parsed. Two answers that are equal and carry a hash are the same tape, line for line: no part
    was added, taken away, written to or written again between them.

    The hash is "" when the tape cannot stand for anything: there is no head, a part is missing from the run,
    a file cannot be read or does not unpack, or one of them does not end with the end of a line (a line that
    was being written, or a part that a kill left empty).

    Every byte of the session's tape is read, the compressed parts unpacked: seconds for a day at the size
    the shipped settings allow. That is paid once when a session ends on purpose and once at the next start.
    """
    for _ in range(2):  # the housekeeping job may replace a part with its compressed copy between the two looks
        with contextlib.suppress(FileNotFoundError):
            return _ending(head)
    return [0, 0, ""]


def _ending(head: Path) -> list[object]:
    digest, size, count = hashlib.sha256(), 0, 0
    try:
        files = [head, *parts(head)]
        count = len(files) - 1
        for file in files:
            last = b""
            with gzip.open(file, "rb") if file.suffix == ".gz" else file.open("rb") as stream:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    last = chunk[-1:]
            if last != b"\n":
                return [count, size, ""]
    except FileNotFoundError:
        raise
    except (TapeError, OSError, EOFError, zlib.error):
        return [count, size, ""]
    return [count, size, digest.hexdigest()]


def tape_bytes(days: Path) -> int:
    """The bytes of every tape file under the folder of day folders: what is counted against the quota."""
    total = 0
    for path in days.glob("*/session-*"):
        with contextlib.suppress(FileNotFoundError):  # housekeeping replaces a part with its compressed copy
            total += path.stat().st_size
    return total


def _sync_folder(folder: Path) -> None:
    """Make the names in a folder durable: a file whose name was never synced can vanish in a power cut."""
    handle = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _ends_stopped(part: Path) -> bool:
    with part.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - _TAIL))
        tail = stream.read()
    return tail.endswith(b"\n") and tail[:-1].rpartition(b"\n")[2].startswith(ENDS)


def being_written(days: Path) -> Path | None:
    """The one part a running trader may still append to, or None.

    It is the last part of the last session of the newest day, unless it is compressed already or ends with
    its session's `stop` or `dark`. Every other part is closed: a new session writes new files and never an
    old one.
    """
    folders = sorted(path for path in days.iterdir() if path.is_dir()) if days.is_dir() else []
    last_heads = heads(folders[-1]) if folders else []
    found = _listed(last_heads[-1]) if last_heads else {}
    if not found:
        return None
    last = found[max(found)]
    return None if last.suffix == ".gz" or _ends_stopped(last) else last


def housekeep(days: Path) -> int:
    """Compress every closed part that is not compressed yet. Returns how many. Nothing is deleted but the
    plain copy of a part whose compressed copy has been read back and found the same."""
    open_now = being_written(days)
    done = 0
    for path in sorted(days.glob("*/session-*.part-*.jsonl")):
        if path != open_now:
            compress(path)
            done += 1
    return done


# ---- live: the venue, the clock and the switch files, written down as they are read ------------------------------


def leave_request(folder: Path, what: str, subject: str, value: str, operator: str) -> Path:
    """Leave a request for the running trader: written whole, then given the name it is looked for under."""
    folder.mkdir(parents=True, exist_ok=True)
    number = len(list(folder.glob("*.json"))) + 1
    partial = folder / f"{number:04d}-{what}.partial"
    partial.write_text(json.dumps({"what": what, "subject": subject, "value": value, "operator": operator}) + "\n")
    return partial.rename(partial.with_suffix(".json"))


def read_requests(folder: Path) -> tuple[Request, ...]:
    """Take every request file out of `folder`, oldest name first. A file that is not a request is left alone."""
    found = []
    try:
        names = sorted(path for path in folder.iterdir() if path.suffix == ".json")
    except OSError:
        return ()
    for path in names:
        try:
            said = json.loads(path.read_text(encoding="utf-8"))
            request = Request(*(str(said[key]) for key in ("what", "subject", "value", "operator")))
            path.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found.append(request)
    return tuple(found)


class _Recorded:
    """The venue's three questions, asked of the venue and written to the tape with what it said."""

    def __init__(self, venue: Asked, edge: LiveEdge) -> None:
        self._venue, self._edge = venue, edge

    def _ask(self, question: str, answer: Callable[[], tuple[Any, ...]]) -> tuple[Any, ...]:
        try:
            found = answer()
        except VenueError as failure:
            self._edge.write(["ask", question, self._edge.state.value, None, type(failure).__name__])
            raise
        self._edge.write(["ask", question, self._edge.state.value, [_ANSWERS[question][0](item) for item in found]])
        return found

    def open_orders(self) -> tuple[OpenOrder, ...]:
        return self._ask("open_orders", self._venue.open_orders)

    def positions(self) -> tuple[VenuePosition, ...]:
        return self._ask("positions", self._venue.positions)

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        return self._ask("executions_since", lambda: self._venue.executions_since(ts_ns))

    def cash(self) -> tuple[VenueCash, ...]:
        return self._ask("cash", self._venue.cash)


class LiveEdge:
    """The venue as it is, the clock as it runs and the switches as they are, each written down as it is read.

    `source` is where the events come from; `venue` is the same venue with its order side, or None for a run
    that can send nothing (`source` is then an order-free view, and there is nobody to ask and nothing to
    witness).
    """

    def __init__(
        self,
        source: QuoteSource,
        clock: Clock,
        *,
        pairs: Sequence[tuple[str, InstrumentKind]],
        switches: Callable[[], frozenset[str]],
        requests: Path,
        should_stop: Callable[[], bool],
        venue: Venue | None = None,
        poll_ns: int = 50 * MS,
        beat_ns: int = 1_000 * MS,
        part_bytes: int = PART_BYTES,
        quota_bytes: int | None = None,
        soft_bytes: int | None = None,
        reserve_bytes: int = 0,
        fsync: bool = False,
    ) -> None:
        if venue is not None and not isinstance(venue, GuardedVenue):
            raise TypeError("the trader sends orders through a guarded venue and through nothing else")
        hard = None if quota_bytes is None else quota_bytes - reserve_bytes
        if hard is None and (soft_bytes is not None or reserve_bytes):
            raise ValueError("a soft limit and a reserve are parts of a quota: there is none without one")
        if hard is not None and reserve_bytes <= 0:
            raise ValueError("a quota without a reserve would stop the tape with nothing closed: it needs one")
        if hard is not None and (hard <= 0 or (soft_bytes is not None and not 0 < soft_bytes <= hard)):
            raise ValueError("the soft limit lies under the hard limit, which is the quota less the reserve")
        self._source, self._clock, self._pairs, self._venue = source, clock, tuple(pairs), venue
        self._switches, self._requests, self._should_stop = switches, requests, should_stop
        self._poll_ns, self._beat_ns = poll_ns, beat_ns
        self._part_bytes, self._quota, self._fsync = part_bytes, quota_bytes, fsync
        self._asks = _Recorded(venue, self) if venue is not None else None
        self._stream: IO[bytes] | None = None
        self._file: Path | None = None  # the head of the session being taped; None between sessions
        self._number, self._day, self._size, self._used = 0, "", 0, 0
        self._soft, self._hard = soft_bytes, hard
        self._days: Path | None = None  # the folder of day folders: what is counted against the quota
        # The room the session in hand was last told of, on a turn's line. None: not yet, so its first turn says.
        self._told: int | None = ROOM
        self._pending: list[VenueEvent] = []  # at the hard limit: what came in since the last turn, thinned
        self.dark = False  # the reserve is used up: this session's tape has its last line, and the session goes on
        self._head: list[Row] = []
        self._last = 0
        self._beat_at = 0
        self._state = ConnectionState.DISCONNECTED

    @property
    def venue(self) -> Venue | None:
        return self._venue

    @property
    def asks(self) -> Asked:
        if self._asks is None:
            raise TapeError("this run holds no venue that could be asked about orders")
        return self._asks

    @property
    def state(self) -> ConnectionState:
        return self._source.state

    def open(self) -> Opened:
        """Connect, look the pairs up and subscribe. The paper-only lock is passed here, or this raises."""
        self._source.connect()
        instruments = tuple(self._source.instrument(pair, kind) for pair, kind in self._pairs)
        for instrument in instruments:
            self._source.subscribe_quotes(instrument)
        self._last = self._beat_at = self._clock.now_ns()
        self._state = self._source.state
        described = [describe(instrument) for instrument in instruments]
        self._head = [["open", self._last, self._source.name, described, self._state.value]]
        return Opened(self._last, self._source.name, instruments, self._state)

    @property
    def level(self) -> int:
        """The room on the tapes as they were last counted: ROOM, SOFT or HARD (hftlab.risk.room)."""
        return self._level_at(self._used)

    def _level_at(self, used: int) -> int:
        if self._hard is None or used < (self._hard if self._soft is None else self._soft):
            return ROOM
        return HARD if used >= self._hard else SOFT

    def room(self) -> dict[str, int | None] | None:
        if self._quota is None:
            return None
        return {
            "used_bytes": self._used, "soft_bytes": self._soft, "hard_bytes": self._hard, "quota_bytes": self._quota,
            "level": self.level, "dark": int(self.dark),
        }  # fmt: skip

    def measure(self, days: Path) -> int:
        """Count the tapes under `days` again, and say what room that leaves. Housekeeping makes room from outside."""
        self._days = days
        self._used = tape_bytes(days)
        return self.level

    def wait_for_room(self) -> bool:
        """Between two sessions, with the book flat and the tapes at their hard limit: wait until they are under it.

        Nothing is decided and nothing is written. The venue is still polled, so that what it sends does not
        pile up; the tapes are counted again on every beat. True when there is room for a session; False when
        the process was told to stop first.
        """
        while self._days is not None and not self._should_stop():
            self._source.poll(self._poll_ns)
            now = self._clock.now_ns()
            if now - self._beat_at >= self._beat_ns:
                self._beat_at = now
                if self.measure(self._days) != HARD:
                    return True
        return False

    def begin(self, path: Path, marks: Mapping[str, object]) -> None:
        """Write the head of a new session's tape. It is written whatever the tapes hold: a start is never refused
        for want of room, because a session that does not begin watches nothing (ADR 0027, addendum)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.measure(path.parent.parent)
        self._stream = path.open("xb")  # a tape is written once; a name that is taken is a mistake, not a retry
        self._file, self._number, self._day, self._size = path, 0, "", 0
        self.dark, self._told, self._pending = False, ROOM if self._hard is None else None, []
        for row in (*self._head, ["marks", dict(marks)]):
            self._put(row, _line(row))
        self._shut_part()  # the head is on the disk, and is never written again; the turns go into parts
        if self._fsync:
            _sync_folder(path.parent)

    def write(self, row: Row) -> None:
        """One line, handed to the operating system at once: a kill of this process loses none of it.

        A line that would not leave room under the quota for the tape's last word is not written, and the last
        word is: `["dark"]`. Nothing is written after it.
        """
        if self._file is None:
            raise TapeError("the tape is not open")
        if self.dark:
            return
        line = _line(row)
        if self._quota is not None and self._used + len(line) + len(DARK) > self._quota:
            self.dark = True
            self._put(["dark"], DARK)
            self._shut_part()  # it is closed for good, so the housekeeping job may compress it and make room
            return
        self._put(row, line)

    def _put(self, row: Row, line: bytes) -> None:
        """A turn's first line is its `step`. Before one is written, the part is changed when the turn's UTC day
        is not the part's or the part is full, so that a part holds whole turns."""
        assert self._file is not None
        timed = row[0] in ("step", "stop") and type(row[1]) is int
        day = day_of(row[1]) if timed else self._day or day_of(self._last)
        if self._number or self._stream is None:  # past the head
            due = row[0] == "step" and (day != self._day or self._size >= self._part_bytes)
            if self._stream is None or due:
                self._shut_part()
                self._open_part(day)
        assert self._stream is not None
        self._stream.write(line)
        self._stream.flush()
        self._size += len(line)
        self._used += len(line)

    def _shut_part(self) -> None:
        """Close the file being written, synced: a closed file of a tape is on the disk."""
        if self._stream is not None:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None

    def _open_part(self, day: str) -> None:
        """Begin the next part. Its name is new, and with `fsync` on it is synced into its folder."""
        assert self._file is not None
        self._number, self._day, self._size = self._number + 1, day, 0
        path = part_path(self._file, self._number, day)
        self._stream = path.open("xb")
        if self._fsync:
            _sync_folder(path.parent)
        if self._quota is not None:
            self._used = tape_bytes(path.parent.parent)  # housekeeping may have made room since the last look

    def next(self, *, urgent: bool) -> Step | None:
        while not self._should_stop():
            events = [event for event in self._source.poll(self._poll_ns) if encode(event) is not None]
            now = max(self._last + 1, self._clock.now_ns())  # a turn has a time of its own, later than the last
            state = self._source.state
            due = now - self._beat_at >= self._beat_ns
            if self.dark and due and self._days is not None and self.measure(self._days) != HARD:
                # Room was made: this session ends here, and the next one is recorded again. Looked at before
                # the beat is read, so that what an operator asked for is left for the session that follows.
                return None
            if self._told == HARD:
                # The reserve has to last: a turn on a beat only, and of each pair's quotes only the newest.
                self._hold(events)
                if not due:
                    continue
                events, self._pending = self._pending, []
            beat = self._beat(state) if due else None
            pressing = urgent and state in UP and now - self._last >= URGENT_NS
            if not (events or beat or pressing or state is not self._state):
                continue
            row: Row = ["step", now, state.value, [encode(event) for event in events], _beat_row(beat)]
            # The room this turn is taken under counts the turn's own line: a turn whose line ends at or past a
            # limit is already under it, so nothing is opened on a line that reaches the soft limit.
            level = ROOM
            if self._hard is not None:
                level = self._level_at(self._used + len(_line(row)))
                if level != self._told:  # the line says so, and is that much longer for it
                    level = self._level_at(self._used + len(_line([*row, level])))
            self._last, self._state = now, state
            if beat is not None:
                self._beat_at = now
            room = None
            if level != self._told:
                room = self._told = level
                row.append(level)
            self.write(row)
            if beat is not None and self._fsync and self._stream is not None:
                os.fsync(self._stream.fileno())  # what a power cut can lose is what was written since this
            return Step(now, state, tuple(events), beat, room)
        return None

    def _hold(self, events: Sequence[VenueEvent]) -> None:
        """Keep what came in for the next turn: everything, but of each pair's quotes only the last one."""
        for event in events:
            if isinstance(event, Quote):
                older = [one for one in self._pending if isinstance(one, Quote) and one.pair == event.pair]
                self._pending = [one for one in self._pending if one not in older]
            self._pending.append(event)

    def _beat(self, state: ConnectionState) -> Beat:
        if self._days is not None and not self.dark and self.level != ROOM:
            self._used = tape_bytes(self._days)  # the housekeeping job may have made room since the last beat
        switched: frozenset[str] | None
        try:
            switched = frozenset(self._switches())
        except OSError:
            switched = None
        answered = None
        if state in UP:
            with contextlib.suppress(VenueError):
                answered = self._source.server_time_ns()
        return Beat(switched, answered, read_requests(self._requests))

    def witness(self, verb: str, client_order_id: str, outcome: str) -> None:
        self.write(["sent", verb, client_order_id, outcome, self._source.state.value])

    def stop(self, at_ns: int) -> None:
        if self._file is not None:
            self.write(["stop", at_ns])  # not written by a session that went dark: its tape has its last line
            self._shut_part()
            self._file = None


# ---- replay: the recording stands in for the venue -----------------------------------------------------------------


def _line(row: Row) -> bytes:
    return json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _content(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()


def read_tape(path: Path) -> list[Row]:
    """Every whole line of a session's tape: its head, then its parts in order, compressed or not.

    A last line that was being written when the process died is left out. Only the last file can end so: a
    part that ends in the middle of a line and has a part after it is damaged, and is an error.
    """
    rows = []
    files = [path, *parts(path)]
    for file in files:
        lines = _content(file).split(b"\n")
        if lines[-1] and file is not files[-1]:
            raise TapeError(f"{file.name} ends in the middle of a line, and a later part follows it")
        for number, raw in enumerate(lines[:-1]):
            try:
                row = json.loads(raw)
            except ValueError as exc:
                raise TapeError(f"{file.name} line {number + 1} is not a tape line") from exc
            if not isinstance(row, list) or not row or not isinstance(row[0], str):
                raise TapeError(f"{file.name} line {number + 1} is not a tape line")
            rows.append(row)
    if len(rows) < 2 or rows[0][0] != "open" or rows[1][0] != "marks":
        raise TapeError(f"{path.name} does not begin as a tape does")
    return rows


class ReplayVenue:
    """What the order manager is handed in a replay: it answers a send or a cancel as the recording says it went.

    Nothing is behind it. An order "placed" here goes nowhere; the recording only says whether the real send
    succeeded, was refused or failed, so that the manager draws the conclusion it drew then.
    """

    def __init__(self, edge: ReplayEdge, name: str, instruments: Sequence[Instrument]) -> None:
        self._edge, self._name = edge, name
        self._instruments = {(instrument.pair, instrument.kind): instrument for instrument in instruments}

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> ConnectionState:
        return self._edge.state

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(frozenset(kind for _, kind in self._instruments), depth=False, historical_quotes=False)

    def place(self, order: Order, instrument: Instrument) -> None:
        how = self._edge.sent("place", order.client_order_id)
        if how == REFUSED:
            raise OrderRefused("the recording says the venue refused this order before it was sent")
        if how == LOCKED:
            raise PaperOnlyError("the recording says the paper-only lock refused this order")
        if how == UNCERTAIN:
            raise VenueUnavailable("the recording says the send failed part way")
        if how != SENT:
            raise RuntimeError("the recording says the send raised something unexpected")

    def cancel(self, client_order_id: str) -> None:
        if self._edge.sent("cancel", client_order_id) == FAILED:
            raise VenueUnavailable("the recording says the cancel could not be sent")

    def mark_reconciled(self) -> None:
        if self._edge.state is ConnectionState.RECONCILING:
            self._edge.state = ConnectionState.READY
        elif self._edge.state is not ConnectionState.READY:
            raise NotReady(f"there is no connection to reconcile: the venue is {self._edge.state.name}")

    def instrument(self, pair: str, kind: InstrumentKind) -> Instrument:
        return self._instruments[pair, kind]

    def open_orders(self) -> tuple[OpenOrder, ...]:
        return self._edge.asks.open_orders()

    def positions(self) -> tuple[VenuePosition, ...]:
        return self._edge.asks.positions()

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        return self._edge.asks.executions_since(ts_ns)

    def cash(self) -> tuple[VenueCash, ...]:
        return self._edge.asks.cash()

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def poll(self, timeout_ns: int = 0) -> list[VenueEvent]:
        return []

    def managed_accounts(self) -> tuple[str, ...]:
        return ()

    def server_time_ns(self) -> int:
        raise Unsupported("a recording has no clock of its own")

    def subscribe_quotes(self, instrument: Instrument) -> None:
        return None

    def unsubscribe_quotes(self, instrument: Instrument) -> None:
        return None

    def subscribe_depth(self, instrument: Instrument, levels: int) -> None:
        raise Unsupported("a recording has no depth")

    def unsubscribe_depth(self, instrument: Instrument) -> None:
        return None

    def historical_quotes(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]:
        raise Unsupported("a recording has no history")


class _Replayed:
    def __init__(self, edge: ReplayEdge) -> None:
        self._edge = edge

    def _ask(self, question: str) -> tuple[Any, ...]:
        row = self._edge.take("ask", question)
        self._edge.state = ConnectionState(row[2])
        if row[3] is None:
            raise _ERRORS.get(row[4], VenueError)("the recording says the venue could not be asked")
        return tuple(_ANSWERS[question][1](item) for item in row[3])

    def open_orders(self) -> tuple[OpenOrder, ...]:
        return self._ask("open_orders")

    def positions(self) -> tuple[VenuePosition, ...]:
        return self._ask("positions")

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        return self._ask("executions_since")

    def cash(self) -> tuple[VenueCash, ...]:
        return self._ask("cash")


class ReplayEdge:
    """A recorded session as the edge the trader ran on. `paper` says whether that run could send orders."""

    def __init__(self, rows: Sequence[Row], *, paper: bool) -> None:
        self._rows, self._at = list(rows), 2
        self.stopped = self._rows[-1][0] == "stop"  # the session said it ended; otherwise the process was killed,
        self.dark = self._rows[-1][0] == "dark"  # or the session went on with no room left to record itself
        opened = self._rows[0]
        self.state = ConnectionState(opened[4])
        described = tuple(instrument_of(entry) for entry in opened[3])
        self._opened = Opened(opened[1], opened[2], described, self.state)
        self._venue = ReplayVenue(self, opened[2], self._opened.instruments) if paper else None
        self._asks = _Replayed(self)

    @property
    def venue(self) -> Venue | None:
        return self._venue

    @property
    def asks(self) -> Asked:
        return self._asks

    @property
    def marks(self) -> dict[str, Any]:
        found: dict[str, Any] = self._rows[1][1]
        return found

    def open(self) -> Opened:
        return self._opened

    def begin(self, path: Path, marks: Mapping[str, object]) -> None:
        return None  # the replay writes no tape; what it was started from is checked by whoever built it

    def take(self, kind: str, what: str | None = None) -> Row:
        """The next line, which must be of this kind. The end of the recording is the end of the process."""
        if self._at >= len(self._rows):
            raise TapeEnded()
        row = self._rows[self._at]
        if row[0] == "dark":
            raise TapeEnded()  # the session went on from here, and nothing says how
        if row[0] != kind or (what is not None and row[1] != what):
            wanted = kind if what is None else f"{kind} {what}"
            raise TapeError(f"line {self._at + 1}: the replay wants {wanted!r} where the recording has {row[:2]!r}")
        self._at += 1
        return row

    def next(self, *, urgent: bool) -> Step | None:
        if self._at >= len(self._rows):
            return None
        if self._rows[self._at][0] in ("stop", "dark"):
            return None
        row = self.take("step")
        self.state = ConnectionState(row[2])
        room = row[5] if len(row) > 5 else None
        return Step(row[1], self.state, tuple(decode(event) for event in row[3]), _beat_of(row[4]), room)

    def sent(self, verb: str, client_order_id: str) -> str:
        row = self.take("sent", verb)
        if row[2] != client_order_id:
            raise TapeError(f"line {self._at}: the replay sends {client_order_id} where the recording sent {row[2]}")
        self.state = ConnectionState(row[4])
        return str(row[3])

    def witness(self, verb: str, client_order_id: str, outcome: str) -> None:
        return None

    def stop(self, at_ns: int) -> None:
        return None

    def room(self) -> None:
        return None  # a replay counts no tapes: the room is on the lines it reads


def _is_a_venue(venue: ReplayVenue) -> Venue:
    return venue  # checked by mypy: the order manager can be built on it


def _are_edges(running: LiveEdge, replayed: ReplayEdge) -> tuple[Edge, Edge]:
    return running, replayed
