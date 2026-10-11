"""Datasets: feature rows joined to labels, under the one rule that matters (ADR 0020).

    A row's features were all known strictly before its label window starts,
    and a label is usable for training only by a decision made after its window has ended.

The first half is checked here, by the builder itself, on every dataset it makes:

  the stamp     a row made on event `n` says when it became known. That must be event `n`'s own time: a row
                that says it was known later was made from a later event, whatever it is attached to.
  the cut       the stream is cut after an event and the features are computed twice more: from the stream
                with nothing after the cut, and from the stream with everything after the cut changed (other
                prices, other sizes, later times). Every row up to the cut must come out exactly the same both
                times. A feature that looked one event ahead cannot give the same row when that event is gone
                or different. Cuts are taken at up to `checks` places spread over the stream, every place when
                the stream is that short.

A dataset that fails either is refused with `LeakError`; there is no setting that lets one through. The second
half is carried by every row as `end_ns`, the time its label's window ends, and is enforced by the splitter
(`hftlab.learn.validate`). A decision whose label window has not ended inside the recording has no row at all.

That is the only reason a decision has no row. Whether an order would have filled is part of what happened
after the decision, and the trades that could not be closed are the expensive ones: leaving them out would
make every evaluation look better than the trading it stands for. So a row is kept and says how each side was
priced (`buy_how`, `sell_how`, the codes of `hftlab.labels`): an entry that would not have filled is no trade,
worth exactly nothing; an exit that would not have filled at the horizon is priced at the first later moment
the shadow venue would have filled it, within the label's wait; and past that wait at the label's stated
worst case. The dataset counts each kind (`counted`), and so does every report made from it.

A dataset is made per pair and horizon from day folders of the recorder's store. It records what it was made
from: the feature keys, the label spec, the cost model's parameters with its placeholder flag, the source days
and a hash of all of it and every row. Everything in it is an integer or text. Nothing here is committed: a
dataset is derived data and stays on the host.

It also records where each of those days came from (ADR 0028): this platform's own recording, or history
imported from files under a label. That is in the hash, so two datasets from different mixes of sources are
two datasets, and `split` says of any of its rows how many came from which. A day whose origin nobody stated
is counted as `unstated`, never as recorded: only a day folder that says it is the platform's own is. A day
whose sizes are an importer's placeholder makes the whole dataset `placeholder_sizes`, and nothing fitted to
it a result.
"""
from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from hftlab.calendar import Calendar, last_rollover, next_close
from hftlab.core import Instrument, Quote
from hftlab.features import FeatureBuilder, FeatureConfig, FeatureRow
from hftlab.journal import canonical
from hftlab.labels import AT_HORIZON as AT_HORIZON
from hftlab.labels import LATE_EXIT as LATE_EXIT
from hftlab.labels import NO_ENTRY as NO_ENTRY
from hftlab.labels import PENALISED as PENALISED
from hftlab.labels import Labeller, LabelSpec, PairTape
from hftlab.record.format import RecordedEvent
from hftlab.record.origin import origin_of
from hftlab.replay.reader import events as day_events
from hftlab.replay.reader import instruments as day_instruments
from hftlab.venues.port import ConnectionEvent
from hftlab.venues.sim.cost import CostModel

VERSION: Final = 3
UNSTATED: Final = "unstated"  # the source of events no day folder vouched for
HOW: Final = {AT_HORIZON: "at_horizon", NO_ENTRY: "no_entry", LATE_EXIT: "late_exit", PENALISED: "penalised"}  # by code
CHECKS: Final = 16
DISTURB_NS: Final = 3_600 * 1_000_000_000 + 7
DISTURB_PRICE: Final = 1_009


class LeakError(ValueError):
    """A feature row holds, or claims, something that was not known at its decision. The dataset is not made."""


class DatasetError(ValueError):
    """The recording cannot be made into a dataset: no such pair, or days that disagree about it."""


class FeatureSource(Protocol):
    """Anything that turns a stream into feature rows. The builder trusts none of it: see `LeakError`."""

    pair: str

    @property
    def keys(self) -> tuple[str, ...]: ...

    def rows(self, events: Iterable[RecordedEvent]) -> Iterable[FeatureRow]: ...


@dataclass(frozen=True, slots=True)
class DaySource:
    """One day of the stream a dataset is made from: where its events came from, and how many it gave."""

    day: str
    source: str  # `recorded`, `imported:<label>` (hftlab.record.origin) or UNSTATED
    events: int
    placeholder_sizes: bool = False  # its sizes are an importer's placeholder, not anything a venue displayed

    def plain(self) -> list[object]:
        return [self.day, self.source, self.events, int(self.placeholder_sizes)]


def day_source(folder: Path, events: int) -> DaySource:
    """A day folder of a store as a source: what it says it is (`hftlab.record.origin`), and its events."""
    origin = origin_of(folder)
    return DaySource(folder.name, origin.key, events, origin.placeholder_sizes)


@dataclass(frozen=True, slots=True)
class Row:
    n: int  # the event the decision was made on
    known_ns: int  # when the features were known: that event's receive time
    end_ns: int  # when the label's window ends; the label may train no decision made before this
    day_ns: int  # the rollover that began the decision's trading day
    week_ns: int  # the Friday close that ends its trading week
    features: tuple[int, ...]
    buy_micro: int  # zero where the entry would not have filled: no trade
    sell_micro: int
    buy_barrier: int | None = None
    sell_barrier: int | None = None
    buy_how: int = AT_HORIZON  # how the outcome was priced: AT_HORIZON, NO_ENTRY, LATE_EXIT or PENALISED
    sell_how: int = AT_HORIZON

    def plain(self) -> list[object]:
        head = [self.n, self.known_ns, self.end_ns, self.day_ns, self.week_ns]
        tail = [self.buy_barrier, self.sell_barrier, self.buy_how, self.sell_how]
        return [*head, list(self.features), self.buy_micro, self.sell_micro, *tail]


@dataclass(frozen=True, slots=True)
class Dataset:
    pair: str
    features: tuple[str, ...]  # the feature keys, in the order of a row's values
    label: Mapping[str, int]
    cost_model: Mapping[str, object]
    size: int  # the order size the outcomes are for
    window_ns: int  # from a decision to the end of its label's window
    days: tuple[str, ...]  # the day folders it was made from
    events: int
    dropped: Mapping[str, int]  # feature rows that became no dataset row, by reason
    counted: Mapping[str, int]  # outcomes that were not a fill at both ends as sent, by how they were priced
    rows: tuple[Row, ...]
    content_hash: str
    sources: tuple[DaySource, ...] = ()  # the stream, day by day and in order: every event is in exactly one

    @property
    def placeholder(self) -> bool:
        """Were the outcomes priced on placeholder costs? Then nothing fitted to them is a result."""
        return self.cost_model.get("placeholder") is not False

    @property
    def placeholder_sizes(self) -> bool:
        """Does any of its days carry an importer's placeholder for a size? Then nothing fitted to it is a result."""
        return any(source.placeholder_sizes for source in self.sources)

    def split(self, positions: Iterable[int] | None = None) -> dict[str, dict[str, int]]:
        """Of the rows at `positions` (all of them by default): how many rows, and days, came from each source.

        A row belongs to the day its decision's event came from, and a day is counted when it gave a row.
        """
        ends, total = [], 0
        for source in self.sources:
            total += source.events
            ends.append(total)
        rows: dict[str, int] = {}
        days: dict[str, set[int]] = {}
        for position in range(len(self.rows)) if positions is None else positions:
            number = bisect_left(ends, self.rows[position].n)
            key = self.sources[number].source
            rows[key] = rows.get(key, 0) + 1
            days.setdefault(key, set()).add(number)
        return {"rows": dict(sorted(rows.items())), "days": {key: len(days[key]) for key in sorted(days)}}

    @property
    def horizon_ns(self) -> int:
        return self.label["horizon_ns"]

    def describe(self) -> dict[str, object]:
        """Everything but the rows, as plain values."""
        return {
            "kind": "dataset",
            "version": VERSION,
            "pair": self.pair,
            "features": list(self.features),
            "label": dict(self.label),
            "cost_model": dict(self.cost_model),
            "placeholder": self.placeholder,
            "size": self.size,
            "window_ns": self.window_ns,
            "days": list(self.days),
            "sources": {
                "days": [source.plain() for source in self.sources],
                "placeholder_sizes": int(self.placeholder_sizes),
                "split": self.split(),
            },
            "events": self.events,
            "dropped": dict(self.dropped),
            "counted": dict(self.counted),
            "rows": len(self.rows),
            "content_hash": self.content_hash,
        }


def cuts(length: int, checks: int) -> list[int]:
    """Where the stream is cut for the look-ahead check: every place when it is short, else spread evenly."""
    if length <= 1 or checks <= 0:
        return []
    if length - 1 <= checks:
        return list(range(1, length))
    if checks == 1:
        return [length // 2]
    return sorted({1 + (length - 2) * k // (checks - 1) for k in range(checks)})


def disturbed(event: RecordedEvent) -> RecordedEvent:
    """The same kind of event, an hour later, at another price and with the sizes the other way round."""
    if isinstance(event, ConnectionEvent):
        return replace(event, ts_ns=event.ts_ns + DISTURB_NS)
    if isinstance(event, Quote):
        return replace(
            event, bid=event.bid + DISTURB_PRICE, ask=event.ask + 2 * DISTURB_PRICE, bid_size=event.ask_size + 1,
            ask_size=event.bid_size + 2, recv_ts_ns=event.recv_ts_ns + DISTURB_NS,
        )  # fmt: skip
    return replace(event, recv_ts_ns=event.recv_ts_ns + DISTURB_NS)


def check_causal(
    source: FeatureSource, events: Sequence[RecordedEvent], rows: Sequence[FeatureRow], checks: int = CHECKS
) -> None:
    """Refuse feature rows that were, or say they were, made from anything after their own event."""
    tape = PairTape(events, source.pair)
    width, last = len(source.keys), 0
    for row in rows:
        if not last < row.n <= len(events) or row.pair != source.pair or len(row.values) != width:
            raise LeakError(f"row {row.n} is out of order, of another pair, or not {width} values wide")
        if row.known_ns != tape.at[row.n - 1]:
            raise LeakError(
                f"row {row.n} says it was known at {row.known_ns}, and event {row.n} was received at "
                f"{tape.at[row.n - 1]}: it was not made from the events up to its own"
            )
        last = row.n
    for cut in cuts(len(events), checks):
        known = [row for row in rows if row.n <= cut]
        before = list(events[:cut])
        without = list(source.rows(before))
        changed = [row for row in source.rows([*before, *(disturbed(event) for event in events[cut:])]) if row.n <= cut]
        if without != known or changed != known:
            what = "taken away" if without != known else "changed"
            raise LeakError(
                f"the rows up to event {cut} change when the events after it are {what}: a feature reads the future"
            )


def build(
    events: Sequence[RecordedEvent],
    source: FeatureSource,
    labeller: Labeller,
    *,
    days: Sequence[str] = (),
    sources: Sequence[DaySource] = (),
    checks: int = CHECKS,
) -> Dataset:
    """The dataset of one stream: every feature row that has a whole label, after the look-ahead check.

    `sources` says, day by day and in the stream's order, where the events came from. Without it the whole
    stream is `unstated`: a caller that does not say where its events are from does not get them counted as
    the platform's own.
    """
    if source.pair != labeller.instrument.pair:
        raise DatasetError(f"features of {source.pair} cannot be joined to labels of {labeller.instrument.pair}")
    if sources:
        if sum(one.events for one in sources) != len(events) or (days and list(days) != [one.day for one in sources]):
            raise DatasetError("the stated sources do not account for every event of the stream, day by day")
        days = [one.day for one in sources]
    spans = tuple(sources) or (DaySource("", UNSTATED, len(events)),)
    found = list(source.rows(events))
    check_causal(source, events, found, checks)
    tape = PairTape(events, source.pair)
    dropped = {"window_not_ended": 0}
    counted = {HOW[how]: 0 for how in (NO_ENTRY, LATE_EXIT, PENALISED)}  # by side: a row has two outcomes
    rows: list[Row] = []
    for row in found:
        label = labeller.label(tape, row.n)
        if label.n != row.n or label.decided_ns < row.known_ns or label.end_ns < label.decided_ns:
            raise LeakError(f"the label of row {row.n} is for another decision, or ends before it starts")
        if label.end_ns >= tape.last_ns:
            dropped["window_not_ended"] += 1  # the recording stops before the label can be known
        else:
            up, down = label.buy_barrier, label.sell_barrier
            day, week = last_rollover(row.known_ns), next_close(row.known_ns)
            hits = (up.hit if up else None, down.hit if down else None)
            how = (label.buy_how, label.sell_how)
            if (label.buy_micro is None) != (how[0] == NO_ENTRY) or (label.sell_micro is None) != (how[1] == NO_ENTRY):
                raise LeakError(f"the label of row {row.n} has an outcome without a trade, or a trade without one")
            for one in how:
                if one != AT_HORIZON:
                    counted[HOW[one]] += 1
            outcomes = (label.buy_micro or 0, label.sell_micro or 0)
            rows.append(Row(row.n, row.known_ns, label.end_ns, day, week, row.values, *outcomes, *hits, *how))
    body: dict[str, object] = {
        "kind": "dataset",
        "version": VERSION,
        "pair": source.pair,
        "features": list(source.keys),
        "label": labeller.spec.as_dict(),
        "cost_model": labeller.model.as_dict(),
        "size": labeller.size,
        "window_ns": labeller.window_ns,
        "days": list(days),
        "sources": [one.plain() for one in spans],
        "events": len(events),
        "dropped": dropped,
        "counted": counted,
        "rows": [row.plain() for row in rows],
    }
    digest = hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
    return Dataset(
        pair=source.pair,
        features=tuple(source.keys),
        label=MappingProxyType(labeller.spec.as_dict()),
        cost_model=MappingProxyType(labeller.model.as_dict()),
        size=labeller.size,
        window_ns=labeller.window_ns,
        days=tuple(days),
        events=len(events),
        dropped=MappingProxyType(dropped),
        counted=MappingProxyType(counted),
        rows=tuple(rows),
        content_hash=digest,
        sources=spans,
    )


def build_for(
    events: Sequence[RecordedEvent], instrument: Instrument, config: FeatureConfig, model: CostModel,
    calendar: Calendar, *, horizon_ns: int, exit_wait_ns: int, stuck_increments: int, days: Sequence[str] = (),
    sources: Sequence[DaySource] = (), checks: int = CHECKS,
) -> Dataset:  # fmt: skip
    """The dataset of one pair and horizon from a stream already read, with the wait and the worst case written out."""
    spec = LabelSpec(horizon_ns, exit_wait_ns=exit_wait_ns, stuck_increments=stuck_increments)
    source = FeatureBuilder(instrument.pair, config, calendar)
    labeller = Labeller(spec, instrument, model, calendar)
    return build(events, source, labeller, days=days, sources=sources, checks=checks)


def build_from_days(
    folders: Sequence[Path],
    pair: str,
    config: FeatureConfig,
    spec: LabelSpec,
    model: CostModel,
    calendar: Calendar,
    *,
    checks: int = CHECKS,
) -> Dataset:
    """The dataset of one pair and horizon from day folders of the store, read in name order as one stream."""
    ordered = sorted(folders, key=lambda folder: folder.name)
    if not ordered:
        raise DatasetError("a dataset needs at least one recorded day")
    instrument = None
    for folder in ordered:
        described = day_instruments(folder).get(pair)
        if described is None:
            raise DatasetError(f"{folder.name} does not describe {pair}; its prices cannot be read")
        if instrument is not None and described != instrument:
            raise DatasetError(f"{folder.name} describes {pair} differently from the days before it")
        instrument = described
    assert instrument is not None
    stream: list[RecordedEvent] = []
    sources = []
    for folder in ordered:
        before = len(stream)
        stream += day_events(folder)
        sources.append(day_source(folder, len(stream) - before))
    source = FeatureBuilder(pair, config, calendar)
    labeller = Labeller(spec, instrument, model, calendar)
    return build(stream, source, labeller, sources=sources, checks=checks)
