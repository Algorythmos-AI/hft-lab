"""The recording format: fixed-width records, in checksummed blocks, in hourly segments (ADR 0012).

  segment  = header, block*
  header   = "HFTSEG1\\n", length u32, crc32 u32, JSON {version, run, opened_ns, pairs}
  block    = "BLK1", count u32, crc32 u32, count records      (the crc covers the count and the records)
  record   = 64 bytes, little endian:
             index u64 | recv_ts_ns u64 | venue_seq u64 | kind u8 | pair u16 | 5 reserved | a, b, c, d u64

`index` is the recorder's own counter: one sequence across every pair and every restart, with no holes.
`recv_ts_ns` is the injected clock's time when this process received the event; `venue_seq` is the venue's
sequence number for it. What a, b, c and d mean depends on the kind:

  QUOTE       bid, ask, bid size, ask size (integers at the pair's scale; the pair is named in the header)
  HEARTBEAT   the venue's own clock, when it last answered; the book of this pair had not changed
  CONNECTION  the connection state code
  START       the run number; the recorder process started here
  STOP        the recorder stopped on purpose here

A block is written with one append. A process killed part-way leaves a short or damaged last block, which a
reader recognises and stops before; a block that fails its checksum with more data after it is corruption and
is an error. Nothing here uses a float, and nothing needs anything but the standard library.
"""
from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import IO, Final

from hftlab.core import Instrument, InstrumentKind, Quote
from hftlab.venues.port import ConnectionEvent, ConnectionState

VERSION: Final = 1
SEGMENT_MAGIC: Final = b"HFTSEG1\n"
BLOCK_MAGIC: Final = b"BLK1"
RECORD: Final = struct.Struct("<QQQBH5xQQQQ")
RECORD_SIZE: Final = RECORD.size
BLOCK_HEAD: Final = struct.Struct("<4sII")
SEGMENT_HEAD: Final = struct.Struct("<8sII")
MAX_BLOCK_RECORDS: Final = 65_536
NO_PAIR: Final = 0xFFFF

QUOTE, HEARTBEAT, CONNECTION, START, STOP = 1, 2, 3, 4, 5
KINDS: Final = frozenset({QUOTE, HEARTBEAT, CONNECTION, START, STOP})
STATE_CODES: Final = {state: code for code, state in enumerate(ConnectionState, 1)}
STATES: Final = {code: state for state, code in STATE_CODES.items()}

assert RECORD_SIZE == 64


class CorruptSegment(Exception):
    """A segment that is damaged somewhere other than its very end. Never read past, never repaired."""


@dataclass(frozen=True, slots=True)
class Record:
    index: int
    recv_ts_ns: int
    venue_seq: int
    kind: int
    pair: int
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0

    def pack(self) -> bytes:
        head = (self.index, self.recv_ts_ns, self.venue_seq, self.kind, self.pair)
        return RECORD.pack(*head, self.a, self.b, self.c, self.d)


@dataclass(frozen=True, slots=True)
class Header:
    run: int
    opened_ns: int
    pairs: tuple[str, ...]

    def pack(self) -> bytes:
        body = json.dumps(
            {"version": VERSION, "run": self.run, "opened_ns": self.opened_ns, "pairs": list(self.pairs)},
            sort_keys=True,
        ).encode("ascii")
        return SEGMENT_HEAD.pack(SEGMENT_MAGIC, len(body), zlib.crc32(body)) + body


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """The venue answered and this pair's book had not changed: "no change", as opposed to "no data"."""

    pair: str
    recv_ts_ns: int
    server_ts_ns: int
    seq: int


@dataclass(frozen=True, slots=True)
class Marker:
    """The recorder started (`run` is its run number) or stopped on purpose."""

    started: bool
    recv_ts_ns: int
    run: int


RecordedEvent = Quote | Heartbeat | ConnectionEvent | Marker


def describe(instrument: Instrument) -> dict[str, object]:
    """An instrument as the journal keeps it: every price in the records is an integer at this scale."""
    return {
        "pair": instrument.pair,
        "kind": instrument.kind.value,
        "price_scale": instrument.price_scale,
        "min_order_size": instrument.min_order_size,
        "size_increment": instrument.size_increment,
        "price_increment": instrument.price_increment,
    }


def instrument_of(entry: dict[str, object]) -> Instrument:
    def whole(key: str) -> int:
        value = entry.get(key)
        if type(value) is not int:
            raise CorruptSegment(f"an instrument in the journal has no whole number for {key}: {entry!r}")
        return value

    try:
        return Instrument.from_pair(
            str(entry.get("pair")),
            price_scale=whole("price_scale"),
            min_order_size=whole("min_order_size"),
            kind=InstrumentKind(entry.get("kind")),
            size_increment=whole("size_increment"),
            price_increment=whole("price_increment"),
        )
    except ValueError as exc:
        raise CorruptSegment(f"an instrument in the journal cannot be read: {exc}") from exc


def pack_block(records: Sequence[Record]) -> bytes:
    if not 0 < len(records) <= MAX_BLOCK_RECORDS:
        raise ValueError(f"a block holds 1 to {MAX_BLOCK_RECORDS} records, not {len(records)}")
    payload = b"".join(record.pack() for record in records)
    count = struct.pack("<I", len(records))
    return BLOCK_HEAD.pack(BLOCK_MAGIC, len(records), zlib.crc32(count + payload)) + payload


def read_header(stream: IO[bytes]) -> Header:
    head = stream.read(SEGMENT_HEAD.size)
    if len(head) < SEGMENT_HEAD.size:
        raise CorruptSegment("the segment is shorter than its own header")
    magic, length, crc = SEGMENT_HEAD.unpack(head)
    body = stream.read(length) if magic == SEGMENT_MAGIC and length < 1_000_000 else b""
    if magic != SEGMENT_MAGIC or len(body) != length or zlib.crc32(body) != crc:
        raise CorruptSegment("the segment header is damaged or this is not a segment")
    fields = json.loads(body)
    if fields.get("version") != VERSION:
        raise CorruptSegment(f"segment version {fields.get('version')!r} is not {VERSION}")
    return Header(int(fields["run"]), int(fields["opened_ns"]), tuple(fields["pairs"]))


@dataclass(slots=True)
class BlockScan:
    """What reading the blocks of a segment found. `good_bytes` is where the last whole block ends."""

    good_bytes: int = 0
    blocks: int = 0
    records: int = 0
    torn: bool = False


def read_blocks(stream: IO[bytes], scan: BlockScan) -> Iterator[list[Record]]:
    """Each whole, checksummed block as a list of records. Stops cleanly at a torn tail and says so in `scan`."""
    while True:
        head = stream.read(BLOCK_HEAD.size)
        if not head:
            return
        if len(head) < BLOCK_HEAD.size:
            scan.torn = True
            return
        magic, count, crc = BLOCK_HEAD.unpack(head)
        if magic != BLOCK_MAGIC or not 0 < count <= MAX_BLOCK_RECORDS:
            raise CorruptSegment(f"no block where one should start, at byte {scan.good_bytes}")
        payload = stream.read(count * RECORD_SIZE)
        if len(payload) < count * RECORD_SIZE:
            scan.torn = True
            return
        if zlib.crc32(head[4:8] + payload) != crc:
            if stream.read(1):
                raise CorruptSegment(f"a block fails its checksum at byte {scan.good_bytes} and is not the last")
            scan.torn = True
            return
        scan.good_bytes += BLOCK_HEAD.size + len(payload)
        scan.blocks += 1
        scan.records += count
        yield [Record(*fields) for fields in RECORD.iter_unpack(payload)]


def from_quote(index: int, quote: Quote, pair: int) -> Record:
    return Record(index, quote.recv_ts_ns, quote.seq, QUOTE, pair, quote.bid, quote.ask, quote.bid_size, quote.ask_size)


def to_event(record: Record, pairs: Sequence[str]) -> RecordedEvent:
    """A record as the event the live process saw."""
    if record.kind == QUOTE:
        return Quote(pairs[record.pair], record.a, record.b, record.c, record.d, record.recv_ts_ns, record.venue_seq)
    if record.kind == HEARTBEAT:
        return Heartbeat(pairs[record.pair], record.recv_ts_ns, record.a, record.venue_seq)
    if record.kind == CONNECTION:
        return ConnectionEvent(STATES[record.a], record.recv_ts_ns, record.venue_seq)
    if record.kind in (START, STOP):
        return Marker(record.kind == START, record.recv_ts_ns, record.a)
    raise CorruptSegment(f"record {record.index} has an unknown kind {record.kind}")
