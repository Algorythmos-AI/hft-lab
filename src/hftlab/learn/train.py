"""Training: a dataset's rows into a model file, and `python -m hftlab.learn.train`, the weekend run (ADR 0023).

    python -m hftlab.learn.train                 build, evaluate, fit, register; the market must be closed
    python -m hftlab.learn.train --dry-run       the same evaluation at any time; registers nothing
    python -m hftlab.learn.train --hold-out      also score the candidate on the held-out weeks, once

For every configured pair and horizon, from the day folders of the recorder's store:

  1. the dataset is built, which refuses a feature that reads ahead (`hftlab.learn.dataset`);
  2. the configuration is written into the trial ledger, and only then evaluated: walk-forward by week, a ridge
     model fitted to each fold's training rows and scored on its test week, with the oracle and a seeded coin
     on the same rows, the day-block interval and the deflated Sharpe ratio with the trials counted;
  3. the candidate is fitted to every development week and kept in the model registry under its hash;
  4. the evaluation report is kept under its own hash, and the candidate is registered in the lineup journal.

The report is one line of canonical JSON holding integers and text only, so the same store, settings and
ledger give the same model hash and the same report, byte for byte. What differs from run to run (the time,
the memory the process peaked at) is in a file of its own beside it, `last-run.json`.

What it refuses (exit 2): to run while the market is open, unless it is a dry run; a store with too few weeks
to make one fold; a hold-out this configuration has already read. The held-out weeks are in no fold, in no
fit and in no summary, and are read only through `HoldOut.read`, which writes the read into the ledger first.
It fails (exit 1) when the process peaked above its memory budget, and then registers nothing.

Nothing here reaches a venue, and nothing here changes what the trader runs: a candidate serves nowhere until
the weekly decision (`python -m hftlab.learn.promote`) makes it a challenger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.calendar import Calendar, CalendarError, load_calendar
from hftlab.core import Clock, Instrument, Side, WallClock
from hftlab.feasibility.map import FeasibilityError, feasibility_map, load_settings
from hftlab.feasibility.map import Settings as FeasibilitySettings
from hftlab.features import FeatureRow, at_ns
from hftlab.journal import JournalError, canonical
from hftlab.learn.config import LearnConfigError, Settings, config_hash, load
from hftlab.learn.dataset import Dataset, DatasetError, LeakError, build_for
from hftlab.learn.drift import EXPECTED_KEY, by_column, summarise
from hftlab.learn.validate import (
    HoldOut,
    HoldOutSpent,
    Plan,
    ValidationError,
    deflated_sharpe,
    embargo_for,
    evaluate,
    mean_per_trade,
    null_variance,
    three_way,
    walk_forward,
)
from hftlab.lineup import REGISTER, LineupError, Named, append
from hftlab.lineup import read as read_lineup
from hftlab.models.file import Model, ModelError, ModelRegistry
from hftlab.models.ridge import RidgeError, fit
from hftlab.models.scorer import BUY, SELL, RidgeScorer
from hftlab.record.format import CorruptSegment, RecordedEvent
from hftlab.record.store import days as store_days
from hftlab.registry.ledger import TrialLedger, counted_trials
from hftlab.replay.reader import events as day_events
from hftlab.replay.reader import instruments as day_instruments
from hftlab.venues.sim.cost import CostModel, CostModelError, load_cost_model

CONFIG: Final = Path("config/learn.yaml")
COSTS: Final = Path("config/cost_model.yaml")
CALENDAR: Final = Path("config/calendar")
FEASIBILITY: Final = Path("config/feasibility.yaml")
RECORDER: Final = Path("config/recorder.yaml")
LAST_RUN: Final = "last-run.json"
REPORT_VERSION: Final = 1
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


class TrainRefused(Exception):
    """The run will not be made as things are. A person, or more recorded weeks, is needed; a retry is not."""


def _model(dataset: Dataset, kind: str, params: dict[str, object]) -> Model:
    return Model(
        kind, dataset.pair, dataset.features, dict(dataset.label), dict(dataset.cost_model), dataset.size,
        dataset.content_hash, params,
    )  # fmt: skip


def ridge_model(dataset: Dataset, positions: Sequence[int], penalty: Fraction) -> Model:
    """A ridge regression of the outcome of each side on the features, fitted exactly to the rows at `positions`."""
    rows = [dataset.rows[position] for position in positions]
    targets = {BUY: [row.buy_micro for row in rows], SELL: [row.sell_micro for row in rows]}
    return _model(dataset, "ridge", fit([row.features for row in rows], targets, penalty).params())


def flat_model(dataset: Dataset) -> Model:
    """The baseline that never trades."""
    return _model(dataset, "flat", {})


def random_model(dataset: Dataset, seed: int) -> Model:
    """The baseline that takes a side by a seeded coin."""
    return _model(dataset, "random", {"seed": seed})


# ---- one pair and horizon ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trained:
    pair: str
    horizon_ns: int
    model: Model
    report: bytes  # the evaluation report: one canonical line

    @property
    def report_hash(self) -> str:
        return hashlib.sha256(self.report).hexdigest()

    @property
    def named(self) -> Named:
        return Named(self.model.content_hash, self.report_hash)


def _walk(dataset: Dataset, settings: Settings) -> tuple[Plan, list[int]]:
    """The plan of folds, and the development rows: every row of a week that is not held out."""
    plan = walk_forward(
        dataset.rows, embargo_ns=embargo_for([max(settings.horizons_ns) + settings.exit_wait_ns, dataset.window_ns]),
        horizons_ns=settings.horizons_ns, min_train_weeks=settings.min_train_weeks,
        hold_out_weeks=settings.hold_out_weeks,
    )  # fmt: skip
    if not plan.folds:
        raise TrainRefused(
            f"{dataset.pair} at {dataset.horizon_ns} ns: {len(plan.weeks)} development week(s) make no fold; a fold "
            f"needs {settings.min_train_weeks} earlier week(s) to train on"
        )
    kept = set(plan.weeks)
    return plan, [position for position, row in enumerate(dataset.rows) if row.week_ns in kept]


def train_one(
    dataset: Dataset, settings: Settings, *, ledger: TrialLedger, registry: Path | None,
    feasibility: dict[str, object], feasibility_placeholder: bool, hold_out: bool,
) -> Trained:  # fmt: skip
    """Evaluate one pair and horizon walk-forward, fit the candidate, and write the report. Pure but for the ledger."""
    frozen = settings.frozen(dataset.pair, dataset.horizon_ns, dict(dataset.cost_model))
    config = config_hash(frozen)
    plan, development = _walk(dataset, settings)
    fold_of = {dataset.rows[position].n: number for number, fold in enumerate(plan.folds) for position in fold.test}
    scorers: dict[int, RidgeScorer | None] = {}
    expected: list[int] = []
    untrained: set[int] = set()

    def decide(row: FeatureRow) -> Side | None:
        """The side the model of the row's own fold takes. That model was fitted to earlier, ended labels only."""
        number = fold_of[row.n]
        if number not in scorers:
            try:
                scorers[number] = RidgeScorer(ridge_model(dataset, plan.folds[number].train, settings.penalty))
            except RidgeError:
                scorers[number] = None  # too few training rows left after the purge: the fold abstains
                untrained.add(number)
        scorer = scorers[number]
        found = scorer.score(row) if scorer is not None else None
        if found is None:
            return None
        expected.append(found.expected_micro)
        return found.side

    tested = [position for fold in plan.folds for position in fold.test]
    scored = evaluate(dataset, tested, decide, seed=settings.seed, config=config, ledger=ledger)  # counted first
    how = {"seed": settings.seed, "draws": settings.draws, "level_ppm": settings.level_ppm}
    interval = mean_per_trade(scored.model.by_day, **how).as_dict() if scored.model.trades else None
    trials = counted_trials(ledger, registry)
    daily = [sum(scored.model.by_day[day]) for day in sorted(scored.model.by_day)]
    sharpe: dict[str, object]
    try:
        sharpe = deflated_sharpe(daily, trials=trials, trial_variance=null_variance(len(daily))).as_dict()
        sharpe["trial_variance"] = "that of a ratio with no skill over the same periods: the trials' own were not kept"
    except ValidationError as exc:
        sharpe = {"ratio": None, "trials": trials, "why_not": str(exc)}
    candidate = ridge_model(dataset, development, settings.penalty)
    columns = by_column(dataset.features, [dataset.rows[position].features for position in development])
    if expected:
        columns[EXPECTED_KEY] = expected  # what the fold models expected of the trades they took
    summary = summarise(columns, settings.summary_bins, rows=len(development))
    held: dict[str, object] = {"weeks": len(plan.held_out), "read": 0}
    if hold_out and plan.held_out:
        rows = HoldOut(dataset, plan, ledger).read(config)  # written into the ledger before it is handed over
        scorer = RidgeScorer(candidate)

        def final(row: FeatureRow) -> Side | None:
            found = scorer.score(row)
            return found.side if found is not None else None

        found = three_way(rows, dataset.pair, final, seed=settings.seed)
        held = {"weeks": len(plan.held_out), "read": 1, **found.as_dict()}
    placeholders = {
        "cost_model": int(dataset.placeholder), "feasibility": int(feasibility_placeholder),
        "learn": int(settings.placeholder),
    }  # fmt: skip
    report: dict[str, object] = {
        "kind": "evaluation-report",
        "version": REPORT_VERSION,
        "not_a_result": int(any(placeholders.values())),
        "placeholders": placeholders,
        "pair": dataset.pair,
        "horizon_ns": dataset.horizon_ns,
        "config": frozen,
        "config_hash": config,
        "dataset": dataset.describe(),
        "rows": {"dropped": dict(dataset.dropped), "counted_by_side": dict(dataset.counted)},
        "plan": {
            "embargo_ns": plan.embargo_ns, "weeks": list(plan.weeks), "held_out": list(plan.held_out),
            "folds": plan.record(dataset.rows), "untrained_folds": sorted(untrained),
        },
        "three_way": scored.as_dict(),
        "interval": interval,
        "deflated_sharpe": sharpe,
        "trials": trials,
        "hold_out": held,
        "model": {"hash": candidate.content_hash, "kind": candidate.kind, "fitted_rows": len(development)},
        "serving": {"features": frozen["features"], "feasibility": feasibility},
        "summary": summary,
    }
    return Trained(dataset.pair, dataset.horizon_ns, candidate, (canonical(report) + "\n").encode("ascii"))


# ---- the store -----------------------------------------------------------------------------------------------------


def read_store(root: Path, pairs: Sequence[str]) -> tuple[list[RecordedEvent], dict[str, Instrument], list[str]]:
    """Every event of every day folder as one stream, what the days say the pairs are, and the days' names."""
    folders = store_days(root)
    if not folders:
        raise TrainRefused(f"{root} holds no recorded day")
    described: dict[str, Instrument] = {}
    for folder in folders:
        found = day_instruments(folder)
        for pair in pairs:
            if pair not in found:
                raise TrainRefused(f"{folder.name} does not describe {pair}; its prices cannot be read")
            if described.setdefault(pair, found[pair]) != found[pair]:
                raise TrainRefused(f"{folder.name} describes {pair} differently from the days before it")
    stream = [event for folder in folders for event in day_events(folder)]
    return stream, described, [folder.name for folder in folders]


def train_all(
    stream: Sequence[RecordedEvent], instruments: dict[str, Instrument], days: Sequence[str], settings: Settings, *,
    costs: CostModel, calendar: Calendar, feasibility: FeasibilitySettings, ledger: TrialLedger,
    registry: Path | None = None, hold_out: bool = False,
) -> list[Trained]:  # fmt: skip
    """Every configured pair and horizon, in the order they are written. The stream is held once."""
    out = []
    for pair in settings.pairs:
        made: list[Dataset] = []
        for horizon in settings.horizons_ns:
            made.append(
                build_for(
                    stream, instruments[pair], settings.features, costs, calendar, horizon_ns=horizon,
                    exit_wait_ns=settings.exit_wait_ns, stuck_increments=settings.stuck_increments, days=days,
                    checks=settings.leak_checks,
                )
            )  # fmt: skip
        # The feasibility map reads the stream as far as the last development week and no further.
        weeks = sorted({row.week_ns for dataset in made for row in dataset.rows})
        last = weeks[: max(0, len(weeks) - settings.hold_out_weeks)][-1:] or [0]
        seen = [event for event in stream if at_ns(event) <= last[0]] if settings.hold_out_weeks else stream
        mapped = feasibility_map(seen, {pair: instruments[pair]}, costs, calendar, feasibility)
        for dataset in made:
            out.append(
                train_one(
                    dataset, settings, ledger=ledger, registry=registry, feasibility=mapped,
                    feasibility_placeholder=feasibility.placeholder, hold_out=hold_out,
                )
            )  # fmt: skip
    return out




# ---- what is kept --------------------------------------------------------------------------------------------------


def keep_report(folder: Path, data: bytes) -> str:
    """Keep a report under its SHA-256, written whole and never written over. Returns the hash."""
    digest = hashlib.sha256(data).hexdigest()
    final = folder / f"{digest}.json"
    folder.mkdir(parents=True, exist_ok=True)
    if final.is_file() and final.read_bytes() == data:
        return digest
    partial = final.with_name(f".{digest}.partial")
    with partial.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(partial, final)
    except FileExistsError:
        if final.read_bytes() != data:
            raise ModelError(f"{final.name} already holds something else; it is not written over") from None
    finally:
        partial.unlink()
    return digest


def peak_bytes() -> int:
    """The most memory this process has held at once, as the operating system counted it."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # the one reports bytes, the others kilobytes


def say(text: str) -> None:
    print(f"train: {text}", file=sys.stderr, flush=True)


def _write_run(folder: Path, said: dict[str, object]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / (LAST_RUN + ".partial")
    partial.write_text(json.dumps(said, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, folder / LAST_RUN)


def run(args: argparse.Namespace, settings: Settings, clock: Clock, measure: Callable[[], int] = peak_bytes) -> int:
    calendar, costs = load_calendar(args.calendar), load_cost_model(args.cost_model)
    now = clock.now_ns()
    if calendar.is_open(now) and not args.dry_run:
        raise TrainRefused("the market is open: training runs at the weekend, with the trading processes stopped")
    root = args.store
    if root is None:
        loaded: Any = yaml.safe_load(args.recorder_config.read_text(encoding="utf-8"))
        root = Path(loaded["root"]) if isinstance(loaded, dict) and isinstance(loaded.get("root"), str) else None
    if root is None:
        raise TrainRefused(f"{args.recorder_config} does not say where the recordings are")
    stream, instruments, days = read_store(root, settings.pairs)
    ledger = TrialLedger(settings.ledger)
    trained = train_all(
        stream, instruments, days, settings, costs=costs, calendar=calendar,
        feasibility=load_settings(args.feasibility), ledger=ledger, registry=args.trial_registry,
        hold_out=args.hold_out,
    )  # fmt: skip
    peak = measure()
    within = peak <= settings.memory_budget_bytes
    said: dict[str, object] = {
        "at_ns": now, "ok": within, "dry_run": bool(args.dry_run), "days": days, "events": len(stream),
        "rows": sum(json.loads(one.report)["dataset"]["rows"] for one in trained),
        "memory": {"peak_bytes": peak, "budget_bytes": settings.memory_budget_bytes, "within": within},
        "candidates": [
            {"pair": one.pair, "horizon_ns": one.horizon_ns, "model": one.model.content_hash, "report": one.report_hash}
            for one in trained
        ],
    }  # fmt: skip
    if args.dry_run:
        for one in trained:
            sys.stdout.write(one.report.decode("ascii"))
        say(f"dry run: nothing registered. Peak memory {peak} bytes of a budget of {settings.memory_budget_bytes}.")
        return EXIT_OK if within else EXIT_FAILED
    if not within:
        _write_run(settings.state_dir, said)
        say(f"FAILED: peaked at {peak} bytes, over the budget of {settings.memory_budget_bytes}. Nothing registered.")
        return EXIT_FAILED
    models = ModelRegistry(settings.models)
    for one in trained:
        models.put(one.model)
        keep_report(settings.reports, one.report)
        lineup = read_lineup(settings.lineup)
        after = lineup.registered(one.named)
        if not after.same_models(lineup):
            append(settings.lineup, after, action=REGISTER, at_ns=now, report=one.report_hash)
        say(f"{one.pair} at {one.horizon_ns} ns: model {one.model.content_hash[:12]}, report {one.report_hash[:12]}")
    _write_run(settings.state_dir, said)
    print(json.dumps(said, sort_keys=True), flush=True)
    if any(json.loads(one.report)["not_a_result"] for one in trained):
        say("NOT A RESULT: the cost model, the feasibility settings or the learning settings are placeholders")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None, clock: Clock | None = None, measure: Callable[[], int] = peak_bytes
) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.learn.train", description="The weekend training run.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--cost-model", type=Path, default=COSTS)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--feasibility", type=Path, default=FEASIBILITY)
    parser.add_argument("--recorder-config", type=Path, default=RECORDER, help="says where the recordings are")
    parser.add_argument("--store", type=Path, help="the recordings (default: the recorder's `root`)")
    parser.add_argument("--trial-registry", type=Path, help="the signed trial count (default: the repository's)")
    parser.add_argument("--dry-run", action="store_true", help="evaluate and print; register nothing")
    parser.add_argument("--hold-out", action="store_true", help="score the candidate on the held-out weeks, once")
    args = parser.parse_args(argv)
    try:
        return run(args, load(args.config), clock or WallClock(), measure)
    except (TrainRefused, HoldOutSpent, LearnConfigError, CostModelError, CalendarError, FeasibilityError) as exc:
        say(f"refused: {exc}")
        return EXIT_REFUSED
    except LeakError as exc:
        say(f"refused: the dataset was not made: {exc}")
        return EXIT_REFUSED
    except (DatasetError, ValidationError, ModelError, LineupError, JournalError, CorruptSegment, OSError) as exc:
        say(f"FAILED: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
