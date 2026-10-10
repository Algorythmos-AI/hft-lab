"""The feasibility map: for each pair and holding time, does the price move by more than a round trip costs?

It is the first thing the platform asks of its own recordings, before any strategy exists: a strategy is only
worth building where the answer is yes (docs/architecture.md).

  sample point  a moment on a fixed grid (every `step` of UTC time) at which a pair has a usable book: its last
                quote, received at or before that moment, was confirmed within the cost model's staleness
                limit, is not crossed, and the market is open.
  window        two sample points of one pair, one holding time apart.
  move          how far the mid went across the window, either way. It is what one round trip of the minimum
                size would have made with perfect foresight of the direction, before costs.
  cost          what the cost model charges for that round trip at the quotes of the window's two ends
                (`CostModel.round_trip_cost_micro`): half the spread and the slippage at each end, and both
                commissions. Near the rollover each spread is widened exactly as the shadow venue widens it.
  pays          the move is larger than the cost, and the shadow venue would have filled both ends: neither is
                inside the no-fill window, and the side to be taken shows at least the minimum size.

A cell is one pair at one holding time. It is `viable` when it has at least `min_windows` windows and at least
`min_share_ppm` millionths of them pay. Both numbers are settings, and the map carries them with the cost
model's parameters and its placeholder flag: a map computed on placeholder costs says so in itself.

Money is in millionths of the pair's quote currency TIMES TWO, like the cost model's own figure: a mid can fall
between two price units, and doubled, everything stays an integer. There is no float here. Quantiles are by
nearest rank, so each is a value that occurred.

The map is a pure function of the events, the instruments, the cost model, the calendar and the settings.
Windows of a long holding time overlap, so their number is not a number of independent observations. The
commissions are priced as for buying first; selling first differs by the rate on one spread.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NamedTuple

import yaml

from hftlab.calendar import Calendar
from hftlab.core import CASH_SCALE, Instrument, Quote
from hftlab.record.format import Heartbeat, RecordedEvent
from hftlab.replay.reader import at_ns
from hftlab.venues.sim.cost import NO_FILL, WIDENED, CostModel

MILLION: Final = 1_000_000
SECOND: Final = 1_000_000_000
VERSION: Final = 1
MONEY: Final = "millionths of the pair's quote currency, TIMES TWO, for one round trip of `size`"


class FeasibilityError(ValueError):
    """The settings file is missing something, or asks for a map that cannot be computed exactly."""


@dataclass(frozen=True, slots=True)
class Settings:
    horizons_ns: tuple[int, ...]
    step_ns: int
    upper_quantile_ppm: int
    min_windows: int
    min_share_ppm: int
    placeholder: bool

    def __post_init__(self) -> None:
        horizons = self.horizons_ns
        if self.step_ns <= 0 or not horizons:
            raise FeasibilityError("the map needs a positive step and at least one holding time")
        if any(h <= 0 or h % self.step_ns for h in horizons) or len(set(horizons)) != len(horizons):
            raise FeasibilityError("each holding time is a positive multiple of the step, and is listed once")
        shares = (self.upper_quantile_ppm, self.min_share_ppm)
        if not all(0 < share <= MILLION for share in shares) or self.min_windows < 1:
            raise FeasibilityError("a quantile and a share are 1 to 1,000,000 millionths; min_windows is at least 1")

    def as_dict(self) -> dict[str, object]:
        """Every setting as a plain value: what the map keeps beside its cells."""
        return {
            "placeholder": self.placeholder,
            "horizons_ns": list(self.horizons_ns),
            "step_ns": self.step_ns,
            "upper_quantile_ppm": self.upper_quantile_ppm,
            "min_windows": self.min_windows,
            "min_share_ppm": self.min_share_ppm,
        }


class Point(NamedTuple):
    """A pair's book at one grid moment, and the cost model's phase of the day there."""

    bid: int
    ask: int
    bid_size: int
    ask_size: int
    phase: str


def quantile(ordered: Sequence[int], ppm: int) -> int:
    """The smallest of the sorted values that has at least `ppm` millionths of them at or below it."""
    return ordered[max(1, -(-len(ordered) * ppm // MILLION)) - 1]


def sample_points(
    events: Iterable[RecordedEvent],
    instruments: Mapping[str, Instrument],
    model: CostModel,
    calendar: Calendar,
    step_ns: int,
) -> tuple[dict[str, dict[int, Point]], dict[str, int]]:
    """Each pair's usable book at every grid moment, and what the stream held: (points, counts).

    Time is the events' own and never runs backwards: an event stamped earlier than the one before it is taken
    at the time already reached, as a replay takes it.
    """
    top: dict[str, Quote] = {}
    fresh: dict[str, int] = {}
    due: dict[str, int] = {}  # the next grid moment not yet looked at, per pair
    points: dict[str, dict[int, Point]] = {pair: {} for pair in instruments}
    day: dict[int, tuple[bool, str]] = {}  # is the market open, and the phase: the same for every pair
    counts = {"events": 0, "quotes": 0, "crossed_points": 0}
    now, first = 0, -1

    def catch_up(pair: str, until: int) -> None:
        """Sample the pair's grid moments up to `until`, for as long as its last quote stays confirmed."""
        quote = top[pair]
        moment, last = due[pair], min(until, fresh[pair] + model.stale_after_ns)
        while moment <= last:
            if moment not in day:
                day[moment] = (calendar.is_open(moment), model.phase(moment))
            is_open, phase = day[moment]
            if quote.is_crossed:
                counts["crossed_points"] += 1
            elif is_open:
                points[pair][moment] = Point(quote.bid, quote.ask, quote.bid_size, quote.ask_size, phase)
            moment += step_ns
        due[pair] = max(due[pair], -(-(until + 1) // step_ns) * step_ns)

    for event in events:
        now = max(now, at_ns(event))
        first = now if first < 0 else first
        counts["events"] += 1
        if not isinstance(event, Quote | Heartbeat) or event.pair not in instruments:
            continue
        if event.pair in top:
            catch_up(event.pair, now - 1)  # the book as it was until just before this event
            fresh[event.pair] = now
        if isinstance(event, Quote):
            counts["quotes"] += 1
            top[event.pair], fresh[event.pair] = event, now
            due.setdefault(event.pair, -(-now // step_ns) * step_ns)
    for pair in top:
        catch_up(pair, now)
    return points, {**counts, "first_ns": max(first, 0), "last_ns": now}


def _charged(point: Point, instrument: Instrument, model: CostModel) -> tuple[int, int]:
    """The (bid, ask) the cost model prices against at this point: the quote, widened near the rollover."""
    if point.phase != WIDENED:
        return point.bid, point.ask
    extra = model.extra_half_spread(point.ask - point.bid, instrument)
    return point.bid - extra, point.ask + extra


def _cell(
    points: Mapping[int, Point],
    horizon_ns: int,
    instrument: Instrument,
    size: int,
    model: CostModel,
    settings: Settings,
) -> dict[str, object]:
    to_micro = size * int(10 ** (CASH_SCALE - instrument.price_scale))
    moves: list[int] = []
    costs: list[int] = []
    paying = unfillable = 0
    for moment, start in points.items():
        end = points.get(moment + horizon_ns)
        if end is None:
            continue
        went = (end.bid + end.ask) - (start.bid + start.ask)  # twice the move of the mid, in price units
        move = abs(went) * to_micro
        entry, exit_ = _charged(start, instrument, model), _charged(end, instrument, model)
        cost = model.round_trip_cost_micro(instrument, size, entry, exit_)
        # With foresight a rise is bought at the start and sold at the end, a fall the other way round.
        shown = min(start.ask_size, end.bid_size) if went > 0 else min(start.bid_size, end.ask_size)
        fillable = NO_FILL not in (start.phase, end.phase) and shown >= size
        unfillable += not fillable
        paying += fillable and move > cost
        moves.append(move)
        costs.append(cost)
    windows = len(moves)
    moves.sort()
    costs.sort()
    return {
        "horizon_ns": horizon_ns,
        "windows": windows,
        "unfillable": unfillable,
        "paying": paying,
        "share_ppm": paying * MILLION // windows if windows else 0,
        "median_move_x2_micro": quantile(moves, MILLION // 2) if windows else None,
        "upper_move_x2_micro": quantile(moves, settings.upper_quantile_ppm) if windows else None,
        "median_cost_x2_micro": quantile(costs, MILLION // 2) if windows else None,
        "viable": windows >= settings.min_windows and paying * MILLION >= settings.min_share_ppm * windows,
    }


def feasibility_map(
    events: Iterable[RecordedEvent],
    instruments: Mapping[str, Instrument],
    model: CostModel,
    calendar: Calendar,
    settings: Settings,
) -> dict[str, object]:
    """The map of one stream of recorded events, as plain values ready to be written as JSON."""
    points, counts = sample_points(events, instruments, model, calendar, settings.step_ns)
    pairs: dict[str, object] = {}
    viable: list[list[object]] = []
    for pair in sorted(instruments):
        instrument = instruments[pair]
        size = model.minimum_size(instrument)
        cells = [_cell(points[pair], horizon, instrument, size, model, settings) for horizon in settings.horizons_ns]
        viable += [[pair, cell["horizon_ns"]] for cell in cells if cell["viable"]]
        pairs[pair] = {
            "currency": instrument.quote,
            "price_scale": instrument.price_scale,
            "size": size,
            "sample_points": len(points[pair]),
            "cells": cells,
        }
    placeholder = model.placeholder or settings.placeholder
    return {
        "kind": "feasibility-map",
        "version": VERSION,
        "placeholder": placeholder,
        "note": (
            "NOT A RESULT: computed on placeholder costs or a placeholder rule; no cell of this map means anything"
            if placeholder
            else "computed on a measured cost model and a decided rule"
        ),
        "money": MONEY,
        "cost_model": model.as_dict(),
        "settings": settings.as_dict(),
        "stream": counts,
        "pairs": pairs,
        "viable_cells": viable,
    }


def _whole(mapping: Mapping[str, Any], key: str, what: str) -> int:
    value = mapping.get(key)
    if type(value) is not int or value <= 0:
        raise FeasibilityError(f"{what}{key} must be a whole, positive number, not {value!r}")
    return value


def load_settings(path: Path) -> Settings:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FeasibilityError(f"cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("version") != VERSION:
        raise FeasibilityError(f"{path} is not a version {VERSION} feasibility file")
    horizons, rule = loaded.get("horizons_seconds"), loaded.get("viable")
    if not isinstance(horizons, list) or any(type(h) is not int for h in horizons) or not isinstance(rule, dict):
        raise FeasibilityError(f"{path} needs horizons_seconds, a list of whole seconds, and a `viable` section")
    return Settings(
        horizons_ns=tuple(h * SECOND for h in horizons),
        step_ns=_whole(loaded, "step_seconds", "") * SECOND,
        upper_quantile_ppm=_whole(loaded, "upper_quantile_ppm", ""),
        min_windows=_whole(rule, "min_windows", "viable."),
        min_share_ppm=_whole(rule, "min_share_ppm", "viable."),
        placeholder=loaded.get("status") != "decided",
    )
