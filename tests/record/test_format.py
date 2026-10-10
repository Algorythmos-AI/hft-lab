"""The recording format: exact round trips, and a torn tail that is recognised at every possible byte."""
from __future__ import annotations

import io

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import Quote
from hftlab.record import format as fmt
from hftlab.record.segment import SegmentWriter, inspect, read_segment
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.record.kit import PAIRS, T0

u64 = st.integers(0, 2**64 - 1)
kinds, pair_ids = st.sampled_from(sorted(fmt.KINDS)), st.integers(0, 0xFFFF)
records = st.builds(fmt.Record, u64, u64, u64, kinds, pair_ids, u64, u64, u64, u64)
HEADER = fmt.Header(run=3, opened_ns=T0, pairs=PAIRS)


def a_record(index: int) -> fmt.Record:
    book = (108_500 + index, 108_510 + index, 10**6, 2 * 10**6)
    return fmt.Record(index, T0 + index, index + 1, fmt.QUOTE, index % 2, *book)


def write(path, blocks: list[int], start: int = 0) -> list[fmt.Record]:
    """A segment with the given numbers of records per block. Returns every record written."""
    writer = SegmentWriter(path, HEADER, fsync=False)
    written, index = [], start
    for count in blocks:
        for _ in range(count):
            writer.append(a_record(index))
            written.append(a_record(index))
            index += 1
        writer.flush()
    writer.close()
    return written


def read(path) -> tuple[list[fmt.Record], fmt.BlockScan]:
    _, scan, found = read_segment(path)
    return list(found), scan


# ---- records, blocks, headers ---------------------------------------------------------------------------------------


def test_a_record_is_sixty_four_bytes_and_a_block_is_its_records_plus_twelve():
    assert fmt.RECORD_SIZE == 64 and len(a_record(0).pack()) == 64
    assert len(fmt.pack_block([a_record(i) for i in range(5)])) == 12 + 5 * 64


@given(st.lists(records, min_size=1, max_size=40))
def test_a_block_round_trips_every_field_of_every_record(batch):
    scan = fmt.BlockScan()
    (found,) = list(fmt.read_blocks(io.BytesIO(fmt.pack_block(batch)), scan))
    assert found == batch and (scan.blocks, scan.records, scan.torn) == (1, len(batch), False)
    assert scan.good_bytes == 12 + 64 * len(batch)


prices, sizes = st.integers(1, 10**9), st.integers(0, 10**12)


@given(prices, prices, sizes, sizes, u64, u64, st.integers(0, 1))
def test_a_quote_survives_the_trip_through_a_record(bid, ask, bid_size, ask_size, ts, seq, pair):
    quote = Quote(PAIRS[pair], bid, ask, bid_size, ask_size, ts, seq)
    assert fmt.to_event(fmt.from_quote(7, quote, pair), PAIRS) == quote


def test_every_kind_of_record_becomes_the_event_the_live_process_saw():
    heartbeat = fmt.to_event(fmt.Record(1, T0, 9, fmt.HEARTBEAT, 1, T0 - 5), PAIRS)
    assert heartbeat == fmt.Heartbeat("USD.JPY", T0, T0 - 5, 9)
    code = fmt.STATE_CODES[ConnectionState.RECONCILING]
    assert fmt.to_event(fmt.Record(2, T0, 10, fmt.CONNECTION, fmt.NO_PAIR, code), PAIRS) == ConnectionEvent(
        ConnectionState.RECONCILING, T0, 10
    )
    assert fmt.to_event(fmt.Record(3, T0, 0, fmt.START, fmt.NO_PAIR, 4), PAIRS) == fmt.Marker(True, T0, 4)
    assert fmt.to_event(fmt.Record(4, T0, 0, fmt.STOP, fmt.NO_PAIR, 4), PAIRS) == fmt.Marker(False, T0, 4)
    assert set(fmt.STATES.values()) == set(ConnectionState)
    with pytest.raises(fmt.CorruptSegment):
        fmt.to_event(fmt.Record(5, T0, 0, 99, 0), PAIRS)


def test_the_header_round_trips_and_a_damaged_one_is_refused():
    packed = HEADER.pack()
    assert fmt.read_header(io.BytesIO(packed)) == HEADER
    for damaged in (packed[:10], b"NOTASEG\n" + packed[8:], packed[:-1] + b"]", packed[:20] + b"X" + packed[21:]):
        with pytest.raises(fmt.CorruptSegment):
            fmt.read_header(io.BytesIO(damaged))


@pytest.mark.parametrize("count", [0, fmt.MAX_BLOCK_RECORDS + 1])
def test_a_block_holds_at_least_one_record_and_not_too_many(count):
    with pytest.raises(ValueError):
        fmt.pack_block([a_record(0)] * count)


# ---- a torn tail ----------------------------------------------------------------------------------------------------


def test_a_whole_segment_reads_back_exactly(tmp_path):
    written = write(tmp_path / "03.seg", [3, 1, 5])
    found, scan = read(tmp_path / "03.seg")
    assert found == written and (scan.blocks, scan.records, scan.torn) == (3, 9, False)
    info = inspect(tmp_path / "03.seg")
    assert info.last == written[-1] and info.good_end == (tmp_path / "03.seg").stat().st_size


def test_cut_at_every_byte_of_the_tail_block_the_reader_stops_cleanly_before_it(tmp_path):
    path = tmp_path / "03.seg"
    written = write(path, [4, 2, 3])
    whole = path.read_bytes()
    tail_start = len(whole) - (12 + 3 * 64)
    for cut in range(tail_start, len(whole) + 1):
        path.write_bytes(whole[:cut])
        found, scan = read(path)
        complete = cut == len(whole)
        assert found == (written if complete else written[:6]), cut
        assert scan.torn == (tail_start < cut < len(whole)), cut
        assert inspect(path).good_end == (len(whole) if complete else tail_start), cut


def test_after_any_cut_a_restarted_writer_continues_with_no_duplicate_and_no_hole(tmp_path):
    path = tmp_path / "03.seg"
    written = write(path, [4, 2, 3])
    whole = path.read_bytes()
    tail_start = len(whole) - (12 + 3 * 64)
    for cut in range(tail_start, len(whole), 7):
        path.write_bytes(whole[:cut])
        writer = SegmentWriter(path, HEADER, fsync=False)
        assert writer.last == written[5] and writer.size == tail_start, "cut back to the last whole block"
        for index in range(6, 10):
            writer.append(a_record(index))
        writer.close()
        found, scan = read(path)
        assert [r.index for r in found] == list(range(10)) and not scan.torn, cut


def test_a_tail_block_with_a_bad_checksum_is_a_torn_tail_but_one_in_the_middle_is_corruption(tmp_path):
    path = tmp_path / "03.seg"
    written = write(path, [4, 2, 3])
    whole = bytearray(path.read_bytes())
    whole[-1] ^= 0xFF
    path.write_bytes(bytes(whole))
    found, scan = read(path)
    assert found == written[:6] and scan.torn
    whole[-1] ^= 0xFF
    first_block_byte = len(HEADER.pack()) + 12 + 5
    whole[first_block_byte] ^= 0xFF
    path.write_bytes(bytes(whole))
    with pytest.raises(fmt.CorruptSegment, match="not the last"):
        read(path)
    whole[first_block_byte] ^= 0xFF
    whole[len(HEADER.pack())] = ord("X")
    path.write_bytes(bytes(whole))
    with pytest.raises(fmt.CorruptSegment, match="no block where one should start"):
        read(path)


def test_a_segment_for_other_pairs_cannot_be_continued(tmp_path):
    write(tmp_path / "03.seg", [2])
    with pytest.raises(fmt.CorruptSegment, match="other pairs"):
        SegmentWriter(tmp_path / "03.seg", fmt.Header(4, T0, ("GBP.USD",)), fsync=False)


def test_nothing_is_on_disk_until_the_block_is_flushed(tmp_path):
    writer = SegmentWriter(tmp_path / "03.seg", HEADER, fsync=True)
    for index in range(3):
        writer.append(a_record(index))
    assert read(tmp_path / "03.seg")[0] == [] and writer.buffered == 3 and writer.pending_bytes() == 12 + 3 * 64
    assert writer.flush() == 12 + 3 * 64 and len(read(tmp_path / "03.seg")[0]) == 3
    writer.append(a_record(3))
    assert writer.discard() == 1 and writer.flush() == 0
    writer.close()
    assert len(read(tmp_path / "03.seg")[0]) == 3
