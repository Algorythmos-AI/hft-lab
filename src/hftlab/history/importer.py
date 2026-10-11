"""The import: a folder of described tick files into a store of imported history (ADR 0028).

  <root>/import-store.json          says what this folder is; the recorder refuses a folder that has it
  <root>/<YYYY-MM-DD>/<HH>.seg      the recorder's format (ADR 0012), with run number 0, which no recorder writes
  <root>/<YYYY-MM-DD>/journal.jsonl the day's provenance first, then what a recorded day's journal holds

It reads files from a disk and writes files to a disk. It opens no connection, holds no address of anybody's
server and has no clock: what it writes is a function of the files, the description, the label and the
calendar, and of nothing else. So:

  * the same inputs give the same bytes, in every segment and every journal;
  * a day already in the store is compared with what this run would write. The same bytes: nothing is touched.
    Anything else (a file that changed, another label, a pair more or less) is refused, and the day in the store
    stays as it is. A day is never merged and never written over; to import it differently, the owner removes
    its folder first;
  * a refusal of any file or any day refuses the run. Days are built in a staging folder and moved into place
    only when all of them can be: a run that is refused, or killed, leaves the days in the store as they were.

A day's journal opens with its provenance: `source: imported`, the owner's label, `terms_verified: false`, the
parser and its version, the stated time zone, everything else that was stated, and the name, pair and SHA-256
of every input file that had a row in that day. Then a carry line that starts the gap tracker connected and
with no history (a day is checked by itself, so a silence that runs over midnight is not counted), the
instruments, the gaps, the rows dropped by file and reason, and a count.

A record's venue sequence is its own number in the day plus one. There is no heartbeat, no connection record
and no start: nothing here pretends a recorder ran. A reader that wants a book confirmed finds it confirmed
only by quotes, which is the less flattering reading.
"""
from __future__ import annotations

import heapq
import json
import os
import shutil
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from hftlab.calendar import Calendar
from hftlab.history.describe import Described, Description, DescriptionError, load
from hftlab.history.parsers import PARSERS, Refused
from hftlab.history.rows import DROPPED, PLACEHOLDER_SIZE, Tally, Tick, read_file
from hftlab.record.format import QUOTE, Header, Record, describe
from hftlab.record.gaps import GapTracker
from hftlab.record.origin import IMPORT_RUN, IMPORTED, PLACEHOLDER, STATED, origin_of, source_label
from hftlab.record.segment import SegmentWriter
from hftlab.record.store import IMPORT_MARKER, JOURNAL, PROVENANCE, day_of, days, file_sha256, hour_of

VERSION: Final = 1
SECOND: Final = 1_000_000_000
BLOCK_RECORDS: Final = 256
STAGING: Final = ".staging"
GAPS: Final = "gaps.partial"
RECORDERS_OWN: Final = ("state.json", "status.json")  # what the recorder leaves at the root of its store
WRITTEN, UNCHANGED, NOTHING_KEPT, WOULD_WRITE, EXISTS = "written", "unchanged", "nothing_kept", "would_write", "exists"
_MARKER: Final = {
    "kind": "import-store",
    "version": VERSION,
    "terms_verified": False,
    "note": "imported history, read from files the owner exported; not this platform's recording (ADR 0028)",
}
_CARRY: Final = {"type": "carry", "connected": True, "last_data_ns": {}, "lost": [], "down": []}


class ImportRefused(Exception):
    """The import will not be made as things are. Nothing was written, and the store is as it was."""


def check_root(root: Path, recorder_root: Path | None) -> None:
    """Refuse a root that is, or may be, the recorder's store. Nothing imported is ever written beside a recording."""
    if recorder_root is not None and root.resolve() == recorder_root.resolve():
        raise ImportRefused(f"{root} is where the recorder writes; imported history has a store of its own")
    if not root.exists():
        return
    if not root.is_dir():
        raise ImportRefused(f"{root} is not a folder")
    if found := [name for name in RECORDERS_OWN if (root / name).exists()]:
        raise ImportRefused(f"{root} holds {', '.join(found)}: it is a recorder's store; nothing is imported into one")
    if recorded := [day.name for day in days(root) if not origin_of(day).imported]:
        raise ImportRefused(f"{root} holds recorded days ({', '.join(recorded[:3])}): nothing is imported beside them")
    if not (root / IMPORT_MARKER).exists() and any(entry.name != STAGING for entry in root.iterdir()):
        raise ImportRefused(f"{root} is not empty and is not an import store; choose a folder of its own")


@dataclass(slots=True)
class _Day:
    """One UTC day being built: its segments as they are written, and what its journal will need."""

    name: str
    folder: Path | None  # None on a dry run: everything is counted and nothing is written
    tracker: GapTracker
    index: int = 0
    hour: int = -1
    buffer: list[tuple[str, Tick]] = field(default_factory=list)
    pairs: Counter[str] = field(default_factory=Counter)
    kept: Counter[str] = field(default_factory=Counter)  # by file name
    gaps: Counter[str] = field(default_factory=Counter)
    first_ns: int = 0
    _gap_lines: IO[bytes] | None = None

    def add(self, pair: str, name: str, tick: Tick) -> None:
        hour = hour_of(tick.ts_ns)
        if hour != self.hour:
            self.flush()
            self.hour = hour
        if not self.index and not self.buffer:
            self.first_ns = tick.ts_ns
        self.buffer.append((pair, tick))
        self.pairs[pair] += 1
        self.kept[name] += 1

    def flush(self) -> None:
        """Write the hour in hand as one segment. Its pair table is the pairs that hour holds, in name order."""
        if not self.buffer:
            return
        pairs = tuple(sorted({pair for pair, _ in self.buffer}))
        numbers = {pair: number for number, pair in enumerate(pairs)}
        writer = None
        if self.folder is not None:
            header = Header(IMPORT_RUN, self.buffer[0][1].ts_ns, pairs)
            writer = SegmentWriter(self.folder / f"{self.hour:02d}.seg", header, fsync=False)
            if self._gap_lines is None:
                self._gap_lines = (self.folder / GAPS).open("wb")
        try:
            for pair, tick in self.buffer:
                book = (tick.bid, tick.ask, tick.bid_size, tick.ask_size)
                record = Record(self.index, tick.ts_ns, self.index + 1, QUOTE, numbers[pair], *book)
                self.index += 1
                for gap in self.tracker.observe(record, pairs):
                    self.gaps[gap.reason] += 1
                    if self._gap_lines is not None:
                        self._gap_lines.write(_line(gap.as_dict()))
                if writer is not None:
                    writer.append(record)
                    if writer.buffered >= BLOCK_RECORDS:
                        writer.flush()
        finally:
            if writer is not None:
                writer.close()
        self.buffer = []

    def close(self) -> None:
        self.flush()
        if self._gap_lines is not None:
            self._gap_lines.close()
            self._gap_lines = None


def _line(entry: dict[str, object]) -> bytes:
    return (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")


def _lines(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        yield from stream


@dataclass(frozen=True, slots=True)
class Imported:
    """What a run did, as the command prints it. `days` is one entry per UTC day any file had a row in."""

    label: str
    store: str
    dry_run: bool
    files: tuple[dict[str, object], ...]
    days: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        statuses = Counter(str(day["status"]) for day in self.days)
        return {
            "kind": "import-report",
            "version": VERSION,
            "source": IMPORTED,
            "label": self.label,
            "terms_verified": False,
            "store": self.store,
            "dry_run": self.dry_run,
            "rows": {
                "read": sum(int(str(one["rows_read"])) for one in self.files),
                "kept": sum(int(str(one["rows_kept"])) for one in self.files),
            },
            "days_by_status": dict(sorted(statuses.items())),
            "files": list(self.files),
            "days": list(self.days),
        }


class Importer:
    def __init__(
        self, root: Path, calendar: Calendar, *, recorder_root: Path | None = None, gap_threshold_ns: int = 5 * SECOND
    ) -> None:
        self.root, self._calendar, self._recorder_root = root, calendar, recorder_root
        self._threshold = gap_threshold_ns

    def run(self, folder: Path, label: str, *, dry_run: bool = False) -> Imported:
        """Import every file the folder's description lists. Raises `ImportRefused`, having written nothing."""
        try:
            label = source_label(label)
            description = load(folder)
        except (ValueError, DescriptionError) as exc:
            raise ImportRefused(str(exc)) from exc
        check_root(self.root, self._recorder_root)
        staging = self.root / STAGING
        if not dry_run:
            self.root.mkdir(parents=True, exist_ok=True)
            marker = self.root / IMPORT_MARKER
            if not marker.exists():
                marker.write_bytes(_line(dict(_MARKER)))
            shutil.rmtree(staging, ignore_errors=True)  # what a killed run left: never part of the store
            staging.mkdir()
        try:
            return self._run(folder, description, label, None if dry_run else staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _run(self, folder: Path, description: Description, label: str, staging: Path | None) -> Imported:
        parser = PARSERS[description.layout]
        hashes = {one.name: file_sha256(one.path) for one in description.files}
        tallies = {one.name: Tally() for one in description.files}
        order = sorted(description.instruments)

        def stream(rank: int, one: Described) -> Iterator[tuple[int, str, int, int, Tick]]:
            instrument = description.instruments[one.pair]
            try:
                kept = read_file(
                    _lines(one.path), parser, instrument, description, self._calendar, day_of, tallies[one.name]
                )
                for tick in kept:
                    yield tick.ts_ns, one.pair, rank, tick.line, tick
            except Refused as exc:
                raise ImportRefused(f"{one.name}: {exc}. The file is not what {folder.name}/import.yaml says") from exc
            except OSError as exc:
                raise ImportRefused(f"{one.name} cannot be read: {exc}") from exc

        built: dict[str, _Day] = {}
        day: _Day | None = None
        for at, pair, rank, _, tick in heapq.merge(*(stream(n, one) for n, one in enumerate(description.files))):
            name = day_of(at)
            if day is None or day.name != name:
                if day is not None:
                    day.close()
                where = staging / name if staging is not None else None
                if where is not None:
                    where.mkdir()
                tracker = GapTracker.resume(order, self._calendar, self._threshold, _CARRY)
                day = built[name] = _Day(name, where, tracker)
            day.add(pair, description.files[rank].name, tick)
        if day is not None:
            day.close()

        sizes = STATED if description.has_sizes else PLACEHOLDER
        reported: list[dict[str, object]] = []
        seen = sorted({name for tally in tallies.values() for name in tally.seen})
        for name in seen:
            made = built.get(name)
            inputs = [
                {
                    "name": one.name, "sha256": hashes[one.name], "pair": one.pair,
                    "rows_read": tallies[one.name].seen[name], "rows_kept": made.kept[one.name] if made else 0,
                }
                for one in description.files if tallies[one.name].seen[name]
            ]  # fmt: skip
            dropped = [
                {"type": "dropped", "file": one.name, "reason": reason, "rows": tallies[one.name].dropped[reason][name]}
                for one in description.files for reason in DROPPED
                if tallies[one.name].dropped.get(reason, Counter())[name]
            ]  # fmt: skip
            by_reason: Counter[str] = Counter()
            for entry in dropped:
                by_reason[str(entry["reason"])] += int(str(entry["rows"]))
            said: dict[str, object] = {"day": name, "rows": 0, "dropped": dict(sorted(by_reason.items()))}
            if made is None:
                reported.append({**said, "status": NOTHING_KEPT})
                continue
            said |= {"rows": made.index, "pairs": dict(sorted(made.pairs.items())), "sizes": sizes}
            said["gaps"] = dict(sorted(made.gaps.items()))
            if made.folder is not None:
                provenance: dict[str, object] = {
                    "type": PROVENANCE, "source": IMPORTED, "label": label, "terms_verified": False,
                    "importer_version": VERSION, "parser": parser.name, "parser_version": parser.version,
                    "time_zone": description.time_zone, "sizes": sizes,
                    "placeholder_size": None if description.has_sizes else PLACEHOLDER_SIZE,
                    "stated": description.stated(made.pairs), "inputs": inputs,
                }  # fmt: skip
                instruments = [describe(description.instruments[pair]) for pair in sorted(made.pairs)]
                count = {"type": "imported", "rows": made.index, "pairs": dict(sorted(made.pairs.items()))}
                with (made.folder / JOURNAL).open("wb") as journal:
                    journal.write(_line(provenance))
                    journal.write(_line({**_CARRY, "hour": hour_of(made.first_ns), "at_ns": made.first_ns}))
                    journal.write(_line({"type": "instruments", "instruments": instruments}))
                    for line in _lines(made.folder / GAPS):
                        journal.write(line)
                    for entry in dropped:
                        journal.write(_line(entry))
                    journal.write(_line(count))
                (made.folder / GAPS).unlink()
            reported.append(said)

        files = tuple(
            {
                "name": one.name, "sha256": hashes[one.name], "pair": one.pair, "parser": parser.name,
                "parser_version": parser.version, "rows_read": tallies[one.name].read,
                "rows_kept": tallies[one.name].kept, "dropped": tallies[one.name].dropped_by_reason(),
                "counted": dict(sorted(tallies[one.name].counted.items())),
            }
            for one in description.files
        )  # fmt: skip
        self._place(staging, reported)
        return Imported(label, str(self.root), staging is None, files, tuple(reported))

    def _place(self, staging: Path | None, reported: list[dict[str, object]]) -> None:
        """Say what happens to each day that was built, and, unless a day is refused, move the new ones into place."""
        new, differing = [], []
        for said in reported:
            if "status" in said:
                continue
            final = self.root / str(said["day"])
            if staging is None:
                said["status"] = EXISTS if final.exists() else WOULD_WRITE
            elif not final.exists():
                said["status"] = WRITTEN
                new.append(staging / final.name)
            elif _same(staging / final.name, final):
                said["status"] = UNCHANGED
            else:
                differing.append(final.name)
        if differing:
            raise ImportRefused(
                f"already imported from other files, under another label or described differently: "
                f"{', '.join(differing)}. A day in the store is never merged or written over; nothing was written"
            )
        for folder in new:
            _sync(folder)
            os.replace(folder, self.root / folder.name)
        if new:
            _sync_folder(self.root)


def _same(one: Path, other: Path) -> bool:
    """Do two day folders hold the same files with the same bytes?"""
    names = sorted(path.name for path in one.iterdir())
    if names != sorted(path.name for path in other.iterdir()):
        return False
    if not all((folder / name).is_file() for folder in (one, other) for name in names):
        return False
    return all(file_sha256(one / name) == file_sha256(other / name) for name in names)


def _sync_folder(folder: Path) -> None:
    handle = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _sync(folder: Path) -> None:
    """Put a built day on the disk before it is given its name in the store."""
    for path in folder.iterdir():
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    _sync_folder(folder)
