"""Reading a recorded day back, in the order and at the times the live process saw it (ADR 0005).

`replay` yields the recorded events one by one and moves a `SimClock` to each event's receive time first.
That is the contract for code that must replay: its "now" is the receive time of the event it is handling,
live and offline alike. The wall-clock moment at which a live process got round to an event is not recorded
and must not enter a decision. `check` re-reads a day and
verifies what a recording promises: every block passes its checksum, the index has no hole, the venue's
sequence never goes back within a run, and the gap journal says exactly what the records imply.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from hftlab.calendar import Calendar
from hftlab.core import Instrument, Quote, SimClock
from hftlab.record import format as fmt
from hftlab.record.gaps import UNEXPLAINED, GapTracker
from hftlab.record.segment import read_segment
from hftlab.record.store import read_day, read_journal, segments
from hftlab.venues.port import ConnectionEvent


def at_ns(event: fmt.RecordedEvent) -> int:
    """When the live process received this event."""
    if isinstance(event, ConnectionEvent):
        return event.ts_ns
    return event.recv_ts_ns


def events(day: Path) -> Iterator[fmt.RecordedEvent]:
    """The recorded events of a day folder, in index order."""
    for record, pairs in read_day(day):
        yield fmt.to_event(record, pairs)


def replay(day: Path, clock: SimClock) -> Iterator[fmt.RecordedEvent]:
    """The same events, with `clock` moved to each one's receive time before it is handed over.

    A wall clock that was corrected backwards during the recording does not move the simulated clock back:
    the event is delivered at the time already reached.
    """
    for event in events(day):
        clock.advance_to(max(clock.now_ns(), at_ns(event)))
        yield event


@dataclass(slots=True)
class CheckReport:
    day: str
    segments: int = 0
    records: int = 0
    quotes: int = 0
    heartbeats: int = 0
    runs: int = 0
    first_index: int | None = None
    last_index: int | None = None
    torn_tail: bool = False
    clock_steps_back: int = 0
    gaps: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def unexplained(self) -> int:
        return self.gaps.get(UNEXPLAINED, 0)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, object]:
        return {
            "day": self.day, "ok": self.ok, "segments": self.segments, "records": self.records,
            "quotes": self.quotes, "heartbeats": self.heartbeats, "runs": self.runs,
            "first_index": self.first_index, "last_index": self.last_index, "torn_tail": self.torn_tail,
            "clock_steps_back": self.clock_steps_back, "gaps": dict(sorted(self.gaps.items())),
            "unexplained_gaps": self.unexplained, "problems": self.problems,
        }  # fmt: skip


def check(day: Path, calendar: Calendar, gap_threshold_ns: int) -> CheckReport:
    """Verify one recorded day. Problems are integrity failures; unexplained gaps are counted, not failures."""
    report = CheckReport(day.name)
    paths = segments(day)
    journal = read_journal(day)
    carry = next((line for line in journal if line.get("type") == "carry"), {})
    tracker: GapTracker | None = None
    found: list[dict[str, object]] = []
    last_index = last_ts = last_seq = None
    for number, path in enumerate(paths, 1):
        try:
            header, scan, records = read_segment(path)
            if tracker is None:
                names = sorted({pair for p in paths for pair in read_segment(p)[0].pairs})
                tracker = GapTracker.resume(names, calendar, gap_threshold_ns, carry)
            for record in records:
                if last_index is not None and record.index != last_index + 1:
                    report.problems.append(f"{path.name}: index {record.index} follows {last_index}")
                if last_ts is not None and record.recv_ts_ns < last_ts:
                    report.clock_steps_back += 1
                if record.kind == fmt.START:
                    report.runs, last_seq = report.runs + 1, None
                elif record.venue_seq:
                    if last_seq is not None and record.venue_seq <= last_seq:
                        report.problems.append(f"{path.name}: venue sequence {record.venue_seq} follows {last_seq}")
                    last_seq = record.venue_seq
                report.quotes += record.kind == fmt.QUOTE
                report.heartbeats += record.kind == fmt.HEARTBEAT
                report.first_index = record.index if report.first_index is None else report.first_index
                last_index, last_ts = record.index, record.recv_ts_ns
                found += [gap.as_dict() for gap in tracker.observe(record, header.pairs)]
            report.records += scan.records
            if scan.torn:
                report.torn_tail = True
                if number != len(paths):
                    report.problems.append(f"{path.name}: a torn block in a segment that is not the last")
        except fmt.CorruptSegment as exc:
            report.problems.append(f"{path.name}: {exc}")
    report.segments, report.last_index = len(paths), last_index
    journalled = [line for line in journal if line.get("type") == "gap"]
    if journalled != found:
        report.problems.append(
            f"the gap journal has {len(journalled)} gap(s) and the records imply {len(found)}: they do not match"
        )
    report.gaps = dict(Counter(str(gap["reason"]) for gap in found))
    return report


def instruments(day: Path) -> dict[str, Instrument]:
    """What the day's journal says its instruments are. A day that describes one pair two ways cannot be priced."""
    out: dict[str, Instrument] = {}
    for line in read_journal(day):
        listed = line.get("instruments") if line.get("type") == "instruments" else None
        for entry in listed if isinstance(listed, list) else []:
            instrument = fmt.instrument_of(entry)
            if out.setdefault(instrument.pair, instrument) != instrument:
                raise fmt.CorruptSegment(f"{day.name}: the journal describes {instrument.pair} in two different ways")
    return out


def last_quotes(day: Path) -> dict[str, Quote]:
    """The last quote of each pair in a day: a small convenience for tools that only need where the day ended."""
    out: dict[str, Quote] = {}
    for event in events(day):
        if isinstance(event, Quote):
            out[event.pair] = event
    return out
