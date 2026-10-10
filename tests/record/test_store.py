"""The store: hourly segments, recovery after a kill, a quota that is never broken, and no deletion before a copy."""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hftlab.record import format as fmt
from hftlab.record.segment import count_records
from hftlab.record.store import (
    JOURNAL,
    DirectoryOffload,
    Housekeeper,
    QuotaExceeded,
    StoreIOError,
    StoreSettings,
    append_line,
    days,
    file_sha256,
    measure,
    read_journal,
    segments,
)
from tests.record.kit import HOUR, MS, ROOMY, SECOND, T0, a_disk, a_store, everything, indices, quote
from tests.support import ROOT


def names(root) -> list[str]:
    return [f"{day.name}/{path.name}" for day in days(root) for path in segments(day)]


# ---- layout and order -----------------------------------------------------------------------------------------------


def test_records_go_to_the_segment_of_their_utc_hour_with_one_index_across_all(tmp_path):
    store = a_store(tmp_path)
    for at in (T0, T0 + 10 * MS, T0 + HOUR - 1, T0 + HOUR, T0 + 21 * HOUR, T0 + 21 * HOUR + 5):
        quote(store, at, pair="USD.JPY" if at % 2 else "EUR.USD")
    store.close()
    assert names(tmp_path) == ["2026-10-14/03.seg", "2026-10-14/04.seg", "2026-10-15/00.seg"]
    found = everything(tmp_path)
    assert [r.index for r in found] == list(range(6))
    assert [r.recv_ts_ns for r in found] == sorted(r.recv_ts_ns for r in found)
    assert {r.pair for r in found} == {0, 1}


def test_a_clock_that_steps_back_does_not_reopen_a_closed_hour(tmp_path):
    store = a_store(tmp_path)
    quote(store, T0 + HOUR)
    quote(store, T0 + HOUR - 5 * SECOND)  # the wall clock was corrected backwards
    store.close()
    assert names(tmp_path) == ["2026-10-14/04.seg"] and indices(tmp_path) == [0, 1]


def test_a_changed_pair_list_starts_a_new_part_instead_of_mixing_tables(tmp_path):
    first = a_store(tmp_path)
    quote(first, T0)
    first.close()
    second = a_store(tmp_path, pairs=("GBP.USD", "EUR.USD"))
    quote(second, T0 + SECOND, pair="GBP.USD")
    second.close()
    assert names(tmp_path) == ["2026-10-14/03.seg", "2026-10-14/03.1.seg"] and indices(tmp_path) == [0, 1]


# ---- restart and kill -----------------------------------------------------------------------------------------------


def test_a_restart_continues_the_index_and_counts_a_new_run(tmp_path):
    first = a_store(tmp_path)
    for i in range(20):
        quote(first, T0 + i * MS)
    first.close()
    second = a_store(tmp_path)
    assert (first.run, second.run, second.next_index) == (1, 2, 20)
    quote(second, T0 + SECOND)
    second.close()
    assert indices(tmp_path) == list(range(21))


def test_what_was_never_flushed_is_lost_and_its_indices_are_used_again(tmp_path):
    first = a_store(tmp_path)  # blocks of 8
    for i in range(20):
        quote(first, T0 + i * MS)
    del first  # killed: 16 records are on disk in two blocks, 4 were still in memory
    second = a_store(tmp_path)
    assert second.next_index == 16
    quote(second, T0 + SECOND)
    second.close()
    assert indices(tmp_path) == list(range(17))


def test_a_torn_tail_left_by_a_kill_is_cut_off_before_anything_is_appended(tmp_path):
    first = a_store(tmp_path)
    for i in range(16):
        quote(first, T0 + i * MS)
    first.close()
    path = tmp_path / "2026-10-14" / "03.seg"
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) - 100])  # the second block is torn
    second = a_store(tmp_path)
    assert second.next_index == 8
    for i in range(8):
        quote(second, T0 + SECOND + i)
    second.close()
    assert indices(tmp_path) == list(range(16))


WRITER = """
import sys
sys.path[:0] = [{src!r}, {root!r}]
from pathlib import Path
from hftlab.calendar import Calendar
from hftlab.record import format as fmt
from hftlab.record.store import Store, StoreSettings
store = Store(Path(sys.argv[1]), ("EUR.USD", "USD.JPY"), StoreSettings(1 << 30, 10**18, block_records=4, fsync=False),
              calendar=Calendar())
store.open()
store.write(fmt.START, store.last_ts_ns + 1, 0, None, store.run)
print("ready", flush=True)
at = store.last_ts_ns
while True:
    at += 1000
    store.write(fmt.QUOTE, at, at, "EUR.USD" if at % 2000 else "USD.JPY", 108500, 108510, 1, 1)
"""


def test_a_writer_process_killed_mid_write_three_times_leaves_one_unbroken_sequence(tmp_path):
    script = WRITER.format(src=str(ROOT / "src"), root=str(ROOT))
    for _ in range(3):
        child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE)
        assert child.stdout.readline().strip() == b"ready"
        time.sleep(0.15)
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
        child.stdout.close()
    found = everything(tmp_path)
    assert len(found) > 100, "the writers did write"
    assert [r.index for r in found] == list(range(len(found))), "no hole, no duplicate, no reordering"
    assert [r.recv_ts_ns for r in found] == sorted(r.recv_ts_ns for r in found)
    starts = [r.a for r in found if r.kind == fmt.START]
    assert starts == [1, 2, 3]
    a_store(tmp_path).close()  # and it opens cleanly a fourth time


# ---- the quota ------------------------------------------------------------------------------------------------------


def test_the_store_refuses_to_grow_past_its_quota(tmp_path):
    store = a_store(tmp_path, StoreSettings(quota_bytes=4_000, keep_ns=HOUR, block_records=8, fsync=False))
    written = 0
    with pytest.raises(QuotaExceeded, match="nothing more is written"):
        for i in range(1_000):
            quote(store, T0 + i * MS)
            written += 1
    on_disk = sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file())
    assert on_disk <= 4_000 and store.used_bytes == on_disk
    with pytest.raises(QuotaExceeded):
        store.flush()
    stored = len(everything(tmp_path))
    assert 0 < stored < written and stored % 8 == 0, "whole blocks only, and the refused block is not there"


def test_closing_a_store_that_is_over_quota_drops_the_refused_block_and_the_next_run_has_no_hole(tmp_path):
    settings = StoreSettings(quota_bytes=4_000, keep_ns=HOUR, block_records=8, fsync=False)
    store = a_store(tmp_path, settings)
    with pytest.raises(QuotaExceeded):
        for i in range(1_000):
            quote(store, T0 + i * MS)
    with pytest.raises(QuotaExceeded):
        store.close()
    roomy = a_store(tmp_path)
    quote(roomy, T0 + HOUR)
    roomy.close()
    assert indices(tmp_path) == list(range(len(indices(tmp_path))))


def test_room_made_by_the_offload_lets_recording_continue(tmp_path):
    settings = StoreSettings(quota_bytes=6_000, keep_ns=HOUR, block_records=8, fsync=False)
    store = a_store(tmp_path / "rec", settings, offload=DirectoryOffload(tmp_path / "away"))
    for hour in range(6):
        for i in range(40):
            quote(store, T0 + hour * HOUR + i * MS)
    store.close()
    assert sum(p.stat().st_size for p in (tmp_path / "rec").rglob("*") if p.is_file()) <= 6_000
    kept, sent = names(tmp_path / "rec"), sorted(p.name for p in (tmp_path / "away" / "2026-10-14").iterdir())
    assert kept and sent and all(name.endswith(".seg.gz") for name in sent)
    assert len(kept) + len(sent) == 6, "every hour is in exactly one place"


# ---- a write the operating system refuses ---------------------------------------------------------------------------


def test_a_write_the_disk_refuses_is_named_taken_back_and_ends_the_stores_writing(tmp_path):
    disk = a_disk()
    store = a_store(tmp_path, writer=disk)
    for i in range(12):  # blocks of 8: one block is on disk, four records are still in memory
        quote(store, T0 + i * MS)
    path = tmp_path / "2026-10-14" / "03.seg"
    size = path.stat().st_size
    disk.full = True
    with pytest.raises(StoreIOError, match="ENOSPC") as refused:
        for i in range(12, 20):
            quote(store, T0 + i * MS)
    assert refused.value.code == "ENOSPC" and not isinstance(refused.value, QuotaExceeded | OSError)
    assert path.stat().st_size == size, "the half of a block that got out was taken back"
    assert store.failed and store.buffered == 0, "what was buffered is dropped, not kept for a later try"
    disk.full = False  # there is room again, but not for this process
    with pytest.raises(StoreIOError, match="ENOSPC"):
        quote(store, T0 + SECOND)
    store.flush()
    store.close()
    assert indices(tmp_path) == list(range(8)), "nothing was appended after the refusal"

    second = a_store(tmp_path)
    assert second.next_index == 8 and not second.failed
    quote(second, T0 + 2 * SECOND)
    second.close()
    assert indices(tmp_path) == list(range(9))


def test_a_block_the_disk_would_not_take_back_is_cut_off_by_the_next_writer(tmp_path, monkeypatch):
    disk = a_disk(errno.EIO)
    store = a_store(tmp_path, writer=disk)
    for i in range(8):
        quote(store, T0 + i * MS)
    path = tmp_path / "2026-10-14" / "03.seg"
    size = path.stat().st_size

    def refuse(fd: int, length: int) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    disk.full = True
    with monkeypatch.context() as patch:
        patch.setattr(os, "ftruncate", refuse)
        with pytest.raises(StoreIOError, match="EIO"):
            for i in range(8, 16):
                quote(store, T0 + i * MS)
        store.close()
    assert path.stat().st_size > size, "the torn half of a block is still in the file"
    assert indices(tmp_path) == list(range(8)), "and a reader stops before it"
    second = a_store(tmp_path)
    quote(second, T0 + SECOND)
    second.close()
    assert indices(tmp_path) == list(range(9)) and path.stat().st_size > size


def test_a_disk_that_refuses_the_very_first_write_fails_the_open_by_name(tmp_path):
    disk = a_disk(half=False)
    disk.full = True
    store = a_store(tmp_path, writer=disk)  # opening an empty store writes only the run counter
    with pytest.raises(StoreIOError, match="ENOSPC"):
        quote(store, T0)  # the segment's header is the first thing the disk is asked to take
    assert store.failed and indices(tmp_path) == []


def test_a_journal_line_the_disk_cuts_short_is_taken_back(tmp_path, monkeypatch):
    path = tmp_path / JOURNAL
    append_line(path, b'{"n": 1}\n')
    real = os.write

    def short(fd: int, data: bytes) -> int:
        real(fd, bytes(data)[:4])
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short)
        with pytest.raises(OSError, match="No space"):
            append_line(path, b'{"n": 2}\n')
    assert path.read_bytes() == b'{"n": 1}\n'
    append_line(path, b'{"n": 3}\n')
    assert read_journal(tmp_path) == [{"n": 1}, {"n": 3}]


def test_a_torn_last_journal_line_is_skipped_by_the_reader_and_cut_off_by_the_next_append(tmp_path):
    store = a_store(tmp_path)
    quote(store, T0)
    store.close()
    day = tmp_path / "2026-10-14"
    whole = read_journal(day)
    with (day / JOURNAL).open("ab") as journal:
        journal.write(b'{"type": "ga')  # a line the last process never finished
    assert read_journal(day) == whole

    second = a_store(tmp_path)  # opens although the journal ends in the middle of a line
    quote(second, T0 + SECOND)
    second.note({"type": "note", "n": 1})
    second.close()
    assert read_journal(day)[: len(whole)] == whole and read_journal(day)[-1] == {"type": "note", "n": 1}
    assert b'"ga{' not in (day / JOURNAL).read_bytes() and (day / JOURNAL).read_bytes().endswith(b"\n")


def test_a_refused_journal_line_ends_the_writing_too_and_no_record_outlives_its_gap_line(tmp_path, monkeypatch):
    """A record whose gap could not be journalled must not reach the disk: the day check compares the two."""
    store = a_store(tmp_path)
    quote(store, T0)
    store.flush()

    def refuse(path: Path, line: bytes) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as patch:
        patch.setattr("hftlab.record.store.append_line", refuse)
        with pytest.raises(StoreIOError, match="ENOSPC"):
            quote(store, T0 + 60 * SECOND)  # closes a gap of a minute, which has to be journalled
        with pytest.raises(StoreIOError):
            store.note({"type": "note"})
    store.close()
    assert indices(tmp_path) == [0], "the quote that closed the gap was dropped with the line that explains it"
    assert [line for line in read_journal(tmp_path / "2026-10-14") if line["type"] == "gap"] == []


def test_measuring_the_store_skips_a_file_that_goes_away_meanwhile(tmp_path, monkeypatch):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "03.seg").write_bytes(b"x" * 10)
    (tmp_path / "d" / "04.seg").write_bytes(b"y" * 5)
    assert measure(tmp_path) == 15 and measure(tmp_path / "nothing-here") == 0
    real = Path.is_file

    def taken_away(self: Path) -> bool:
        if self.name == "03.seg":  # housekeeping compressed it between the listing and the look
            self.unlink(missing_ok=True)
        return real(self)

    monkeypatch.setattr(Path, "is_file", taken_away)
    assert measure(tmp_path) == 5


def test_counting_a_segments_records_from_its_block_heads_agrees_with_reading_it(tmp_path):
    store = a_store(tmp_path / "rec")
    for i in range(21):
        quote(store, T0 + i * MS)
    store.close()
    path = tmp_path / "rec" / "2026-10-14" / "03.seg"
    assert count_records(path) == 21 == len(everything(tmp_path / "rec"))
    whole = path.read_bytes()
    path.write_bytes(whole[:-100])  # a torn last block is not counted
    assert count_records(path) == 16
    path.write_bytes(whole)
    Housekeeper(tmp_path / "rec", ROOMY).run(T0 + 2 * HOUR)
    assert count_records(path.with_name("03.seg.gz")) == 21


# ---- housekeeping ---------------------------------------------------------------------------------------------------


def three_hours(tmp_path, **options):
    store = a_store(tmp_path / "rec", **options)
    for hour in range(3):
        for i in range(20):
            quote(store, T0 + hour * HOUR + i * MS)
    store.flush()
    return store


def test_closed_segments_are_compressed_and_read_back_the_same(tmp_path):
    store = three_hours(tmp_path)
    before = everything(tmp_path / "rec")
    report = Housekeeper(tmp_path / "rec", ROOMY).run(T0 + 2 * HOUR + SECOND, keep=None)
    assert report.compressed == 2 and names(tmp_path / "rec") == [
        "2026-10-14/03.seg.gz", "2026-10-14/04.seg.gz", "2026-10-14/05.seg",
    ]  # fmt: skip
    assert everything(tmp_path / "rec") == before, "the hour still being written is left alone, the rest read the same"
    quote(store, T0 + 2 * HOUR + SECOND)
    store.close()
    assert indices(tmp_path / "rec") == list(range(61))


def test_without_an_offload_nothing_is_ever_deleted(tmp_path):
    three_hours(tmp_path).close()
    report = Housekeeper(tmp_path / "rec", ROOMY).run(T0 + 1_000 * HOUR)
    assert report.handed_over == 0 and len(names(tmp_path / "rec")) == 3 and len(everything(tmp_path / "rec")) == 60


def test_old_segments_are_deleted_only_after_the_offload_confirms_them(tmp_path):
    three_hours(tmp_path).close()
    away = DirectoryOffload(tmp_path / "away")
    now = T0 + 26 * HOUR + 1  # the retention window is 24 hours: hours 03 and 04 are past it, 05 is not
    report = Housekeeper(tmp_path / "rec", ROOMY, away).run(now)
    assert (report.compressed, report.handed_over, report.refused) == (3, 2, 0)
    assert names(tmp_path / "rec") == ["2026-10-14/05.seg.gz"]
    for name in ("03.seg.gz", "04.seg.gz"):
        assert (tmp_path / "away" / "2026-10-14" / name).is_file()
    later = Housekeeper(tmp_path / "rec", ROOMY, away).run(T0 + 80 * HOUR)
    assert later.handed_over == 2, "the last segment and then the day's journal"
    assert not (tmp_path / "rec" / "2026-10-14").exists()
    assert (tmp_path / "away" / "2026-10-14" / "journal.jsonl").is_file()


class Unreliable(DirectoryOffload):
    """An offload that takes the file and then cannot vouch for it."""

    def confirm(self, name: str, sha256: str, size: int) -> bool:
        return False


class Truncating(DirectoryOffload):
    """An offload that loses the end of what it is given."""

    def put(self, source, name) -> None:
        super().put(source, name)
        target = self.target / name
        target.write_bytes(target.read_bytes()[:-1])


@pytest.mark.parametrize("offload", [Unreliable, Truncating])
def test_a_copy_that_is_not_confirmed_or_not_identical_keeps_the_original(tmp_path, offload):
    three_hours(tmp_path).close()
    before = everything(tmp_path / "rec")
    report = Housekeeper(tmp_path / "rec", ROOMY, offload(tmp_path / "away")).run(T0 + 80 * HOUR)
    assert report.handed_over == 0 and report.refused >= 3
    assert len(names(tmp_path / "rec")) == 3 and everything(tmp_path / "rec") == before


def test_the_directory_offload_confirms_only_an_identical_copy(tmp_path):
    source = tmp_path / "a.seg.gz"
    source.write_bytes(b"segment bytes")
    away = DirectoryOffload(tmp_path / "away")
    digest = file_sha256(source)
    assert not away.confirm("d/a.seg.gz", digest, 13)
    away.put(source, "d/a.seg.gz")
    assert away.confirm("d/a.seg.gz", digest, 13) and not away.confirm("d/a.seg.gz", digest, 12)
    assert not away.confirm("d/a.seg.gz", "0" * 64, 13) and not list((tmp_path / "away" / "d").glob("*.partial"))


def test_each_hour_starts_with_a_carry_line_in_the_days_journal(tmp_path):
    three_hours(tmp_path).close()
    journal = read_journal(tmp_path / "rec" / "2026-10-14")
    carries = [line for line in journal if line["type"] == "carry"]
    assert [line["hour"] for line in carries] == [3, 4, 5] and carries[1]["last_data_ns"] == {"EUR.USD": T0 + 19 * MS}
