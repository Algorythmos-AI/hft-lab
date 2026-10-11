"""`python -m hftlab.feasibility --store <dir>`: the feasibility map of what the recorder stored, as JSON.

    python -m hftlab.feasibility --store var/record                          every recorded day, as one stream
    python -m hftlab.feasibility --store var/record --day 2026-10-14         one day (several may be named)
    python -m hftlab.feasibility --store var/record --out var/feasibility.json
    python -m hftlab.feasibility --store var/record --imports var/history    imported days as well (ADR 0028)

It reads the day folders, the cost model and the calendar, and writes the map to --out, or prints it. It holds
no venue and sends nothing anywhere. The map carries the cost model's parameters and says in itself whether
they are placeholders; while they are, no cell of it is a result.

The store may be the recorder's or one of imported history (ADR 0028): the format is the same and the
evidence is not. So the map says under `sources` how many of its days and of its events came from the
platform's own recordings (`recorded`) and how many from imports (`imported:<label>`), and the command says it
again on its own line. A day whose sizes are an importer's placeholder is named: no window of such a day can
be filled, and the map is then no result either. With --imports the days of an import store are read beside
the store's, in date order; a date both hold is read from --store.

Exit codes: 0 written, 2 the store, a day or a setting could not be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

from hftlab.calendar import CalendarError, load_calendar
from hftlab.core import Instrument
from hftlab.feasibility.map import FeasibilityError, feasibility_map, load_settings
from hftlab.record.format import CorruptSegment, RecordedEvent
from hftlab.record.origin import origin_of
from hftlab.record.store import days
from hftlab.replay.reader import events, instruments
from hftlab.venues.sim.cost import CostModelError, load_cost_model

CONFIG = Path("config/feasibility.yaml")
COSTS = Path("config/cost_model.yaml")
CALENDAR = Path("config/calendar")


class Split(NamedTuple):
    """Where a map's days and events came from: `recorded`, or `imported:` and the owner's label."""

    days: dict[str, int]
    events: dict[str, int]
    placeholder_size_days: list[str]

    def plain(self) -> dict[str, object]:
        events = dict(sorted(self.events.items()))
        return {"days": self.days, "events": events, "placeholder_size_days": self.placeholder_size_days}


def recorded(
    store: Path, names: Sequence[str], imports: Path | None = None
) -> tuple[list[Path], dict[str, Instrument], Iterator[RecordedEvent], Split]:
    """The chosen day folders of a store, what they say their instruments are, their events as one stream, and

    where they came from. The counts of events by source are complete once the stream has been read to its end.
    """
    found = days(store)
    taken = {day.name for day in found}
    more = [day for day in (days(imports) if imports is not None else []) if day.name not in taken]
    found = sorted([*found, *more], key=lambda day: day.name)
    if not found:
        raise FeasibilityError(f"{store} holds no recorded day")
    chosen = [day for day in found if day.name in names] if names else found
    if missing := sorted(set(names) - {day.name for day in chosen}):
        raise FeasibilityError(f"{store} has no day {', '.join(missing)}")
    described: dict[str, Instrument] = {}
    for day in chosen:
        for pair, instrument in instruments(day).items():
            if described.setdefault(pair, instrument) != instrument:
                raise FeasibilityError(f"{day.name} describes {pair} differently from the days before it")
    if not described:
        raise FeasibilityError("the recording does not say what its instruments are; it cannot be priced")

    origins = [origin_of(day) for day in chosen]
    by_days: dict[str, int] = {}
    by_events: dict[str, int] = {}
    for origin in origins:
        by_days[origin.key] = by_days.get(origin.key, 0) + 1
        by_events.setdefault(origin.key, 0)
    unsized = [day.name for day, origin in zip(chosen, origins, strict=True) if origin.placeholder_sizes]
    sources = Split(dict(sorted(by_days.items())), by_events, unsized)

    def stream() -> Iterator[RecordedEvent]:
        for day, origin in zip(chosen, origins, strict=True):
            for event in events(day):
                by_events[origin.key] += 1
                yield event

    return chosen, described, stream(), sources


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.feasibility", description="Map moves against costs.")
    parser.add_argument("--store", type=Path, required=True, help="the recorder's root: a folder of day folders")
    parser.add_argument("--imports", type=Path, help="a store of imported history to read as well (ADR 0028)")
    parser.add_argument("--day", nargs="+", default=[], help="day folder name(s) to read (default: all of them)")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--cost-model", type=Path, default=COSTS)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--out", type=Path, help="where to write the map (default: print it)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        model = load_cost_model(args.cost_model)
        calendar = load_calendar(args.calendar)
        chosen, described, stream, sources = recorded(args.store, args.day, args.imports)
        result = feasibility_map(stream, described, model, calendar, settings)
    except (FeasibilityError, CostModelError, CalendarError, CorruptSegment) as exc:
        print(f"feasibility: {exc}", file=sys.stderr)
        return 2
    if result["placeholder"]:
        print("feasibility: PLACEHOLDER costs or rule; no cell of this map measures anything", file=sys.stderr)
    if sources.placeholder_size_days:
        result["placeholder"] = True
        result["note"] = "NOT A RESULT: a day's sizes are an importer's placeholder; none of its windows can be filled"
        print("feasibility: PLACEHOLDER sizes in imported days; this map measures nothing", file=sys.stderr)
    said = sources.plain()
    print(f"feasibility: by source: days {said['days']}, events {said['events']}", file=sys.stderr)
    text = json.dumps({**result, "days": [day.name for day in chosen], "sources": said}, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="ascii")
        print(f"feasibility: {len(chosen)} day(s), map written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
