"""Validation: splits that cannot leak, a hold-out that is read once, and statistics that count their trials (ADR 0021).

  walk-forward   one fold per trading week: test on that week, train on the weeks before it. A training row is
                 kept only if its label's window ended, and an embargo passed, before the first decision of the
                 test week: a label is usable only by a decision made after its window has ended. The purge
                 goes by each row's own window, however long; the embargo is the longest label window the
                 dataset was built with (`embargo_for`).
  hold-out       the last weeks of a dataset are in no fold. `HoldOut.read` hands them over once per frozen
                 configuration and writes that read into the trial ledger first; a second read is refused.
  three numbers  every evaluation scores, on the same rows and under the same costs: the oracle (in
                 hindsight the better side wherever one pays, and no trade elsewhere: more than any strategy
                 can have), the model, and a seeded coin. The no-trade baseline is the fourth line, and is zero.
  bootstrap      a confidence interval of the mean result per trade after costs, resampling whole trading
                 days: trades of one day stand or fall together, because they are not independent.
  deflated       the Sharpe ratio per period, and the probability that it beats the best of as many
  Sharpe         worthless trials as were actually counted (Bailey and Lopez de Prado, 2014).

Money is integer millionths of the quote currency. The bootstrap is exact: every replicate is a fraction of
two integers, its draws are a hash of the seed, and the same inputs give the same interval on any machine.
The Sharpe statistics are the only floats in this module. Their moments are computed exactly and each ratio is
converted once; the square roots and the normal distribution are the standard library's. They are numbers for a report:
no decision of the platform compares them, and no replay depends on them.

Rows that follow one another have label windows that overlap, so a count of rows is not a count of
independent trades. The day blocks of the bootstrap are what allows for that; nothing else here does.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from statistics import NormalDist
from typing import Final

from hftlab.core import Side
from hftlab.features import FeatureRow
from hftlab.learn.dataset import Dataset, Row
from hftlab.models.baseline import number
from hftlab.registry.ledger import TrialLedger

MILLION: Final = 1_000_000
EULER_GAMMA: Final = 0.5772156649015329
Decide = Callable[[FeatureRow], Side | None]


class ValidationError(ValueError):
    """What was asked for cannot be computed honestly: too little data, no embargo, or a level that is no level."""


class HoldOutSpent(ValidationError):
    """This frozen configuration has already read this hold-out. It is not read again."""


# ---- splits ----------------------------------------------------------------------------------------------------------


def embargo_for(windows_ns: Iterable[int]) -> int:
    """The embargo for labels with these windows (horizon plus latency): the longest of them."""
    return max(windows_ns)


@dataclass(frozen=True, slots=True)
class Fold:
    week_ns: int  # the test week, named by the Friday close that ends it
    train: tuple[int, ...]  # positions in the rows
    test: tuple[int, ...]
    purged: int  # earlier rows left out: their label's window, with the embargo, reaches the test week

    def boundaries(self, rows: Sequence[Row]) -> dict[str, int | None]:
        """Where the fold's data begins and ends, as times and row counts."""
        train, test = [rows[i] for i in self.train], [rows[i] for i in self.test]
        return {
            "week_ns": self.week_ns,
            "train_rows": len(train),
            "train_first_ns": train[0].known_ns if train else None,
            "train_last_end_ns": max((row.end_ns for row in train), default=None),
            "purged_rows": self.purged,
            "test_rows": len(test),
            "test_first_ns": test[0].known_ns,
            "test_last_ns": test[-1].known_ns,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    embargo_ns: int
    weeks: tuple[int, ...]  # the development weeks, oldest first
    held_out: tuple[int, ...]  # the weeks no fold reads
    folds: tuple[Fold, ...]

    def record(self, rows: Sequence[Row]) -> list[dict[str, int | None]]:
        return [fold.boundaries(rows) for fold in self.folds]


def walk_forward(rows: Sequence[Row], *, embargo_ns: int, min_train_weeks: int = 1, hold_out_weeks: int = 0) -> Plan:
    """Folds by trading week over the rows, in the order they were decided, with the last weeks held out."""
    if min_train_weeks < 1 or hold_out_weeks < 0:
        raise ValidationError("a fold trains on at least one earlier week, and a hold-out is not negative")
    if any(later.known_ns < earlier.known_ns for earlier, later in pairwise(rows)):
        raise ValidationError("the rows are not in the order they were decided")
    if embargo_ns <= 0:
        raise ValidationError("the embargo is the longest label window, and that is longer than nothing")
    by_week: dict[int, list[int]] = {}
    for position, row in enumerate(rows):
        by_week.setdefault(row.week_ns, []).append(position)
    weeks = sorted(by_week)
    kept = weeks[: max(0, len(weeks) - hold_out_weeks)]
    held = weeks[len(kept) :]
    folds = []
    for index in range(min_train_weeks, len(kept)):
        test = by_week[kept[index]]
        starts = rows[test[0]].known_ns
        earlier = [position for week in kept[:index] for position in by_week[week]]
        train = tuple(position for position in earlier if rows[position].end_ns + embargo_ns < starts)
        folds.append(Fold(kept[index], train, tuple(test), len(earlier) - len(train)))
    return Plan(embargo_ns, tuple(kept), tuple(held), tuple(folds))


class HoldOut:
    """The untouched weeks of a dataset. They are handed over once per frozen configuration, and never in silence."""

    def __init__(self, dataset: Dataset, plan: Plan, ledger: TrialLedger) -> None:
        if not plan.held_out:
            raise ValidationError("the plan holds no week out")
        weeks = set(plan.held_out)
        self._rows = tuple(row for row in dataset.rows if row.week_ns in weeks)
        self._ledger = ledger
        self.subject = f"{dataset.content_hash[:16]}:{plan.held_out[0]}:{len(plan.held_out)}"

    def read(self, config: str) -> tuple[Row, ...]:
        """The hold-out rows, for the configuration with this hash. The read is written down before it happens."""
        if self._ledger.spent(config, self.subject):
            raise HoldOutSpent(f"configuration {config} has already read hold-out {self.subject}")
        self._ledger.record(config, "hold_out", self.subject)
        return self._rows


# ---- three numbers, side by side -------------------------------------------------------------------------------------


@dataclass(slots=True)
class Tally:
    """One policy's trades on a set of rows: counts and sums, from which win rate, payoff and mean follow."""

    trades: int = 0
    total_micro: int = 0
    wins: int = 0
    losses: int = 0
    won_micro: int = 0
    lost_micro: int = 0
    by_day: dict[int, list[int]] = field(default_factory=dict)

    def add(self, day_ns: int, result: int) -> None:
        self.trades += 1
        self.total_micro += result
        self.wins += result > 0
        self.losses += result < 0
        self.won_micro += max(result, 0)
        self.lost_micro += max(-result, 0)
        self.by_day.setdefault(day_ns, []).append(result)

    def as_dict(self) -> dict[str, object]:
        return {
            "trades": self.trades,
            "total_micro": self.total_micro,
            "mean_micro": self.total_micro // self.trades if self.trades else None,  # rounded down
            "wins": self.wins,
            "losses": self.losses,
            "won_micro": self.won_micro,
            "lost_micro": self.lost_micro,
            "days": len(self.by_day),
        }


@dataclass(frozen=True, slots=True)
class Report:
    rows: int
    seed: int
    oracle: Tally  # hindsight: never a strategy's result
    model: Tally
    random: Tally
    no_trade: Tally

    @property
    def oracle_clears_cost(self) -> bool:
        """Does any row pay after costs, even with perfect foresight? If not, there is nothing here to model."""
        return self.oracle.total_micro > 0

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "money": "millionths of the pair's quote currency, after costs, for one trade of the dataset's size",
            "oracle": {**self.oracle.as_dict(), "hindsight": True},
            "model": self.model.as_dict(),
            "random": {**self.random.as_dict(), "seed": self.seed},
            "no_trade": self.no_trade.as_dict(),
            "oracle_clears_cost": self.oracle_clears_cost,
        }


def three_way(rows: Sequence[Row], pair: str, decide: Decide, *, seed: int) -> Report:
    """The oracle, the model and a seeded coin on the same rows. The model is shown the features and nothing else."""
    oracle, model, coin = Tally(), Tally(), Tally()
    for row in rows:
        if max(row.buy_micro, row.sell_micro) > 0:
            oracle.add(row.day_ns, max(row.buy_micro, row.sell_micro))
        side = decide(FeatureRow(pair, row.n, row.known_ns, row.features))
        if side is not None:
            model.add(row.day_ns, row.buy_micro if side is Side.BUY else row.sell_micro)
        coin.add(row.day_ns, row.buy_micro if number(seed, row.n, row.known_ns) & 1 else row.sell_micro)
    return Report(len(rows), seed, oracle, model, coin, Tally())


def evaluate(
    dataset: Dataset, positions: Sequence[int], decide: Decide, *, seed: int, config: str, ledger: TrialLedger
) -> Report:
    """An evaluation on development rows: the configuration is counted as a trial first, then scored."""
    ledger.record(config, "walk_forward", dataset.content_hash[:16])
    return three_way([dataset.rows[position] for position in positions], dataset.pair, decide, seed=seed)


# ---- the day-block bootstrap -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interval:
    point: Fraction  # the statistic on the days as they are
    lower: Fraction
    upper: Fraction
    days: int
    draws: int
    level_ppm: int
    seed: int

    def as_dict(self) -> dict[str, int]:
        """Whole millionths: the point and the lower bound rounded down, the upper bound rounded up."""
        return {
            "point_micro": math.floor(self.point),
            "lower_micro": math.floor(self.lower),
            "upper_micro": math.ceil(self.upper),
            "days": self.days,
            "draws": self.draws,
            "level_ppm": self.level_ppm,
            "seed": self.seed,
        }


def _resample(
    days: int, statistic: Callable[[Sequence[int]], Fraction], *, seed: int, draws: int, level_ppm: int
) -> Interval:
    if days < 1 or draws < 1 or not 0 < level_ppm < MILLION:
        raise ValidationError("a bootstrap needs a day with a trade, a draw, and a level between 0 and 1,000,000 ppm")
    found = sorted(statistic([number(seed, draw, slot) % days for slot in range(days)]) for draw in range(draws))
    rank = max(1, -(-draws * (MILLION - level_ppm) // (2 * MILLION)))  # nearest rank: a value that was drawn
    return Interval(statistic(range(days)), found[rank - 1], found[draws - rank], days, draws, level_ppm, seed)


def _mean(blocks: Sequence[Sequence[int]], drawn: Sequence[int]) -> Fraction:
    count = sum(len(blocks[slot]) for slot in drawn)
    return Fraction(sum(sum(blocks[slot]) for slot in drawn), count) if count else Fraction(0)


def mean_per_trade(by_day: Mapping[int, Sequence[int]], *, seed: int, draws: int, level_ppm: int) -> Interval:
    """A confidence interval of the mean result per trade, resampling the trading days that had a trade."""
    blocks = [tuple(by_day[day]) for day in sorted(by_day) if by_day[day]]
    return _resample(len(blocks), lambda drawn: _mean(blocks, drawn), seed=seed, draws=draws, level_ppm=level_ppm)


def advantage(
    challenger: Mapping[int, Sequence[int]], champion: Mapping[int, Sequence[int]], *, seed: int, draws: int,
    level_ppm: int,
) -> Interval:  # fmt: skip
    """A confidence interval of the challenger's mean per trade less the champion's, resampling the same days for both.

    A side with no trade on the drawn days has a mean of zero there: not trading earns nothing.
    """
    days = [day for day in sorted({*challenger, *champion}) if challenger.get(day) or champion.get(day)]
    ours = [tuple(challenger.get(day, ())) for day in days]
    theirs = [tuple(champion.get(day, ())) for day in days]
    return _resample(
        len(days), lambda drawn: _mean(ours, drawn) - _mean(theirs, drawn), seed=seed, draws=draws, level_ppm=level_ppm
    )


def drawdown(by_day: Mapping[int, Sequence[int]]) -> int:
    """The largest fall of the running total from an earlier high, trade by trade in day order. Never negative."""
    total = high = worst = 0
    for day in sorted(by_day):
        for result in by_day[day]:
            total += result
            high = max(high, total)
            worst = max(worst, high - total)
    return worst


# ---- the Sharpe ratio, deflated by the trials counted ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sharpe:
    """Mean over standard deviation of the results per period. Per period: it is not annualised here."""

    ratio: float
    periods: int
    interval: str
    skew: float
    kurtosis: float


@dataclass(frozen=True, slots=True)
class Deflated:
    sharpe: Sharpe
    trials: int
    benchmark: float  # the Sharpe ratio the best of that many worthless trials is expected to show
    probability: float  # that the true ratio is above the benchmark


def sharpe(results: Sequence[int], interval: str = "trading day") -> Sharpe:
    """The Sharpe ratio of whole-number results, one per period. Needs three periods that are not all alike."""
    periods = len(results)
    mean = Fraction(sum(results), periods) if periods else Fraction(0)
    second, third, fourth = (Fraction(sum((x - mean) ** k for x in results), max(periods, 1)) for k in (2, 3, 4))
    if periods < 3 or second == 0:
        raise ValidationError("a Sharpe ratio needs at least three periods whose results differ")
    # Each ratio is formed exactly and turned into a float once, so the unit the results are in cannot change a bit.
    ratio = _signed_root(mean, mean**2 * (periods - 1) / (second * periods))
    return Sharpe(ratio, periods, interval, _signed_root(third, third**2 / second**3), float(fourth / second**2))


def _signed_root(sign_of: Fraction, square: Fraction) -> float:
    root = math.sqrt(float(square))
    return -root if sign_of < 0 else root


def null_variance(periods: int) -> float:
    """The variance of the Sharpe ratio of a strategy with no skill over this many periods: for use when the

    ratios of the trials themselves were not kept.
    """
    if periods < 2:
        raise ValidationError("the variance of a Sharpe ratio needs at least two periods")
    return 1.0 / (periods - 1)


def deflated_sharpe(
    results: Sequence[int], *, trials: int, trial_variance: float = 0.0, interval: str = "trading day"
) -> Deflated:
    """The Sharpe ratio of `results` against the best of `trials` worthless ones.

    `trials` is the number of configurations actually counted (`hftlab.registry.counted_trials`), and
    `trial_variance` the variance of their Sharpe ratios. With one trial the benchmark is zero.
    """
    found = sharpe(results, interval)
    if trials < 1 or (trials > 1 and trial_variance <= 0.0):
        raise ValidationError("at least one trial, and with more than one the variance of their Sharpe ratios")
    benchmark = 0.0
    if trials > 1:
        normal = NormalDist()
        near, far = normal.inv_cdf(1 - 1 / trials), normal.inv_cdf(1 - 1 / (trials * math.e))
        benchmark = math.sqrt(trial_variance) * ((1 - EULER_GAMMA) * near + EULER_GAMMA * far)
    shape = 1 - found.skew * found.ratio + (found.kurtosis - 1) / 4 * found.ratio**2
    if shape <= 0:
        raise ValidationError("the results are too skewed for the deflated Sharpe ratio to be defined")
    score = (found.ratio - benchmark) * math.sqrt(found.periods - 1) / math.sqrt(shape)
    return Deflated(found, trials, benchmark, NormalDist().cdf(score))
