"""The store: day folders of hourly segments, a journal per day, a quota, and a way out for old data.

  <root>/state.json                 the run counter
  <root>/<YYYY-MM-DD>/<HH>.seg      the segment being written (UTC day and hour of the receive time)
  <root>/<YYYY-MM-DD>/<HH>.seg.gz   a closed segment, compressed
  <root>/<YYYY-MM-DD>/journal.jsonl gaps, starts and stops; a `carry` line where the tracker's state is needed

Three promises:

  * Restarting never duplicates or reorders. On opening, the last segment is cut back to its last whole block
    and the index continues from the last record that is really on disk.
  * The store never grows past its quota. A block that would not fit is not written; `QuotaExceeded` is raised
    and the caller stops recording and says so. Filling the disk would take the other desks down with it.
  * Nothing is deleted until it is somewhere else. A closed segment older than the retention window is handed
    to the offload, and removed here only after the offload has confirmed its size and SHA-256. With no
    offload configured, nothing is ever deleted.

Compressing and handing over are `Housekeeper`'s work and run as their own command, not inside the recorder:
compressing an hour of data takes long enough to delay the receive time of every quote that arrives meanwhile.
The recorder calls it only as a last resort, when the quota is reached.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from hftlab.calendar import Calendar
from hftlab.record.format import Header, Record
from hftlab.record.gaps import GapTracker
from hftlab.record.segment import SegmentWriter, inspect, read_segment

SECOND: Final = 1_000_000_000
HOUR: Final = 3_600 * SECOND
JOURNAL: Final = "journal.jsonl"
_SEGMENT = re.compile(r"(?P<hour>[0-9]{2})(?:\.(?P<part>[0-9]+))?\.seg(?P<gz>\.gz)?")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class QuotaExceeded(Exception):
    """The next block would take the store past its quota. Nothing was written."""


class Offload(Protocol):
    """Somewhere older segments go. `confirm` is asked before anything is deleted locally."""

    def put(self, source: Path, name: str) -> None: ...

    def confirm(self, name: str, sha256: str, size: int) -> bool: ...


class DirectoryOffload:
    """Offload to another directory (a mounted disk, a synced folder). No network code."""

    def __init__(self, target: Path) -> None:
        self.target = target

    def put(self, source: Path, name: str) -> None:
        destination = self.target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        with source.open("rb") as read, partial.open("wb") as write:
            shutil.copyfileobj(read, write)
            write.flush()
            os.fsync(write.fileno())
        os.replace(partial, destination)

    def confirm(self, name: str, sha256: str, size: int) -> bool:
        destination = self.target / name
        return destination.is_file() and destination.stat().st_size == size and file_sha256(destination) == sha256


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def day_of(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // SECOND, tz=UTC).strftime("%Y-%m-%d")


def hour_of(ts_ns: int) -> int:
    return ts_ns // HOUR % 24


def segments(day: Path) -> list[Path]:
    """The segments of one day folder, in the order they were written."""
    found = []
    for path in day.iterdir():
        match = _SEGMENT.fullmatch(path.name)
        if match:
            found.append((int(match["hour"]), int(match["part"] or 0), path))
    return [path for _, _, path in sorted(found)]


def days(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and _DAY.fullmatch(p.name)) if root.is_dir() else []


def read_day(day: Path) -> Iterator[tuple[Record, tuple[str, ...]]]:
    """Every record of a day, in index order, with the pair table of the segment it came from."""
    for path in segments(day):
        header, _, records = read_segment(path)
        for record in records:
            yield record, header.pairs


def read_journal(day: Path) -> list[dict[str, object]]:
    path = day / JOURNAL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass(frozen=True, slots=True)
class StoreSettings:
    quota_bytes: int
    keep_ns: int
    block_records: int = 256
    fsync: bool = True
    compress: bool = True
    gap_threshold_ns: int = 5 * SECOND


class Store:
    def __init__(
        self,
        root: Path,
        pairs: Sequence[str],
        settings: StoreSettings,
        *,
        calendar: Calendar,
        offload: Offload | None = None,
    ) -> None:
        self.root = root
        self.pairs = tuple(pairs)
        self.settings = settings
        self._calendar = calendar
        self._offload = offload
        self._ids = {pair: number for number, pair in enumerate(self.pairs)}
        self._writer: SegmentWriter | None = None
        self._slot: tuple[str, int] | None = None
        self._tracker = GapTracker(self.pairs, calendar, settings.gap_threshold_ns)
        self.run = 0
        self.next_index = 0
        self.used_bytes = 0
        self.last_ts_ns = 0

    # ---- opening: recover what is really on disk ---------------------------------------------------------------

    def open(self) -> None:
        """Find where the recording really ends and continue from there, under a new run number."""
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / "state.json"
        self.run = (json.loads(state.read_text())["run"] if state.exists() else 0) + 1
        partial = state.with_name("state.json.partial")
        partial.write_text(json.dumps({"run": self.run}) + "\n")
        os.replace(partial, state)
        for day in reversed(days(self.root)):
            found = segments(day)
            if not found:
                continue
            last = found[-1]
            carry = next((line for line in reversed(read_journal(day)) if line.get("type") == "carry"), {})
            self._tracker = GapTracker.resume(self.pairs, self._calendar, self.settings.gap_threshold_ns, carry)
            if last.suffix != ".gz":
                self._truncate_torn_tail(last)
            # Bring the tracker from the last carry to the end of the data, without journalling gaps twice.
            hour_carried = carry.get("hour") if carry else None
            for path in found:
                if hour_carried is not None and int(path.name[:2]) < int(str(hour_carried)):
                    continue
                header, _, records = read_segment(path)
                for record in records:
                    self._tracker.observe(record, header.pairs)
                    self.next_index, self.last_ts_ns = record.index + 1, record.recv_ts_ns
            break
        self.used_bytes = self._measure()

    @staticmethod
    def _truncate_torn_tail(path: Path) -> None:
        info = inspect(path)
        if path.stat().st_size != info.good_end:
            with path.open("r+b") as stream:
                stream.truncate(info.good_end)
                os.fsync(stream.fileno())

    def _measure(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    # ---- writing ---------------------------------------------------------------------------------------------

    def write(self, kind: int, recv_ts_ns: int, venue_seq: int, pair: str | None = None, *values: int) -> Record:
        """Buffer one record. It is on disk after the block it belongs to has been flushed."""
        self._roll_to(recv_ts_ns)
        pair_id = 0xFFFF if pair is None else self._ids[pair]
        record = Record(self.next_index, recv_ts_ns, venue_seq, kind, pair_id, *values)
        assert self._writer is not None
        self._writer.append(record)
        self.next_index += 1
        self.last_ts_ns = max(self.last_ts_ns, recv_ts_ns)
        for gap in self._tracker.observe(record, self.pairs):
            self.note(gap.as_dict())
        if self._writer.buffered >= self.settings.block_records:
            self.flush()
        return record

    @property
    def buffered(self) -> int:
        return self._writer.buffered if self._writer else 0

    def flush(self) -> None:
        """Put the buffered records on disk as one block, unless that would break the quota."""
        writer = self._writer
        if writer is None or not writer.buffered:
            return
        needed = writer.pending_bytes()
        if self.used_bytes + needed > self.settings.quota_bytes:
            self.used_bytes = self._measure()  # the housekeeping command may have made room since the last look
        if self.used_bytes + needed > self.settings.quota_bytes:
            Housekeeper(self.root, self.settings, self._offload).run(self.last_ts_ns, keep=writer.path)
            self.used_bytes = self._measure()
            if self.used_bytes + needed > self.settings.quota_bytes:
                raise QuotaExceeded(
                    f"the recording is at {self.used_bytes} of {self.settings.quota_bytes} bytes and the next block "
                    f"needs {needed}: nothing more is written until space is made"
                )
        self.used_bytes += writer.flush()

    def close(self) -> None:
        """Flush and close. If the flush is refused by the quota, what was buffered is dropped, not forced in."""
        if self._writer is not None:
            try:
                self.flush()
            finally:
                self._writer.discard()
                self._writer.close()
                self._writer = None

    def note(self, entry: dict[str, object]) -> None:
        """Append a line to the journal of the day being written. The journal is exempt from the quota."""
        day = self._slot[0] if self._slot else day_of(self.last_ts_ns)
        path = self.root / day / JOURNAL
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True) + "\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        self.used_bytes += len(line)

    def silent(self, now_ns: int) -> list[dict[str, object]]:
        return [gap.as_dict() for gap in self._tracker.silent(now_ns)]

    def _roll_to(self, ts_ns: int) -> None:
        slot = (day_of(ts_ns), hour_of(ts_ns))
        if self._slot is not None and slot <= self._slot:
            return  # a clock that steps back does not reopen an hour that is closed
        if self._writer is not None:
            self.flush()
            self._writer.close()
            self._writer = None
        self._slot = slot
        day = self.root / slot[0]
        day.mkdir(parents=True, exist_ok=True)
        self.note({**self._tracker.carry(), "hour": slot[1], "at_ns": ts_ns})
        header = Header(self.run, ts_ns, self.pairs)
        self._writer = SegmentWriter(self._path_for(day, slot[1]), header, fsync=self.settings.fsync)
        self.used_bytes = self._measure()

    def _path_for(self, day: Path, hour: int) -> Path:
        """The segment to continue for this hour, or a new part when the last one cannot be continued."""
        parts = [p for p in segments(day) if int(p.name[:2]) == hour]
        if not parts:
            return day / f"{hour:02d}.seg"
        last = parts[-1]
        if last.suffix != ".gz" and inspect(last).header.pairs == self.pairs:
            return last
        return day / f"{hour:02d}.{len(parts)}.seg"



@dataclass(slots=True)
class HousekeepingReport:
    compressed: int = 0
    handed_over: int = 0
    refused: int = 0  # handed over but not confirmed: kept here


class Housekeeper:
    """Compress closed segments, hand old ones to the offload, and delete only what the offload confirmed."""

    def __init__(self, root: Path, settings: StoreSettings, offload: Offload | None = None) -> None:
        self.root, self.settings, self._offload = root, settings, offload

    def run(self, now_ns: int, *, keep: Path | None = None) -> HousekeepingReport:
        """One pass. A segment is closed when its hour is over; `keep` names one that is still being written."""
        report = HousekeepingReport()
        for day in days(self.root):
            for path in segments(day):
                if path == keep or not self._closed_before(day.name, path, now_ns):
                    continue
                if path.suffix != ".gz" and self.settings.compress:
                    path = self._compress(path)
                    report.compressed += 1
                old = self._closed_before(day.name, path, now_ns - self.settings.keep_ns)
                if self._offload is not None and old:
                    self._hand_over(path, f"{day.name}/{path.name}", report)
            journal = day / JOURNAL
            day_over = day.name < day_of(now_ns - self.settings.keep_ns)
            if self._offload is not None and day_over and not segments(day):
                if journal.exists():
                    self._hand_over(journal, f"{day.name}/{JOURNAL}", report)
                if not any(day.iterdir()):
                    day.rmdir()
        return report

    @staticmethod
    def _closed_before(day: str, path: Path, cutoff_ns: int) -> bool:
        opened = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC, hour=int(path.name[:2]))
        return (int(opened.timestamp()) + 3_600) * SECOND <= cutoff_ns

    @staticmethod
    def _compress(path: Path) -> Path:
        """Replace a closed segment with its gzip, after checking the copy holds the same bytes."""
        target = path.with_name(path.name + ".gz")
        partial = target.with_name(target.name + ".partial")
        with path.open("rb") as read, gzip.open(partial, "wb", compresslevel=6) as write:
            shutil.copyfileobj(read, write)
        digest = hashlib.sha256()
        with gzip.open(partial, "rb") as check:
            for chunk in iter(lambda: check.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != file_sha256(path):
            partial.unlink()
            raise OSError(f"the compressed copy of {path} does not read back the same; the original is kept")
        os.replace(partial, target)
        path.unlink()
        return target

    def _hand_over(self, path: Path, name: str, report: HousekeepingReport) -> None:
        assert self._offload is not None
        digest, size = file_sha256(path), path.stat().st_size
        self._offload.put(path, name)
        if self._offload.confirm(name, digest, size):
            path.unlink()
            report.handed_over += 1
        else:
            report.refused += 1
