"""`python -m hftlab.feasibility --store <dir>`: the feasibility map of what the recorder stored, as JSON.

    python -m hftlab.feasibility --store var/record                          every recorded day, as one stream
    python -m hftlab.feasibility --store var/record --day 2026-10-14         one day (several may be named)
    python -m hftlab.feasibility --store var/record --out var/feasibility.json

It reads the day folders, the cost model and the calendar, and writes the map to --out, or prints it. It holds
no venue and sends nothing anywhere. The map carries the cost model's parameters and says in itself whether
they are placeholders; while they are, no cell of it is a result.

Exit codes: 0 written, 2 the store, a day or a setting could not be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

from hftlab.calendar import CalendarError, load_calendar
from hftlab.core import Instrument
from hftlab.feasibility.map import FeasibilityError, feasibility_map, load_settings
from hftlab.record.format import CorruptSegment, RecordedEvent
from hftlab.record.store import days
from hftlab.replay.reader import events, instruments
from hftlab.venues.sim.cost import CostModelError, load_cost_model

CONFIG = Path("config/feasibility.yaml")
COSTS = Path("config/cost_model.yaml")
CALENDAR = Path("config/calendar")


def recorded(store: Path, names: Sequence[str]) -> tuple[list[Path], dict[str, Instrument], Iterator[RecordedEvent]]:
    """The chosen day folders of a store, what they say their instruments are, and their events as one stream."""
    found = days(store)
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

    def stream() -> Iterator[RecordedEvent]:
        for day in chosen:
            yield from events(day)

    return chosen, described, stream()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.feasibility", description="Map moves against costs.")
    parser.add_argument("--store", type=Path, required=True, help="the recorder's root: a folder of day folders")
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
        chosen, described, stream = recorded(args.store, args.day)
        result = feasibility_map(stream, described, model, calendar, settings)
    except (FeasibilityError, CostModelError, CalendarError, CorruptSegment) as exc:
        print(f"feasibility: {exc}", file=sys.stderr)
        return 2
    if result["placeholder"]:
        print("feasibility: PLACEHOLDER costs or rule; no cell of this map measures anything", file=sys.stderr)
    text = json.dumps({**result, "days": [day.name for day in chosen]}, indent=2) + "\n"
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="ascii")
        print(f"feasibility: {len(chosen)} day(s), map written to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
