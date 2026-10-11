"""`python -m hftlab.review.dc_null`: a Directional Change sleeve's day beside its random-walk null (HFT-EXP-0005).

    python -m hftlab.review.dc_null --config research/experiments/HFT-EXP-0005-directional-change-shadow/config.yaml \\
        --day var/record/2026-10-14

For each Directional Change sleeve of the configuration, alone, on one recorded day and one pair:

  observed   the sum of its completed round trips' results on the day as it was recorded, after the spread,
             the slippage and both commissions, as the shadow venue fills and charges;
  null       the same sum on each of N walks of the day (`hftlab.strategy.dc.null`): every tick of the pair
             keeps its time, its sequence number, its sizes and its spread, and only the path of the mid is
             replaced, by a driftless walk whose mean squared change per tick is the day's own, exactly;
  place      the share of those N results, and the observed one counted among them, that are at least the
             observed one (`at_or_above`): 1/(N+1) when no walk did as well. It is never zero.

Everything else of the day stays where it was in every run: the heartbeats, the connection changes and the
other pairs' quotes are shown to the sleeve in the same places, so a quiet stretch is as quiet on a walk as it
was on the day. Each run is a session of the shadow runner of its own, started flat, with the configuration's
shadow seed; a position still open at the end of a run is reported for the day and is in no result.

The number of walks and their seed are the configuration's (`measure.null_walks`), unless they are given on
the command line; the report says which. Whether tick `k` of walk `number` moves is a hash of the seed, the
walk and the tick, so the same day, configuration and seed print the same bytes on any machine.

It holds no venue of its own. The sleeves run in `hftlab.shadow.Shadow`, which builds its shadow venue itself
and can be handed no other. Money is in millionths of the pair's quote currency. A fraction is written as a
numerator and a denominator.

Exit codes: 0 computed; 2 a configuration, a day or an argument that cannot be used.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from hftlab.calendar import Calendar, CalendarError, load_calendar
from hftlab.core import Instrument, Quote
from hftlab.core._checks import pair as check_pair
from hftlab.journal import read
from hftlab.record.format import CorruptSegment, RecordedEvent
from hftlab.replay.reader import events, instruments
from hftlab.review.trips import Found, TripsError, round_trips
from hftlab.shadow import Journal, Shadow, ShadowError, run
from hftlab.shadow.__main__ import DIRECTIONAL_CHANGE, ConfigError, load_config, make_sleeves, minimum_sizes
from hftlab.strategy.dc import DcSleeve, null
from hftlab.venues.sim.cost import CostModel, CostModelError, load_cost_model

COSTS: Final = Path("config/cost_model.yaml")
CALENDAR: Final = Path("config/calendar")
MONEY: Final = "millionths of the pair's quote currency"
EXIT_OK, EXIT_REFUSED = 0, 2


def _pair(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


def walks_of(config: Mapping[str, Any], walks: int | None, seed: int | None) -> tuple[int, int, str]:
    """How many walks, with which seed, and where the two numbers came from."""
    if walks is not None and seed is not None:
        return walks, seed, "the command line"
    measure = config.get("measure")
    fixed = measure.get("null_walks") if isinstance(measure, dict) else None
    if walks is not None or seed is not None or not isinstance(fixed, dict):
        raise ConfigError("give both --walks and --seed, or neither when the configuration fixes measure.null_walks")
    count, fixed_seed = fixed.get("count"), fixed.get("seed")
    if type(count) is not int or type(fixed_seed) is not int:
        raise ConfigError("measure.null_walks needs a whole count and a whole seed")
    return count, fixed_seed, "measure.null_walks of the configuration"


class Day:
    """One recorded day and one pair of it: every event in its place, and the pair's quotes apart."""

    def __init__(self, stream: Sequence[RecordedEvent], traded: Sequence[Instrument], pair: str) -> None:
        self.stream, self.traded, self.pair = list(stream), list(traded), pair
        found = [instrument for instrument in traded if instrument.pair == pair]
        if not found:
            raise ConfigError(f"the day does not describe {pair}")
        self.instrument = found[0]
        self._places = [n for n, event in enumerate(self.stream) if isinstance(event, Quote) and event.pair == pair]
        self.quotes: list[Quote] = [event for event in self.stream if isinstance(event, Quote) and event.pair == pair]
        if len(self.quotes) < 2:
            raise ConfigError(f"the day holds fewer than two quotes of {pair}: there is no change to match a walk to")

    def with_quotes(self, quotes: Sequence[Quote]) -> list[RecordedEvent]:
        """The day with the pair's quotes replaced, one for one, and everything else where it was."""
        if len(quotes) != len(self._places):
            raise ConfigError("a walk has one quote for each quote of the day")
        out = list(self.stream)
        for place, quote in zip(self._places, quotes, strict=True):
            out[place] = quote
        return out


def one_run(
    day: Day, stream: Sequence[RecordedEvent], entry: Mapping[str, Any], *, model: CostModel, calendar: Calendar,
    seed: int, journal: Path,
) -> Found:  # fmt: skip
    """One sleeve alone over one stream in the shadow runner, into a journal of its own. What it did in the pair."""
    sleeves = make_sleeves([entry], minimum_sizes(day.traded, model), costs=model, calendar=calendar)
    shadow = Shadow(day.traded, model, sleeves, Journal(journal), calendar=calendar, seed=seed, source="dc_null")
    run(shadow, stream)
    described = {instrument.pair: instrument for instrument in day.traded}
    return round_trips(read(journal)[1], sleeves[0].name, described).get(day.pair, Found())


def result_micro(found: Found, instrument: Instrument) -> int:
    return sum(trip.result_micro(instrument) for trip in found.trips)


def beside_its_null(
    day: Day, entry: Mapping[str, Any], *, model: CostModel, calendar: Calendar, shadow_seed: int, walks: int,
    seed: int, scratch: Path,
) -> dict[str, object]:  # fmt: skip
    """One sleeve's day, its `walks` null results in the walks' order, and where the day stands among them."""
    name = str(entry["name"])
    (sleeve,) = make_sleeves([entry], minimum_sizes(day.traded, model), costs=model, calendar=calendar)
    assert isinstance(sleeve, DcSleeve)

    def once(stream: Sequence[RecordedEvent], journal: Path) -> Found:
        return one_run(day, stream, entry, model=model, calendar=calendar, seed=shadow_seed, journal=journal)

    observed = once(day.stream, scratch / f"{name}.day.jsonl")
    trips: list[int] = []

    def on_a_walk(quotes: Sequence[Quote]) -> int:
        journal = scratch / f"{name}.walk-{len(trips):06d}.jsonl"
        found = once(day.with_quotes(quotes), journal)
        journal.unlink()
        trips.append(len(found.trips))
        return result_micro(found, day.instrument)

    result_of: Callable[[Sequence[Quote]], int] = on_a_walk
    results = null.results(result_of, day.quotes, day.instrument, walks=walks, seed=seed)
    seen = result_micro(observed, day.instrument)
    said = {"result_micro": seen, "round_trips": len(observed.trips), "irregular": observed.irregular}
    return {
        "name": name,
        "settings": sleeve.settings(),
        "observed": {**said, "open_quantity": observed.open_quantity},
        "null": {"results_micro": list(results), "round_trips": trips},
        "walks_at_or_above": sum(result >= seen for result in results),
        "at_or_above": _pair(null.at_or_above(seen, results)),
    }


def report(
    config: Mapping[str, Any], day: Day, *, model: CostModel, calendar: Calendar, walks: int, seed: int,
    names: Sequence[str], scratch: Path,
) -> dict[str, object]:  # fmt: skip
    """Every chosen Directional Change sleeve of the configuration on the day, each beside its null."""
    listed = [entry for entry in config["sleeves"] if isinstance(entry, dict)]
    entries = [entry for entry in listed if entry.get("kind") == DIRECTIONAL_CHANGE]
    chosen = [entry for entry in entries if not names or entry.get("name") in names]
    if not chosen or (names and len(chosen) != len(set(names))):
        raise ConfigError(f"the configuration has the Directional Change sleeves {[e.get('name') for e in entries]}")
    step, share = null.step_and_share(day.quotes, day.instrument)
    shadow_seed = config["seed"]
    found = []
    for entry in chosen:
        one = beside_its_null(
            day, entry, model=model, calendar=calendar, shadow_seed=shadow_seed, walks=walks, seed=seed,
            scratch=scratch,
        )  # fmt: skip
        found.append(one)
    return {
        "kind": "dc-null", "version": 1, "pair": day.pair, "money": MONEY, "walks": walks, "seed": seed,
        "shadow_seed": config["seed"], "ticks": len(day.quotes),
        "mean_square_x4": _pair(null.mean_square_x4(day.quotes)), "step_x2": step,
        "share_of_ticks_that_move": _pair(share),
        "cost_model": model.as_dict(), "cost_model_is_placeholder": model.placeholder, "sleeves": found,
    }  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.review.dc_null", description="A day beside its null.")
    parser.add_argument("--config", type=Path, required=True, help="the shadow runner's configuration of the run")
    parser.add_argument("--day", type=Path, required=True, help="one recorded day folder")
    parser.add_argument("--pair", default="EUR.USD")
    parser.add_argument("--sleeve", nargs="+", default=[], help="the sleeves to run (default: every one of the kind)")
    parser.add_argument("--walks", type=int, help="the number of walks, when the configuration does not fix it")
    parser.add_argument("--seed", type=int, help="the seed of the walks, when the configuration does not fix it")
    parser.add_argument("--cost-model", type=Path, default=COSTS)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        walks, seed, said_by = walks_of(config, args.walks, args.seed)
        model, calendar = load_cost_model(args.cost_model), load_calendar(args.calendar)
        if not args.day.is_dir():
            raise ConfigError(f"{args.day} is not a day folder")
        day = Day(list(events(args.day)), list(instruments(args.day).values()), check_pair(args.pair))
        with tempfile.TemporaryDirectory(prefix="hft-dc-null-") as scratch:
            found = report(
                config, day, model=model, calendar=calendar, walks=walks, seed=seed, names=args.sleeve,
                scratch=Path(scratch),
            )  # fmt: skip
    except (ConfigError, CostModelError, CalendarError, CorruptSegment, ShadowError, TripsError, null.NullError) as exc:
        print(f"dc_null: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if model.placeholder:
        print("dc_null: the cost model is a PLACEHOLDER; no figure of this run measures anything", file=sys.stderr)
    print(json.dumps({**found, "day": args.day.name, "walks_and_seed_from": said_by}, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
