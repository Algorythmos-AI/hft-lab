"""`python -m hftlab.review.trip_costs`: a sleeve's round trips against the cost model, trip by trip (HFT-EXP-0001).

    python -m hftlab.review.trip_costs --journal var/shadow/HFT-EXP-0001/2026-10-14.jsonl \\
        --day var/record/2026-10-14 var/record/2026-10-15 ...

It reads a shadow journal and the recorded days the journal was made on, and nothing else: the cost model and
the instruments are the ones the journal's first entry keeps, so the comparison is with the model the run was
made under and not with whatever the file says today.

For each completed round trip of the sleeve (`hftlab.review.trips`), per pair, since money of two currencies
is never added:

  result   what the two fills made, less both commissions;
  cost     what the cost model charges for that round trip at the two quotes the fills met
           (`CostModel.round_trip_cost_micro`), each widened as the shadow venue widens it near the rollover;
  excess   result + cost. For a sleeve with no edge it is the move of the mid it happened to hold through,
           and its mean is zero.

The quote a fill met is the last quote of its pair the runner had been shown by the venue's time of the fill.
The fill's price is then worked out again from that quote; a fill that was not made at it is `unmatched`, which
means the recording is not the one the journal was made on, and its round trip is not measured.

Everything printed is exact. Money is in millionths of the pair's quote currency TIMES TWO, like the cost
model's own figure (a mid can fall between two price units; doubled, everything stays an integer). A mean and
a variance are fractions, written as a numerator and a denominator. No root is taken: "within three standard
errors" is decided on the squares.

The verdict of each pair is the one the experiment registered:

  pass           ten times the sum of the excess is at most the sum of the cost, either way, and three
                 standard errors of the mean excess are at most a tenth of the mean cost;
  fail           the first does not hold, and the mean excess is more than three standard errors from zero;
  inconclusive   anything else, a pair with fewer than two round trips among it.

`not_exact` counts the round trips whose excess is not exactly their direction times the move of the mid
between their two fills times their size. While the per-order commission minimum applies it is expected to be
zero.

Exit codes: 0 computed, whatever the verdict; 1 the journal does not verify, or a fill does not meet the
recording; 2 a journal, a day or an argument that cannot be used.
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from hftlab.core import CASH_SCALE, Instrument, Quote
from hftlab.journal import JournalError, read
from hftlab.record.format import CorruptSegment, RecordedEvent, instrument_of
from hftlab.replay.reader import at_ns, events
from hftlab.review.trips import Leg, Trip, TripsError, round_trips
from hftlab.venues.sim.cost import WIDENED, CostModel, CostModelError

MONEY: Final = "millionths of the pair's quote currency, TIMES TWO"
PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"
EXIT_OK, EXIT_DIFFERS, EXIT_REFUSED = 0, 1, 2


class ReviewError(ValueError):
    """A journal or a recording this tool cannot be run on."""


class Books:
    """The quotes of a recording, by pair, at the times the shadow runner was shown them."""

    def __init__(self, stream: Iterable[RecordedEvent]) -> None:
        self._times: dict[str, list[int]] = {}
        self._tops: dict[str, list[tuple[int, int]]] = {}
        now = 0
        for event in stream:
            now = max(now, at_ns(event))  # the runner's clock never goes back, whatever a recorded time says
            if isinstance(event, Quote):
                self._times.setdefault(event.pair, []).append(now)
                self._tops.setdefault(event.pair, []).append((event.bid, event.ask))

    def in_force(self, pair: str, at: int) -> tuple[int, int] | None:
        """The bid and the ask of the last quote of the pair shown at or before `at`."""
        place = bisect_right(self._times.get(pair, []), at) - 1
        return self._tops[pair][place] if place >= 0 else None


def started_with(entries: Sequence[Mapping[str, Any]]) -> tuple[CostModel, dict[str, Instrument]]:
    """The cost model and the instruments the journal's sessions ran under. Two sessions that differ are refused."""
    starts = [entry for entry in entries if entry.get("type") == "start"]
    if not starts:
        raise ReviewError("the journal holds no session")
    first = starts[0]
    under = (first.get("cost_model"), first.get("instruments"))
    if any((one.get("cost_model"), one.get("instruments")) != under for one in starts):
        raise ReviewError("the journal's sessions ran under different cost models or instruments")
    try:
        model = CostModel(**first["cost_model"])
        described = [instrument_of(entry) for entry in first["instruments"]]
    except (TypeError, ValueError, CorruptSegment) as exc:
        raise ReviewError(f"the journal's first entry does not hold a cost model and instruments: {exc}") from exc
    return model, {instrument.pair: instrument for instrument in described}


def met(leg: Leg, pair: str, books: Books, model: CostModel, instrument: Instrument) -> tuple[int, int] | None:
    """The quote this fill met, widened as the venue widened it then; None when the fill was not made at it."""
    top = books.in_force(pair, leg.venue_ts_ns)
    if top is None:
        return None
    bid, ask = top
    widened = model.phase(leg.venue_ts_ns) == WIDENED
    if model.taker_price(leg.side, bid, ask, instrument, widened=widened) != leg.price:
        return None
    extra = model.extra_half_spread(ask - bid, instrument) if widened else 0
    return bid - extra, ask + extra


def measured(trip: Trip, books: Books, model: CostModel, instrument: Instrument) -> tuple[int, int, bool] | None:
    """(twice the result, the modelled cost times two, is the excess exactly the move of the mid) of one trip."""
    entry = met(trip.entry, trip.pair, books, model, instrument)
    leaving = met(trip.exit, trip.pair, books, model, instrument)
    if entry is None or leaving is None:
        return None
    result = 2 * trip.result_micro(instrument)
    try:
        cost = model.round_trip_cost_micro(instrument, trip.quantity, entry, leaving)
    except CostModelError as exc:
        raise ReviewError(str(exc)) from exc
    scale = int(10 ** (CASH_SCALE - instrument.price_scale))
    moved = trip.direction * (sum(leaving) - sum(entry)) * trip.quantity * scale  # twice the move of the mid
    return result, cost, result + cost == moved


def _pair(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


def verdict(count: int, excess: int, cost: int, squares: int) -> tuple[str, dict[str, object]]:
    """The registered rule, on sums of doubled figures. Returns the verdict and the statistics it rests on."""
    said: dict[str, object] = {"mean_excess": None, "mean_cost": None, "variance_of_excess": None}
    if count == 0:
        return INCONCLUSIVE, said
    mean_excess, mean_cost = Fraction(excess, count), Fraction(cost, count)
    said.update(mean_excess=_pair(mean_excess), mean_cost=_pair(mean_cost))
    if count < 2:
        return INCONCLUSIVE, said
    variance = Fraction(count * squares - excess * excess, count * (count - 1))  # of the sample, unbiased
    said["variance_of_excess"] = _pair(variance)
    error_squared = variance / count  # the square of the standard error of the mean
    close = 10 * abs(excess) <= cost
    long_enough = 9 * error_squared <= (mean_cost / 10) ** 2
    apart = mean_excess**2 > 9 * error_squared
    return (PASS if close and long_enough else FAIL if not close and apart else INCONCLUSIVE), said


def compare(entries: Sequence[Mapping[str, Any]], stream: Iterable[RecordedEvent], sleeve: str) -> dict[str, object]:
    """The whole comparison, as the plain values the command prints."""
    model, instruments = started_with(entries)
    books = Books(stream)
    pairs: dict[str, object] = {}
    verdicts = []
    for pair, found in sorted(round_trips(entries, sleeve, instruments).items()):
        count = result = cost = excess = squares = unmatched = not_exact = 0
        for trip in found.trips:
            numbers = measured(trip, books, model, instruments[pair])
            if numbers is None:
                unmatched += 1
                continue
            count, result, cost = count + 1, result + numbers[0], cost + numbers[1]
            excess, squares = excess + numbers[0] + numbers[1], squares + (numbers[0] + numbers[1]) ** 2
            not_exact += not numbers[2]
        outcome, statistics = verdict(count, excess, cost, squares)
        verdicts.append(INCONCLUSIVE if unmatched or found.irregular else outcome)
        pairs[pair] = {
            "currency": instruments[pair].quote, "round_trips": count, "sum_result": result, "sum_cost": cost,
            "sum_excess": excess, "sum_excess_squared": squares, **statistics, "not_exact": not_exact,
            "unmatched": unmatched, "irregular": found.irregular, "open_quantity": found.open_quantity,
            "verdict": verdicts[-1],
        }  # fmt: skip
    overall = FAIL if FAIL in verdicts else PASS if verdicts and set(verdicts) == {PASS} else INCONCLUSIVE
    return {
        "kind": "trip-costs", "version": 1, "sleeve": sleeve, "money": MONEY, "cost_model": model.as_dict(),
        "cost_model_is_placeholder": model.placeholder, "pairs": pairs, "verdict": overall,
    }  # fmt: skip


def recorded(days: Sequence[Path]) -> Iterable[RecordedEvent]:
    for day in days:
        if not day.is_dir():
            raise ReviewError(f"{day} is not a day folder")
        yield from events(day)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.review.trip_costs", description="Trips against costs.")
    parser.add_argument("--journal", type=Path, required=True, help="the shadow journal of the run")
    parser.add_argument("--day", type=Path, nargs="+", required=True, help="the recorded day folder(s), in order")
    parser.add_argument("--sleeve", default="control", help="the sleeve whose round trips are compared")
    args = parser.parse_args(argv)
    try:
        if not args.journal.is_file():
            raise ReviewError(f"{args.journal} is not a journal")
        found = compare(read(args.journal)[1], recorded(args.day), args.sleeve)
    except JournalError as exc:
        print(f"trip_costs: the journal does not verify: {exc}", file=sys.stderr)
        return EXIT_DIFFERS
    except (ReviewError, TripsError, CorruptSegment) as exc:
        print(f"trip_costs: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if found["cost_model_is_placeholder"]:
        print("trip_costs: the cost model is a PLACEHOLDER; this checks the accounting only", file=sys.stderr)
    print(json.dumps({**found, "journal": str(args.journal), "days": [day.name for day in args.day]}, indent=2))
    pairs = found["pairs"]
    assert isinstance(pairs, dict)
    return EXIT_DIFFERS if any(pair["unmatched"] for pair in pairs.values()) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
