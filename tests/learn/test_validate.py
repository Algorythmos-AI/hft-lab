"""Validation: folds that cannot see a label before it ended, a hold-out read once, and statistics that count trials."""
from __future__ import annotations

import dataclasses
import math
from fractions import Fraction
from statistics import NormalDist

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.calendar import next_close
from hftlab.core import Side
from hftlab.features import FeatureRow
from hftlab.journal import JournalError
from hftlab.learn import validate as v
from hftlab.learn.dataset import Row
from hftlab.registry import TrialLedger, counted_trials
from tests.learn.kit import COST, IMBALANCE, WEEK, WIN, dataset, planted
from tests.venues.kit import MS, SECOND, T0

HOUR = 3_600 * SECOND
WINDOW = SECOND + 5 * MS
FIRST_CLOSE = next_close(T0)


def row(known_ns: int, *, window_ns: int = WINDOW, buy: int = 1, sell: int = -1, n: int | None = None) -> Row:
    day = known_ns - known_ns % (24 * HOUR)
    return Row(n or known_ns // MS % 10**9, known_ns, known_ns + window_ns, day, next_close(known_ns), (0,), buy, sell)


def weeks_of_rows(weeks: int, per_week: int = 6, gap_ns: int = HOUR) -> list[Row]:
    """`per_week` decisions an hour apart, ending an hour before each Friday close."""
    return [
        row(FIRST_CLOSE + week * WEEK - (per_week - k) * gap_ns)
        for week in range(weeks) for k in range(per_week)
    ]  # fmt: skip


# ---- walk-forward by trading week ------------------------------------------------------------------------------------


def test_each_fold_tests_one_week_and_trains_on_the_weeks_before_it():
    rows = weeks_of_rows(4)
    plan = v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=(SECOND,))
    assert plan.weeks == tuple(FIRST_CLOSE + k * WEEK for k in range(4)) and plan.held_out == ()
    assert [fold.week_ns for fold in plan.folds] == list(plan.weeks[1:]), "the first week has no earlier one"
    for number, fold in enumerate(plan.folds, 1):
        assert fold.test == tuple(range(6 * number, 6 * number + 6)) and fold.train == tuple(range(6 * number))
        assert fold.purged == 0
    later = v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=(SECOND,), min_train_weeks=3)
    assert [fold.week_ns for fold in later.folds] == [plan.weeks[3]]


def test_a_label_whose_window_reaches_into_the_next_fold_is_removed_from_its_training_rows():
    rows = weeks_of_rows(3)
    monday = rows[6].known_ns  # the first decision of the second week
    planted_at = 5  # the last decision of the first week, given a label that only ends inside the second
    rows[planted_at] = dataclasses.replace(rows[planted_at], end_ns=monday + 10 * SECOND)
    plan = v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=(SECOND,))
    second, third = plan.folds
    assert planted_at not in second.train and second.train == (0, 1, 2, 3, 4) and second.purged == 1
    assert planted_at in third.train, "a week later its window has long ended, and it may train"
    honest = v.walk_forward(weeks_of_rows(3), embargo_ns=WINDOW, horizons_ns=(SECOND,))
    assert planted_at in honest.folds[0].train, "the same decision with a label that ended in time is kept"


def test_a_label_must_have_ended_a_whole_embargo_before_the_first_decision_of_the_test_week():
    start = FIRST_CLOSE + 2 * 24 * HOUR + 15 * 60 * SECOND  # the week after opens
    before = [row(start - WEEK + k * SECOND) for k in range(3)]
    for gap, kept in ((2 * WINDOW + 1, True), (2 * WINDOW, False), (WINDOW, False), (1, False)):
        edge = dataclasses.replace(row(start - gap), week_ns=before[0].week_ns)  # the last one before the test week
        rows = [*before, edge, row(start), row(start + SECOND)]
        plan = v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=(SECOND,))
        assert (3 in plan.folds[0].train) == kept, f"decided {gap} ns before: its label ends {gap - WINDOW} ns before"
        assert plan.folds[0].purged == (0 if kept else 1)


ROWS = st.lists(
    st.tuples(st.integers(0, 40), st.integers(1, 3 * 24), st.integers(1, 200), st.booleans()), min_size=1, max_size=40
)


@given(ROWS, st.integers(1, 100), st.integers(1, 3), st.integers(0, 3))
def test_no_fold_trains_on_a_label_that_had_not_ended_or_on_a_week_held_out(steps, embargo_h, min_train, held):
    at, rows = FIRST_CLOSE - 5 * 24 * HOUR, []
    for wait_h, _, window_h, long in steps:
        at += wait_h * HOUR
        while next_close(at) - at > 5 * 24 * HOUR - 15 * 60 * SECOND or next_close(at) == at:
            at += HOUR  # the weekend: no decision is made while the market is shut
        rows.append(row(at, window_ns=window_h * HOUR * (50 if long else 1), n=len(rows) + 1))
    how = {"min_train_weeks": min_train, "hold_out_weeks": held}
    plan = v.walk_forward(rows, embargo_ns=embargo_h * HOUR, horizons_ns=(HOUR,), **how)
    all_weeks = sorted({r.week_ns for r in rows})
    assert plan.weeks + plan.held_out == tuple(all_weeks) and len(plan.held_out) == min(held, len(all_weeks))
    assert [fold.week_ns for fold in plan.folds] == list(plan.weeks[min_train:])
    for fold in plan.folds:
        first = rows[fold.test[0]].known_ns
        assert [rows[i].week_ns for i in fold.test] == [fold.week_ns] * len(fold.test)
        assert set(fold.test) == {i for i, r in enumerate(rows) if r.week_ns == fold.week_ns}
        earlier = [i for i, r in enumerate(rows) if r.week_ns < fold.week_ns]
        assert set(fold.train) <= set(earlier) and fold.purged == len(earlier) - len(fold.train)
        assert all(rows[i].end_ns + plan.embargo_ns < first for i in fold.train)
        assert all(rows[i].end_ns + plan.embargo_ns >= first for i in set(earlier) - set(fold.train))
        assert not {rows[i].week_ns for i in (*fold.train, *fold.test)} & set(plan.held_out)


def test_the_boundaries_of_every_fold_are_recorded_as_times_and_row_counts():
    rows = weeks_of_rows(3)
    rows[5] = dataclasses.replace(rows[5], end_ns=rows[6].known_ns)
    record = v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=(SECOND,)).record(rows)
    assert record[0] == {
        "week_ns": FIRST_CLOSE + WEEK, "train_rows": 5, "train_first_ns": rows[0].known_ns,
        "train_last_end_ns": rows[4].end_ns, "purged_rows": 1, "test_rows": 6, "test_first_ns": rows[6].known_ns,
        "test_last_ns": rows[11].known_ns,
    }  # fmt: skip
    assert record[1]["train_rows"] == 12 and record[1]["test_rows"] == 6
    assert v.embargo_for([SECOND + 5 * MS, 60 * SECOND + 5 * MS, 5 * SECOND]) == 60 * SECOND + 5 * MS


@pytest.mark.parametrize("wrong", [{"embargo_ns": 0}, {"embargo_ns": WINDOW, "min_train_weeks": 0},
                                   {"embargo_ns": WINDOW, "hold_out_weeks": -1}])  # fmt: skip
def test_a_plan_without_an_embargo_or_without_training_weeks_is_refused(wrong):
    with pytest.raises(v.ValidationError):
        v.walk_forward(weeks_of_rows(2), horizons_ns=(SECOND,), **wrong)
    with pytest.raises(v.ValidationError, match="not in the order"):
        v.walk_forward(weeks_of_rows(2)[::-1], embargo_ns=WINDOW, horizons_ns=(SECOND,))


def test_an_embargo_shorter_than_the_longest_label_horizon_it_is_given_is_refused():
    """The embargo exists to keep a label out of the fold it reaches into, so it can never be shorter than one."""
    rows = weeks_of_rows(3)
    for horizons in ((SECOND, 60 * SECOND), (60 * SECOND,), (WINDOW + 1,)):
        with pytest.raises(v.ValidationError, match="shorter than the longest label horizon"):
            v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=horizons)
    with pytest.raises(v.ValidationError, match="no horizon was given"):
        v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=())
    with pytest.raises(TypeError):
        v.walk_forward(rows, embargo_ns=WINDOW)  # there is no default to fall back on
    assert v.walk_forward(rows, embargo_ns=60 * SECOND, horizons_ns=(SECOND, 60 * SECOND)).embargo_ns == 60 * SECOND
    assert v.walk_forward(rows, embargo_ns=WINDOW, horizons_ns=(WINDOW,)).folds, "equal to the longest is enough"


# ---- the hold-out, and the trials it counts --------------------------------------------------------------------------


def three_weeks():
    stream = [q for week in range(3) for q in planted(week, 4, start_ns=T0 + week * WEEK, first_seq=1 + week * 1_000)]
    made = dataset(stream)
    return made, v.walk_forward(made.rows, embargo_ns=made.window_ns, horizons_ns=(made.horizon_ns,), hold_out_weeks=1)


def test_the_held_out_weeks_are_in_no_fold_and_are_read_once_per_frozen_configuration(tmp_path):
    made, plan = three_weeks()
    assert len(plan.weeks) == 2 and plan.held_out == (FIRST_CLOSE + 2 * WEEK,) and len(plan.folds) == 1
    assert all(made.rows[i].week_ns in plan.weeks for fold in plan.folds for i in (*fold.train, *fold.test))
    ledger = TrialLedger(tmp_path / "state" / "trials.jsonl")
    held = v.HoldOut(made, plan, ledger)
    rows = held.read("config-a")
    assert rows and {r.week_ns for r in rows} == set(plan.held_out) and len(rows) == sum(
        r.week_ns == plan.held_out[0] for r in made.rows
    )
    with pytest.raises(v.HoldOutSpent, match="config-a has already read"):
        held.read("config-a")
    with pytest.raises(v.HoldOutSpent):
        v.HoldOut(made, plan, TrialLedger(ledger.path)).read("config-a")  # a new process finds the same ledger
    assert v.HoldOut(made, plan, ledger).read("config-b") == rows, "another frozen configuration has its own one read"
    assert ledger.trials() == 2 and [e["what"] for e in ledger.entries()] == ["hold_out", "hold_out"]
    assert held.subject == f"{made.content_hash[:16]}:{plan.held_out[0]}:1"
    with pytest.raises(v.ValidationError, match="holds no week out"):
        v.HoldOut(made, v.walk_forward(made.rows, embargo_ns=made.window_ns, horizons_ns=(made.horizon_ns,)), ledger)


def test_the_trial_count_is_the_larger_of_the_signed_registry_and_the_ledger(tmp_path):
    made, plan = three_weeks()
    ledger = TrialLedger(tmp_path / "trials.jsonl")
    assert ledger.trials() == 0 and counted_trials(ledger) == 0 and counted_trials() == 0
    for config in ("a", "b", "a", "c"):
        v.evaluate(made, plan.folds[0].test, lambda _: None, seed=1, config=config, ledger=ledger)
    assert ledger.trials() == 3, "the same frozen configuration evaluated twice is one trial"
    assert [e["what"] for e in ledger.entries()] == ["walk_forward"] * 4
    signed = tmp_path / "trial_registry.yaml"
    signed.write_text("history:\n  - {decision: HFT-DEC-0003, total: 2, status: accepted}\n")
    assert counted_trials(ledger, signed) == 3, "a registry that fell behind does not make a result look better"
    signed.write_text("history:\n  - {decision: HFT-DEC-0003, total: 9, status: accepted}\n")
    assert counted_trials(ledger, signed) == 9 and counted_trials(None, signed) == 9
    with pytest.raises(ValueError, match="a trial is one of"):
        ledger.record("a", "peek", "x")
    ledger.path.write_text(ledger.path.read_text().replace('"config":"b"', '"config":"a"'))
    with pytest.raises(JournalError):
        ledger.trials()


def test_an_evaluation_is_counted_before_it_is_scored(tmp_path):
    made, plan = three_weeks()
    ledger = TrialLedger(tmp_path / "trials.jsonl")

    def decide(_: FeatureRow) -> Side | None:
        assert ledger.trials() == 1, "the trial was written down before the first row was looked at"
        return Side.BUY

    report = v.evaluate(made, plan.folds[0].test, decide, seed=1, config="frozen", ledger=ledger)
    assert report.rows == len(plan.folds[0].test) == report.model.trades


# ---- oracle, model and random on the same rows -----------------------------------------------------------------------


def test_every_evaluation_reports_the_oracle_the_model_and_a_seeded_coin_on_the_same_rows():
    made = dataset(planted(7, 12))
    signals = sum(1 for r in made.rows if r.features[IMBALANCE])

    def follow(shown: FeatureRow) -> Side | None:
        leaning = shown.values[IMBALANCE]
        return None if not leaning else Side.BUY if leaning > 0 else Side.SELL

    report = v.three_way(made.rows, made.pair, follow, seed=11)
    assert report.rows == len(made.rows) == report.random.trades and report.oracle.trades == signals
    assert report.model.trades == signals and report.model.total_micro == signals * WIN == report.model.won_micro
    assert report.oracle.total_micro == signals * WIN, "wherever one side pays: the model found them all"
    assert report.no_trade.trades == 0 and report.no_trade.total_micro == 0
    assert report.random.total_micro < report.no_trade.total_micro < report.model.total_micro
    said = report.as_dict()
    assert said["oracle"]["hindsight"] is True and "hindsight" not in said["model"] and said["random"]["seed"] == 11
    assert list(said) == ["rows", "money", "oracle", "model", "random", "no_trade", "oracle_clears_cost"]
    assert said["model"] == {"trades": signals, "total_micro": signals * WIN, "mean_micro": WIN, "wins": signals,
                             "losses": 0, "won_micro": signals * WIN, "lost_micro": 0, "days": 1, "unfilled": 0,
                             "late_exits": 0, "penalised": 0}  # fmt: skip
    assert said["no_trade"]["mean_micro"] is None and said["oracle_clears_cost"] is True
    still = dataset(planted(7, 12, effect=False))
    assert v.three_way(still.rows, still.pair, follow, seed=11).as_dict()["oracle_clears_cost"] is False, (
        "on a book that never moves even hindsight loses the cost: nothing is worth modelling there"
    )
    assert v.three_way(made.rows, made.pair, follow, seed=11).as_dict() == said
    assert v.three_way(made.rows, made.pair, follow, seed=12).random.total_micro != report.random.total_micro


def test_the_model_is_shown_the_features_of_a_row_and_nothing_of_its_label():
    made = dataset(planted(7, 4))
    seen = []
    v.three_way(made.rows, made.pair, lambda shown: seen.append(shown), seed=1)
    assert len(seen) == len(made.rows) and all(type(shown) is FeatureRow for shown in seen)
    assert [(s.n, s.known_ns, s.values) for s in seen] == [(r.n, r.known_ns, r.features) for r in made.rows]
    assert {f.name for f in dataclasses.fields(FeatureRow)} == {"pair", "n", "known_ns", "values"}


OUTCOMES = st.lists(st.tuples(st.integers(-10**8, 10**8), st.integers(-10**8, 10**8), st.integers(0, 2)), max_size=40)


@given(OUTCOMES, st.integers(0, 2**32))
def test_nothing_beats_the_oracle_and_every_tally_adds_up(outcomes, seed):
    rows = [row(T0 + k * SECOND, buy=buy, sell=sell, n=k + 1) for k, (buy, sell, _) in enumerate(outcomes)]
    choice = {k + 1: (Side.BUY, Side.SELL, None)[pick] for k, (_, _, pick) in enumerate(outcomes)}
    report = v.three_way(rows, "EUR.USD", lambda shown: choice[shown.n], seed=seed)
    for tally in (report.oracle, report.model, report.random, report.no_trade):
        assert tally.won_micro - tally.lost_micro == tally.total_micro and tally.wins + tally.losses <= tally.trades
        assert sum(len(day) for day in tally.by_day.values()) == tally.trades
    assert report.oracle.total_micro >= max(report.random.total_micro, report.model.total_micro, 0)
    best = {r.n: max(r.buy_micro, r.sell_micro, 0) for r in rows}
    took = {r.n: r.buy_micro if choice[r.n] is Side.BUY else r.sell_micro for r in rows if choice[r.n] is not None}
    assert report.model.total_micro == sum(took.values()) and report.oracle.total_micro == sum(best.values())
    assert report.oracle_clears_cost == any(best.values()) and report.random.trades == len(rows)


# ---- the day-block bootstrap -----------------------------------------------------------------------------------------


def test_the_interval_of_results_that_are_all_alike_is_that_result():
    found = v.mean_per_trade({1: [-COST] * 5, 2: [-COST] * 3, 3: []}, seed=1, draws=200, level_ppm=950_000)
    assert found.point == found.lower == found.upper == -COST and found.days == 2, "a day without a trade is no block"
    assert found.as_dict() == {"point_micro": -COST, "lower_micro": -COST, "upper_micro": -COST, "days": 2,
                               "draws": 200, "level_ppm": 950_000, "seed": 1}  # fmt: skip


def test_whole_days_are_resampled_so_every_replicate_is_a_mix_of_days():
    by_day = {1: [10, 10, 10, 10], 2: [-50], 3: [4, 6]}
    means = {Fraction(40 * a - 50 * b + 10 * c, 4 * a + b + 2 * c) for a in range(4) for b in range(4 - a)
             for c in [3 - a - b]}  # fmt: skip
    found = v.mean_per_trade(by_day, seed=5, draws=400, level_ppm=900_000)
    assert found.point == Fraction(0) and found.lower in means and found.upper in means
    assert found.lower < found.point < found.upper
    narrow = v.mean_per_trade(by_day, seed=5, draws=400, level_ppm=500_000)
    assert found.lower <= narrow.lower <= narrow.upper <= found.upper, "a higher level is a wider interval"
    assert v.mean_per_trade(by_day, seed=5, draws=400, level_ppm=900_000) == found, "the same seed, the same interval"
    assert v.mean_per_trade(dict(reversed(by_day.items())), seed=5, draws=400, level_ppm=900_000) == found
    assert v.mean_per_trade(by_day, seed=6, draws=400, level_ppm=900_000) != found
    said = found.as_dict()
    assert said["lower_micro"] == math.floor(found.lower) and said["upper_micro"] == math.ceil(found.upper)


@given(st.dictionaries(st.integers(0, 30), st.lists(st.integers(-10**7, 10**7), max_size=6), min_size=1, max_size=8),
       st.integers(0, 2**32), st.integers(-10**6, 10**6))  # fmt: skip
def test_the_interval_is_exact_ordered_and_moves_with_the_results(by_day, seed, shift):
    if not any(by_day.values()):
        with pytest.raises(v.ValidationError):
            v.mean_per_trade(by_day, seed=seed, draws=50, level_ppm=900_000)
        return
    how = {"seed": seed, "draws": 50, "level_ppm": 900_000}
    found = v.mean_per_trade(by_day, **how)
    every = [x for xs in by_day.values() for x in xs]
    assert type(found.lower) is Fraction and min(every) <= found.lower <= found.upper <= max(every)
    better = {d: [x + shift for x in xs] for d, xs in by_day.items()}
    moved = v.mean_per_trade(better, **how)
    assert (moved.point, moved.lower, moved.upper) == (found.point + shift, found.lower + shift, found.upper + shift)
    ahead = v.advantage(better, by_day, **how)
    assert ahead.point == ahead.lower == ahead.upper == shift, "the same trades, each better by the same amount"


def test_the_advantage_is_over_the_same_days_and_not_trading_counts_as_nothing():
    challenger, champion = {1: [30, 30], 2: [10]}, {1: [], 2: [], 3: []}
    found = v.advantage(challenger, champion, seed=2, draws=300, level_ppm=900_000)
    assert found.point == Fraction(70, 3) and found.days == 2 and 10 <= found.lower <= found.upper <= 30
    assert v.advantage(champion, challenger, seed=2, draws=300, level_ppm=900_000).point == -found.point
    for wrong in ({"draws": 0}, {"level_ppm": 0}, {"level_ppm": 1_000_000}):
        with pytest.raises(v.ValidationError):
            v.mean_per_trade(challenger, **{"seed": 1, "draws": 10, "level_ppm": 900_000, **wrong})
    with pytest.raises(v.ValidationError):
        v.advantage({}, {}, seed=1, draws=10, level_ppm=900_000)


def test_the_drawdown_is_the_largest_fall_from_an_earlier_high_in_day_order():
    assert v.drawdown({}) == 0 and v.drawdown({1: [5, 5]}) == 0
    assert v.drawdown({2: [-4, 10], 1: [5, -3]}) == 7, "5, 2, then -2, 8: from 5 down to -2"
    assert v.drawdown({1: [-6]}) == 6, "a loss from the start counts from zero"


# ---- the Sharpe ratio, deflated --------------------------------------------------------------------------------------


def test_the_sharpe_ratio_is_per_period_and_says_which_period():
    found = v.sharpe([1, 2, 3])
    assert (found.ratio, found.periods, found.interval, found.skew, found.kurtosis) == (2.0, 3, "trading day", 0.0, 1.5)
    assert {f.name for f in dataclasses.fields(v.Sharpe)} == {"ratio", "periods", "interval", "skew", "kurtosis"}
    assert v.sharpe([10**7, 2 * 10**7, 3 * 10**7], "week") == dataclasses.replace(found, interval="week")
    for too_little in ([], [1, 2], [5, 5, 5, 5]):
        with pytest.raises(v.ValidationError):
            v.sharpe(too_little)


def test_with_one_trial_the_deflated_ratio_is_the_probability_of_beating_zero():
    found = v.deflated_sharpe([1, 2, 3], trials=1)
    assert found.benchmark == 0.0 and found.trials == 1
    assert found.probability == NormalDist().cdf(2.0 * math.sqrt(2) / math.sqrt(1 + 0.125 * 4.0))
    assert v.deflated_sharpe([-1, -2, -3], trials=1).probability == pytest.approx(1 - found.probability)


def test_every_trial_counted_raises_the_bar_and_lowers_the_probability():
    results = [3, -1, 4, 1, -5, 9, 2, -6, 5, 3, 5, -8, 9, 7, 9, -3, 2, 3, 8, 4]
    variance = v.null_variance(len(results))
    assert variance == 1 / 19
    found = [v.deflated_sharpe(results, trials=n, trial_variance=variance) for n in (1, 2, 10, 100, 1_000)]
    assert [f.benchmark for f in found] == sorted(f.benchmark for f in found) and found[1].benchmark > 0
    assert [f.probability for f in found] == sorted((f.probability for f in found), reverse=True)
    assert found[0].probability > 0.9 > 0.5 > found[-1].probability, "good alone, and one of a thousand"
    assert v.deflated_sharpe(results, trials=100, trial_variance=variance) == found[3], "bit for bit the same again"
    near, far = NormalDist().inv_cdf(0.9), NormalDist().inv_cdf(1 - 1 / (10 * math.e))
    assert found[2].benchmark == math.sqrt(variance) * ((1 - v.EULER_GAMMA) * near + v.EULER_GAMMA * far)


def test_the_number_of_trials_is_the_number_counted_and_cannot_be_left_out(tmp_path):
    results = [3, -1, 4, 1, -5, 9, 2, -6, 5, 3]
    ledger = TrialLedger(tmp_path / "trials.jsonl")
    for config in ("a", "b", "c"):
        ledger.record(config, "walk_forward", "dataset")
    counted = v.deflated_sharpe(results, trials=counted_trials(ledger), trial_variance=v.null_variance(10))
    assert counted.trials == 3 and counted.probability < v.deflated_sharpe(results, trials=1).probability
    with pytest.raises(TypeError):
        v.deflated_sharpe(results)
    for wrong in ({"trials": 0}, {"trials": 5}, {"trials": 5, "trial_variance": -1.0}):
        with pytest.raises(v.ValidationError):
            v.deflated_sharpe(results, **wrong)
    with pytest.raises(v.ValidationError):
        v.null_variance(1)
