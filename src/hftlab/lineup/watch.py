"""The drift look: `python -m hftlab.learn.drift` (ADR 0023).

    python -m hftlab.learn.drift              look at the week, report, and act on a breach
    python -m hftlab.learn.drift --dry-run    the same look and the same report; asks for nothing and pages nobody

For every ML place of the trader that the lineup puts a model in, it compares the week the trader saw with what
the model's own evaluation report kept of its training rows (`hftlab.learn.drift`):

  feature shift   the quotes of the week's tapes, folded through the feature builder with the report's windows,
                  counted into the report's bins.
  calibration     what the model expected of each trade it took, journalled beside the decision, against what
                  the round trip made in the shadow book.
  spread regime   the median spread of the same rows against the training median.

The rows are made from the quotes and connection changes of the tapes. The heartbeats the trader makes for a
quiet book are not on a tape, so a quiet stretch gives fewer rows here than the sleeve saw. A monitor counts;
it does not have to be the sleeve.

A breach has one consequence: the trader is asked, by a request file as an operator would, to take the sleeve
down to `shadow`, and the owner is paged. The trader journals what came of the request; it refuses while the
sleeve holds a position on paper, and the next look asks again. With any threshold null the monitors are
computed and reported, and nothing is breached. Nothing here reaches a venue, places an order or sets a stage.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from hftlab.calendar import Calendar, CalendarError, load_calendar
from hftlab.core import Clock, Quote, WallClock
from hftlab.features import FeatureBuilder, FeatureConfig
from hftlab.journal import JournalError, canonical
from hftlab.learn.config import Drift, LearnConfigError, Settings, load
from hftlab.learn.drift import EXPECTED_KEY, SPREAD_KEY, DriftError, Watch, calibration, feature_shift, spread_regime
from hftlab.learn.train import keep_report
from hftlab.lineup import journal as lineups
from hftlab.lineup.decide import DecideRefused, Evidence, Week, read_week
from hftlab.lineup.journal import Lineup, LineupError
from hftlab.lineup.serve import load as load_model
from hftlab.lineup.serve import places, standing, windows
from hftlab.ops import host
from hftlab.ops.alerts import Alerts, sender_from
from hftlab.record.format import CorruptSegment
from hftlab.trader.config import Settings as TraderSettings
from hftlab.trader.config import TraderConfigError
from hftlab.trader.config import load as load_trader
from hftlab.trader.tape import REQUESTS, TapeError, decode, leave_request, read_tape
from hftlab.venues.port import ConnectionEvent

CONFIG: Final = Path("config/learn.yaml")
TRADER: Final = Path("config/trader.yaml")
CALENDAR: Final = Path("config/calendar")
REPORT_VERSION: Final = 1
OPERATOR: Final = "the drift monitor"
SHADOW: Final = "shadow"  # the one stage a breach asks for
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


class WatchRefused(Exception):
    """The look will not be taken as things are. A person is needed; a retry is not."""


class Notify(Protocol):
    """How the owner is told. `hftlab.ops.alerts.Alerts` is one."""

    def fire(self, name: str, title: str, message: str, priority: int = 4) -> bool: ...

    def resolve(self, name: str, title: str, message: str) -> bool: ...


def tape_rows(state_dir: Path, days: Sequence[str], builder: FeatureBuilder) -> list[tuple[int, ...]]:
    """The feature rows of the days' tapes, oldest first. Each session starts from nothing, as its sleeve did."""
    out: list[tuple[int, ...]] = []
    for day in days:
        for tape in sorted((state_dir / "days" / day).glob("session-*.tape.jsonl")):
            state = builder.initial()
            for line in read_tape(tape):
                if line[0] != "step":
                    continue
                at: Any = line[1]
                for raw in line[3]:
                    event = decode(raw)
                    if isinstance(event, Quote | ConnectionEvent):
                        state, row = builder.step(state, event, at)
                        if row is not None:
                            out.append(row.values)
    return out


def watch_one(
    sleeve: str, model: str, summary: dict[str, object], keys: Sequence[str], rows: Sequence[Sequence[int]],
    trades: Sequence[tuple[int, int]], limits: Drift,
) -> Watch:  # fmt: skip
    """Every monitor for one sleeve. Pure: the same summary, rows, trades and thresholds give the same findings."""
    force = limits.in_force  # a threshold alone is not in force: all four are written, or nothing is breached
    rows_needed = limits.min_rows if force else None
    found = feature_shift(
        summary, keys, rows, threshold_ppm=limits.feature_shift_ppm if force else None, min_rows=rows_needed
    )
    columns = summary.get("columns")
    if isinstance(columns, dict) and EXPECTED_KEY in columns:
        gap = limits.calibration_gap_micro if force else None
        found += calibration(summary, trades, threshold_micro=gap, min_rows=rows_needed)
    if SPREAD_KEY in keys:
        spreads = [row[list(keys).index(SPREAD_KEY)] for row in rows]
        ratio = limits.spread_ratio_x100 if force else None
        found.append(spread_regime(summary, spreads, threshold_x100=ratio, min_rows=rows_needed))
    return Watch(sleeve, model, tuple(found))


@dataclass(frozen=True, slots=True)
class Desk:
    """Where everything a look reads and writes is."""

    learn: Settings
    trader: TraderSettings
    calendar: Calendar
    notify: Notify

    @property
    def registry(self) -> Path:
        return self.trader.registry or self.learn.state_dir


def look(desk: Desk, lineup: Lineup, week: Week, evidence: Evidence) -> tuple[list[Watch], dict[str, str]]:
    """One watch per ML place with a model, and the places that could not be looked at, with why."""
    watches: list[Watch] = []
    unread: dict[str, str] = {}
    made: dict[tuple[str, FeatureConfig], list[tuple[int, ...]]] = {}
    for sleeve, place in sorted(places(desk.trader.sleeves).items()):
        named = standing(lineup, place)
        if named is None:
            continue
        try:
            model, report, _ = load_model(named, desk.registry)
            config, _ = windows(report)
            builder = FeatureBuilder(model.pair, config, desk.calendar)
            summary = report.get("summary")
            if builder.keys != model.features or not isinstance(summary, dict):
                raise DriftError("the report keeps no summary of the features its model reads")
            if (model.pair, config) not in made:
                made[model.pair, config] = tape_rows(desk.trader.state_dir, week.days, builder)
            trades = [(hoped, got) for by, hoped, got in evidence.expected.get(sleeve, []) if by == model.content_hash]
            watches.append(
                watch_one(sleeve, model.content_hash, summary, builder.keys, made[model.pair, config], trades,
                          desk.learn.drift)
            )  # fmt: skip
        except (LineupError, DriftError) as exc:
            unread[sleeve] = str(exc)
    return watches, unread


def say(text: str) -> None:
    print(f"drift: {text}", file=sys.stderr, flush=True)


def run(desk: Desk, now_ns: int, *, dry_run: bool = False) -> tuple[int, list[Watch]]:
    """One look. Returns the exit code and what was found."""
    lineup = lineups.read(desk.learn.lineup)
    try:
        week, evidence = read_week(desk.trader.state_dir, now_ns)
    except DecideRefused as exc:
        raise WatchRefused(str(exc)) from exc
    watches, unread = look(desk, lineup, week, evidence)
    limits = desk.learn.drift
    said: dict[str, object] = {
        "kind": "drift-report", "version": REPORT_VERSION, "week": week.label, "days": list(week.days),
        "lineup": lineup.head, "in_force": int(limits.in_force),
        "thresholds": {
            "min_rows": limits.min_rows, "feature_shift_ppm": limits.feature_shift_ppm,
            "calibration_gap_micro": limits.calibration_gap_micro, "spread_ratio_x100": limits.spread_ratio_x100,
        },
        "watches": [watch.plain() for watch in watches],
        "unread": dict(sorted(unread.items())),
    }  # fmt: skip
    report = (canonical(said) + "\n").encode("ascii")
    sys.stdout.write(report.decode("ascii"))
    sys.stdout.flush()
    breached = [watch for watch in watches if watch.breached]
    for sleeve, why in sorted(unread.items()):
        say(f"{sleeve} could not be looked at: {why}")
    if not limits.in_force:
        say(f"thresholds not in force: {len(watches)} sleeve(s) looked at, nothing stepped down.")
    if dry_run:
        say(f"dry run, nothing asked and nobody paged. {len(breached)} sleeve(s) over a threshold.")
        return EXIT_OK, watches
    digest = keep_report(desk.learn.reports, report) if breached else ""
    for watch in watches:
        key = f"drift.{watch.sleeve}"
        if watch.breached:
            leave_request(desk.trader.state_dir / REQUESTS, "stage", watch.sleeve, SHADOW, OPERATOR)
            what = ", ".join(f"{finding.monitor} of {finding.subject}" for finding in watch.breached[:5])
            text = f"{watch.sleeve} (model {watch.model[:12]}) is asked down to shadow: {what}. Report {digest[:12]}."
            desk.notify.fire(key, f"Drift: {watch.sleeve}", text, 4)
            say(text)
        elif limits.in_force:
            desk.notify.resolve(key, f"Drift over: {watch.sleeve}", "The last look found every monitor within bounds.")
    return EXIT_OK, watches


def main(argv: Sequence[str] | None = None, clock: Clock | None = None, notify: Notify | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.learn.drift", description="The drift monitors.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--trader", type=Path, default=TRADER, help="the trader's settings: journals, tapes, places")
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--dry-run", action="store_true", help="look and print; ask for nothing")
    args = parser.parse_args(argv)
    clock = clock or WallClock()
    try:
        learn, trader = load(args.config), load_trader(args.trader)
        if learn.lineup != trader.state_dir / lineups.FILE:
            read_by = trader.state_dir / lineups.FILE
            raise WatchRefused(f"{args.config} names {learn.lineup}, and the trader reads {read_by}")
        calendar = load_calendar(args.calendar)
        if notify is None:
            settings = host.settings()
            notify = Alerts(host.state_dir(settings["HFT_STATE_DIR"]) / "alerts", sender_from(settings, say), clock)
        return run(Desk(learn, trader, calendar, notify), clock.now_ns(), dry_run=args.dry_run)[0]
    except (WatchRefused, LearnConfigError, TraderConfigError, CalendarError) as exc:
        say(f"refused: {exc}")
        return EXIT_REFUSED
    except (LineupError, JournalError, TapeError, CorruptSegment, OSError) as exc:
        say(f"FAILED: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
