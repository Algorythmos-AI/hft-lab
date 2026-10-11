"""Shared pieces for the lineup tests: a registry of models that trade a planted effect, and a desk serving them."""
from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

from hftlab import lineup
from hftlab.journal import Journal, canonical
from hftlab.learn.config import Boosting
from hftlab.learn.dataset import build
from hftlab.learn.drift import EXPECTED_KEY, by_column, summarise
from hftlab.learn.train import gbm_model, keep_report, ridge_model
from hftlab.lineup import Lineup, Named
from hftlab.models import ModelRegistry
from hftlab.record.format import describe
from hftlab.trader.session import day_label
from hftlab.trader.stages import day_of
from tests.learn.kit import CONFIG, WIN, features, labels, planted
from tests.strategy.test_ml import the_map
from tests.trader.kit import NEVER, Desk, raw_settings
from tests.venues.kit import EURUSD, MS

MARGIN = 5_000_000
ML, CHALLENGER = "ml", "ml-challenger-1"
SERVING = {
    "lookbacks_ns": list(CONFIG.lookbacks_ns), "rate_window_ns": CONFIG.rate_window_ns,
    "range_window_ns": CONFIG.range_window_ns, "stale_after_ns": CONFIG.stale_after_ns,
}  # fmt: skip


BOOSTING = Boosting(trees=20, max_depth=3, leaves=8, learning_rate_ppm=200_000, min_leaf_rows=5, bins=63,
                    l2_ppm=1_000_000, leaf_scale=1000, seed=1)  # fmt: skip


def register(
    registry: Path, seed: int = 1, *, blocks: int = 40, not_a_result: int = 1, kind: str = "ridge",
    fitted_days: dict[str, int] | None = None,
) -> Named:  # fmt: skip
    """Fit a model to a planted stream and keep it, with a report that says how it is served, by their hashes.

    A ridge model unless `kind` is `gbm`, which needs the optional ML set to be fitted. The report says the model
    was fitted to five days of the platform's own recordings unless `fitted_days` says otherwise, by source.
    """
    stream = planted(seed, blocks)
    made = build(stream, features(), labels())
    everything = range(len(made.rows))
    model = gbm_model(made, everything, BOOSTING) if kind == "gbm" else ridge_model(made, everything, Fraction(1, 1000))
    columns = by_column(made.features, [row.features for row in made.rows])
    columns[EXPECTED_KEY] = [WIN, -WIN, WIN // 2, 0, WIN]
    report = {
        "kind": "evaluation-report", "version": 1, "not_a_result": not_a_result, "pair": model.pair,
        "horizon_ns": model.label["horizon_ns"], "model": {"hash": model.content_hash, "kind": model.kind},
        "serving": {"features": SERVING, "feasibility": the_map(stream)},
        "summary": summarise(columns, 5, rows=len(made.rows)),
        "sources": {"model": {"days": {"recorded": 5} if fitted_days is None else fitted_days}},
    }  # fmt: skip
    ModelRegistry(registry / "models").put(model)
    return Named(model.content_hash, keep_report(registry / "reports", (canonical(report) + "\n").encode("ascii")))


def place(name: str, slot: str, **changes) -> dict:
    return {"name": name, "kind": "ml", "slot": slot, "margin_micro": MARGIN, "retry_seconds": 2,
            "scorer": "in_process", **changes}  # fmt: skip


def settings(registry: Path, **changes) -> dict:
    """The trader's settings with a champion's place and one challenger's beside the idle and control sleeves."""
    raw = raw_settings()
    ladder = {"start": "shadow", "promote": {"min_size": dict(NEVER), "normal": dict(NEVER)}}
    raw["sleeves"] = [*raw["sleeves"], place(ML, "champion"), place(CHALLENGER, "challenger-1")]
    raw["rollout"] = {**raw["rollout"], ML: dict(ladder), CHALLENGER: dict(ladder)}
    return {**raw, "registry": str(registry), **changes}


def put(folder: Path, found: Lineup, action: str = lineup.KEEP, at_ns: int = 1) -> Lineup:
    return lineup.append(folder / lineup.FILE, found, action=action, at_ns=at_ns)


def desk(folder: Path, registry: Path, **options) -> Desk:
    return Desk(folder, paper=options.pop("paper", False), settings=settings(registry), **options)


def effect(made: Desk, seed: int, blocks: int) -> int:
    """Script the planted stream into the desk's market from where its quotes ended. Returns its length in seconds."""
    for quote in planted(seed, blocks, start_ns=made.quoted + MS):
        made.market.script_quote(quote.recv_ts_ns, "EUR.USD", quote.bid, quote.ask, quote.bid_size, quote.ask_size)
    made.market.script_quote(made.quoted + MS, "USD.JPY", 151_230, 151_240)
    made.quoted += blocks * 3_000 * MS
    return blocks * 3


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- a week of the trader's journals, written by hand ----------------------------------------------------------------

DAY = 24 * 3_600 * 1_000 * MS
MONDAY = 1_760_000_000 * 1_000 * MS + 4 * DAY  # 2025-10-13 08:53 UTC: the market is open
SUNDAY = MONDAY + 6 * DAY  # 2025-10-19 08:53 UTC: the weekend, before the open
UNITS = 1_000  # the size of every trade below: one price unit is 10,000 millionths of a dollar


class Days:
    """Writes decision journals as the trader does: one folder per trading day, sessions, fills, latches, breaks."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir, self._fills = state_dir, 0

    def session(self, at_ns: int, serving: dict, trips=(), *, latches=(), breaks: int = 0, fee: int = 0) -> None:
        """One session at `at_ns`. `trips` is (sleeve, result in price units) for each round trip, in order."""
        journal = Journal(self.state_dir / "days" / day_label(day_of(at_ns)) / "decisions.jsonl")
        journal.open()
        journal.write("start", n=0, at_ns=at_ns, serving=serving, instruments=[describe(EURUSD)])
        for number, (sleeve, units) in enumerate(trips):
            at = at_ns + (number + 1) * 1_000 * MS
            for side, price in (("BUY", 108_500), ("SELL", 108_500 + units)):
                self._fills += 1
                fill = {"sleeve": sleeve, "fill_id": f"F{self._fills}", "pair": "EUR.USD", "side": side}
                journal.write("shadow_fill", n=1, at_ns=at, quantity=UNITS, price=price, **fill)
                if fee:
                    paid = {"fill_id": fill["fill_id"], "commission": fee, "currency": "USD"}
                    journal.write("shadow_commission", n=1, at_ns=at, **paid)
        for latch in latches:
            journal.write("latch", n=2, at_ns=at_ns, latch=latch, tripped=1)
        for _ in range(breaks):
            journal.write("break", n=2, at_ns=at_ns, broken=1, blind=0)
        journal.write("stop", n=3, at_ns=at_ns)
        journal.close()

    def week(self, monday: int, serving: dict[str, str | None], by_sleeve: dict[str, list[int]], **more) -> None:
        """Five trading days from `monday`, each with the same round trips for each sleeve."""
        trips = [(sleeve, units) for sleeve, results in by_sleeve.items() for units in results]
        for day in range(5):
            self.session(monday + day * DAY, serving, trips, **(more if day == 2 else {}))
