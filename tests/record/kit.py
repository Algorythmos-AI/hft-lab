"""Shared pieces for the recording tests."""
from __future__ import annotations

import errno
import os
from pathlib import Path

from hftlab.calendar import Calendar
from hftlab.record import format as fmt
from hftlab.record.segment import SegmentWriter
from hftlab.record.store import Store, StoreSettings, days, read_day

SECOND = 1_000_000_000
MS = 1_000_000
HOUR = 3_600 * SECOND
# Wednesday 2026-10-14 03:00:00 UTC: the middle of a forex week, on the hour.
T0 = 1_791_946_800 * SECOND
PAIRS = ("EUR.USD", "USD.JPY")
ROOMY = StoreSettings(quota_bytes=1 << 30, keep_ns=24 * HOUR, block_records=8, fsync=False)


def a_store(root: Path, settings: StoreSettings = ROOMY, pairs=PAIRS, *, opened: bool = True, **options) -> Store:
    store = Store(root, pairs, settings, calendar=options.pop("calendar", Calendar()), **options)
    if opened:
        store.open()
    return store


def quote(store: Store, at_ns: int, pair: str = "EUR.USD", bid: int = 108_500, seq: int = 0) -> fmt.Record:
    return store.write(fmt.QUOTE, at_ns, seq or store.next_index + 1, pair, bid, bid + 10, 1_000_000, 2_000_000)


def everything(root: Path) -> list[fmt.Record]:
    return [record for day in days(root) for record, _ in read_day(day)]


def indices(root: Path) -> list[int]:
    return [record.index for record in everything(root)]


def a_disk(refuses: int = errno.ENOSPC, *, half: bool = True) -> type[SegmentWriter]:
    """A segment writer for a disk that can fill up. Set `.full` on the class it returns and the next block gets

    half-way out (or not at all) before the write is refused with `refuses`.
    """

    class Disk(SegmentWriter):
        full = False

        def _write(self, data: bytes) -> None:
            if Disk.full:
                if half:
                    super()._write(data[: len(data) // 2])
                raise OSError(refuses, os.strerror(refuses))
            super()._write(data)

    return Disk
