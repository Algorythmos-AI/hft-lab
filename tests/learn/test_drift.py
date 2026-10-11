"""Drift: a planted shift is found to the count, a like stream finds nothing, and a null threshold breaches nothing."""
from __future__ import annotations

import ast

import pytest

from hftlab.learn.drift import (
    EXPECTED_KEY,
    SPREAD_KEY,
    DriftError,
    Watch,
    by_column,
    calibration,
    counts_in,
    edges_of,
    feature_shift,
    median,
    shift_ppm,
    spread_regime,
    summarise,
)
from tests.support import imports, sources

KEYS = ("a@v1", SPREAD_KEY)
TRAINED = [(value, 10 + value % 3) for value in range(100)]  # a: 0..99, each once; the spread: 10, 11 or 12


def summary(bins: int = 5, expected=(-40, -20, 0, 20, 40, 60, 80, 100)) -> dict[str, object]:
    columns = by_column(KEYS, TRAINED)
    columns[EXPECTED_KEY] = list(expected)
    return summarise(columns, bins, rows=len(TRAINED))


# ---- the bins ------------------------------------------------------------------------------------------------------


def test_edges_are_values_that_were_seen_and_every_value_falls_in_exactly_one_bin():
    edges = edges_of([value for value, _ in TRAINED], 5)
    assert edges == [19, 39, 59, 79], "a bin holds what is above the edge before it and not above its own"
    assert counts_in(edges, [value for value, _ in TRAINED]) == [20, 20, 20, 20, 20]
    assert counts_in(edges, [-5, 19, 20, 79, 80, 10**9]) == [2, 1, 0, 1, 2]
    assert edges_of([7] * 50, 5) == [7] and counts_in([7], [7] * 50) == [50, 0], "a constant is one bin, and stays one"
    assert median([5, 1, 9, 3]) == 3 and median([4]) == 4, "the lower median: a value that was seen"
    for wrong in (lambda: edges_of([], 5), lambda: edges_of([1, 2], 1), lambda: median([])):
        with pytest.raises(DriftError):
            wrong()


def test_the_shift_is_the_share_of_rows_in_other_bins_exactly_and_the_same_in_any_order():
    assert shift_ppm([20, 20, 20, 20, 20], [20, 20, 20, 20, 20]) == 0
    assert shift_ppm([20, 20, 20, 20, 20], [2, 2, 2, 2, 2]) == 0, "it compares shares, not sizes"
    assert shift_ppm([20, 20, 20, 20, 20], [0, 20, 20, 20, 40]) == 200_000, "a fifth of the rows moved"
    assert shift_ppm([50, 50, 0], [0, 0, 7]) == 1_000_000, "nothing where it was"
    assert shift_ppm([1, 2], [1, 1]) == 166_667, "a sixth, rounded up"
    for wrong in (([1, 2], [1, 2, 3]), ([0, 0], [1, 1]), ([1, 1], [0, 0]), ([1, -1, 1], [1, 1, 1])):
        with pytest.raises(DriftError):
            shift_ppm(*wrong)


# ---- a planted shift -----------------------------------------------------------------------------------------------


def test_a_planted_shift_of_one_feature_is_found_in_that_feature_and_to_the_row(tmp_path):
    kept = summary()
    like = [(value, 10 + value % 3) for value in range(0, 100, 2)]  # every second training row: the same shares
    limits = {"threshold_ppm": 100_000, "min_rows": 50}
    assert [(f.subject, f.value, f.breached) for f in feature_shift(kept, KEYS, like, **limits)] == [
        ("a@v1", 0, False), (SPREAD_KEY, 10_000, False),
    ]  # fmt: skip
    moved = [(value + 80 if value < 15 else value, spread) for value, spread in TRAINED]  # 15 rows from bin 0 to 4
    (first, second) = feature_shift(kept, KEYS, moved, **limits)
    assert (first.subject, first.value, first.breached) == ("a@v1", 150_000, True), "fifteen rows of a hundred"
    assert (second.value, second.breached) == (0, False), "and the feature that did not move is not blamed"
    assert first.plain() == {"monitor": "feature_shift", "subject": "a@v1", "value": 150_000, "threshold": 100_000,
                             "breached": 1}  # fmt: skip
    at_the_bound = feature_shift(kept, KEYS, moved, threshold_ppm=150_000, min_rows=50)
    assert not at_the_bound[0].breached, "a threshold is breached above it, not at it"


def test_too_few_rows_or_a_null_threshold_is_reported_and_breaches_nothing():
    kept = summary()
    moved = [(value + 1_000, spread * 5) for value, spread in TRAINED]
    for limits in (
        {"threshold_ppm": 1, "min_rows": 101},  # fewer live rows than the monitor asks for
        {"threshold_ppm": None, "min_rows": 1},  # the threshold is not in force
        {"threshold_ppm": 1, "min_rows": None},
    ):
        found = feature_shift(kept, KEYS, moved, **limits)
        assert [f.value for f in found] == [800_000, 1_000_000] and not any(f.breached for f in found), limits
    nothing = feature_shift(kept, KEYS, [], threshold_ppm=1, min_rows=1)
    assert [f.value for f in nothing] == [None, None] and not any(f.breached for f in nothing), "no rows: no verdict"
    assert not spread_regime(kept, [50] * 100, threshold_x100=None, min_rows=1).breached
    assert not spread_regime(kept, [50] * 9, threshold_x100=150, min_rows=10).breached
    assert not calibration(kept, [(100, -100)] * 50, threshold_micro=None, min_rows=1)[-1].breached


def test_calibration_is_the_mean_expected_less_the_mean_made_by_bucket_of_what_was_expected():
    kept = summary(bins=4)  # expectations -40..100: edges at -20, 20 and 60
    honest = [(-30, -31), (0, 2), (40, 39), (90, 91)] * 5
    found = calibration(kept, honest, threshold_micro=5, min_rows=5)
    assert [f.subject for f in found] == ["bucket-0", "bucket-1", "bucket-2", "bucket-3"]
    assert [f.value for f in found] == [1, -2, 1, -1] and not any(f.breached for f in found)
    decayed = [*honest, *[(95, 20)] * 5]  # the trades it was surest of stopped coming true
    found = calibration(kept, decayed, threshold_micro=5, min_rows=5)
    assert [f.value for f in found] == [1, -2, 1, 37] and [f.breached for f in found] == [False, False, False, True]
    assert calibration(kept, [(90, 20), (90, 21)], threshold_micro=5, min_rows=5)[3].value == 70, "69.5, rounded up"
    assert not calibration(kept, [(90, 20)] * 4, threshold_micro=5, min_rows=5)[3].breached, "four trades judge nothing"
    better = calibration(kept, [(90, 500)] * 9, threshold_micro=5, min_rows=5)[3]
    assert better.value == -410 and not better.breached, "a model that made more than it expected has not decayed"
    assert [f.value for f in calibration(kept, [], threshold_micro=5, min_rows=1)] == [None] * 4


def test_the_spread_regime_is_the_live_median_over_the_training_median_in_hundredths():
    kept = summary()  # the training median is 11
    assert spread_regime(kept, [11] * 20, threshold_x100=150, min_rows=10).value == 100
    wider = spread_regime(kept, [10, 22, 22, 22, 30] * 4, threshold_x100=150, min_rows=10)
    assert (wider.value, wider.breached, wider.subject) == (200, True, SPREAD_KEY)
    assert spread_regime(kept, [12] * 20, threshold_x100=150, min_rows=10).value == 110, "109.09..., rounded up"
    assert not spread_regime(kept, [5] * 20, threshold_x100=150, min_rows=10).breached, "narrower costs nobody"
    assert spread_regime(kept, [], threshold_x100=150, min_rows=10).value is None


def test_a_summary_that_is_not_one_is_refused_and_a_watch_lists_what_breached():
    kept = summary()
    for damage in (
        lambda s: s["columns"].pop("a@v1"),
        lambda s: s["columns"]["a@v1"]["counts"].pop(),
        lambda s: s["columns"]["a@v1"].update(median="11"),
        lambda s: s["columns"]["a@v1"]["edges"].__setitem__(0, 1.5),
    ):
        broken = summary()
        damage(broken)
        with pytest.raises(DriftError):
            feature_shift(broken, KEYS, TRAINED, threshold_ppm=1, min_rows=1)
    with pytest.raises(DriftError, match=EXPECTED_KEY):
        calibration({"columns": {}}, [], threshold_micro=1, min_rows=1)
    moved = [(value + 1_000, spread) for value, spread in TRAINED]
    watch = Watch("ml", "f" * 64, tuple(feature_shift(kept, KEYS, moved, threshold_ppm=1, min_rows=1)))
    assert [f.subject for f in watch.breached] == ["a@v1"] and watch.plain()["breached"] == ["feature_shift:a@v1"]


def test_the_monitors_read_no_file_no_clock_and_no_process():
    tree = dict(sources())["src/hftlab/learn/drift.py"]
    reached = {name for _, name in imports(tree, "src/hftlab/learn/drift.py")}
    assert {name.split(".")[0] for name in reached} <= {"__future__", "bisect", "collections", "dataclasses",
                                                        "fractions", "typing", "hftlab"}  # fmt: skip
    assert {name for name in reached if name.startswith("hftlab")} == {"hftlab.lineup.watch.main"}
    (last,) = [node for node in tree.body if isinstance(node, ast.If)]
    assert ast.unparse(last.test) == "__name__ == '__main__'" and tree.body[-1] is last, "only the command's start"
