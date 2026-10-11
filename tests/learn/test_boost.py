"""Gradient boosting: the exported whole-number trees are the library's trees, the same every time, and honest.

These tests fit with the library, so they need the optional ML set (`make ml`) and are skipped without it. What
a model of this kind needs in order to be SCORED is tested without it, in tests/models/.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from fractions import Fraction

import pytest

lightgbm = pytest.importorskip(
    "lightgbm", reason="the optional ML set is not installed (make ml): fitting a gbm model needs its library"
)

import numpy  # noqa: E402  (after the skip: without the set this module is not collected)

from hftlab.journal import read  # noqa: E402
from hftlab.learn import boost, train  # noqa: E402
from hftlab.learn.config import Boosting, load  # noqa: E402
from hftlab.learn.validate import advantage, mean_per_trade, three_way  # noqa: E402
from hftlab.lineup import read as read_lineup  # noqa: E402
from hftlab.models import BUY, SELL, ModelRegistry, Trees, TreeScorer, parse, scorer_for  # noqa: E402
from tests.learn.kit import IMBALANCE, SATURDAY, WIN, dataset, learn_settings, record  # noqa: E402
from tests.learn.shapes import BAND, NOISE, shaped_weeks  # noqa: E402
from tests.learn.test_train import go, the_report  # noqa: E402
from tests.support import ROOT  # noqa: E402

SMALL = Boosting(trees=40, max_depth=4, leaves=12, learning_rate_ppm=100_000, min_leaf_rows=20, bins=63,
                 l2_ppm=1_000_000, leaf_scale=1000, seed=1)  # fmt: skip
TEN_DAYS = (0, 1, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15)  # two trading days to train on first, then two whole weeks


def table(seed: int, count: int) -> tuple[list[tuple[int, ...]], dict[str, list[int]]]:
    """Whole-number rows with steps, a band and an interaction in them, and noise: something for trees to split on."""
    rng = random.Random(seed)
    rows, buy, sell = [], [], []
    for _ in range(count):
        row = (rng.randrange(-6, 7), rng.randrange(0, 2_000), rng.randrange(-3, 4) * 250_000, rng.randrange(-1, 2),
               rng.randrange(-(10**12), 10**12))  # fmt: skip
        signal = (900_000 if abs(row[0]) > 3 else -400_000) + (350_000 if row[2] > 0 and row[3] == 0 else 0)
        rows.append(row)
        buy.append(signal + rng.randrange(-200_000, 200_000) + row[1] * 37)
        sell.append(-signal + rng.randrange(-200_000, 200_000))
    return rows, {BUY: buy, SELL: sell}


# ---- the boundary: the library's floats on one side, whole numbers on the other ------------------------------------


def test_the_exported_trees_send_thousands_of_rows_where_the_library_does_and_agree_to_the_stated_rounding():
    """Rows the fit never saw, values on and beside every threshold among them. One rounding per tree, no more."""
    rows, targets = table(1, 4_000)
    grown = boost.grow(rows, targets, SMALL)
    kept = boost.keep(grown, rows, SMALL)
    fresh, _ = table(2, 5_000)
    edges = [tuple(tree.threshold[at] + step if place == tree.feature[at] else value for place, value in enumerate(row))
             for row, tree in zip(fresh, kept.trees[BUY] * 200, strict=False) for at in range(len(tree.feature))
             for step in (-1, 0, 1)][:3_000]  # fmt: skip
    unseen = [*fresh, *edges]
    assert len(unseen) == 8_000 and len(kept.trees[BUY]) == len(kept.trees[SELL]) == SMALL.trees
    matrix = numpy.array(unseen, dtype=numpy.float64)
    bound = Fraction(SMALL.trees, 2 * SMALL.leaf_scale)  # half a unit of the scale per tree: 0.02 millionths here
    worst = Fraction(0)
    for name in (BUY, SELL):
        leaves = grown[name].predict(matrix, pred_leaf=True).tolist()
        theirs = grown[name].predict(matrix).tolist()
        for row, their_leaves, their_value in zip(unseen, leaves, theirs, strict=True):
            assert [tree.leaf(row) for tree in kept.trees[name]] == their_leaves, "the same leaf of every tree: exact"
            apart = abs(kept.predict(name, row) - Fraction(their_value))
            worst = max(worst, apart)
            slack = abs(Fraction(their_value)) / 10**12  # the library adds its leaves in floats; ours are exact
            assert apart <= bound + slack, f"{name} {row}: {float(apart)} apart"
    assert Fraction(1, 10**6) < worst <= bound * (1 + Fraction(1, 10**9)), "they do differ: by the rounding, no more"
    assert any(len(tree.feature) > 1 for tree in kept.trees[BUY]), "and these are trees, not constants"


def test_what_is_kept_is_small_whole_and_within_the_settings():
    rows, targets = table(1, 4_000)
    kept = boost.fit(rows, targets, SMALL)
    assert kept.scale == 1000 and kept.rows == 4_000 and kept.width == 5 and kept.settings == SMALL.as_dict()
    assert kept.fitted_with == f"lightgbm {lightgbm.__version__}"
    for tree in (*kept.trees[BUY], *kept.trees[SELL]):
        assert len(tree.leaves) <= SMALL.leaves and all(type(value) is int for value in (*tree.threshold, *tree.leaves))
    assert Trees.from_params(json.loads(json.dumps(kept.params()))) == kept
    told = boost.parameters(SMALL)
    assert (told["num_threads"], told["deterministic"], told["seed"], told["force_row_wise"]) == (1, True, 1, True)
    assert (told["bagging_fraction"], told["feature_fraction"], told["extra_trees"]) == (1.0, 1.0, False), "none drawn"
    assert (told["num_leaves"], told["max_depth"], told["max_bin"]) == (12, 4, 63)


def test_rows_a_float_cannot_hold_exactly_or_too_few_rows_are_refused():
    rows, targets = table(1, 50)
    with pytest.raises(boost.BoostError, match="holds exactly"):
        boost.fit([(2**53, *rows[0][1:]), *rows[1:]], targets, SMALL)
    with pytest.raises(boost.BoostError, match="holds exactly"):
        boost.fit(rows, {BUY: [2**60] * 50, SELL: targets[SELL]}, SMALL)
    with pytest.raises(boost.BoostError):
        boost.fit([(0.5, 1, 1, 1, 1), *rows[1:]], targets, SMALL)
    with pytest.raises(boost.TooFewRows):
        boost.fit(rows[:1], {BUY: targets[BUY][:1], SELL: targets[SELL][:1]}, SMALL)


def test_a_split_that_is_not_a_plain_threshold_is_never_written_down():
    leaf = {"leaf_index": 0, "leaf_value": 1.5}
    split = {"split_feature": 0, "threshold": 2.5000000000000004, "decision_type": "<=", "missing_type": "None",
             "left_child": leaf, "right_child": {"leaf_index": 1, "leaf_value": -0.25}}  # fmt: skip
    tree = boost.exported(split, 1000)
    assert (tree.threshold, tree.leaves) == ((2,), (1500, -250)), "floor of the threshold; the leaf to the nearest"
    assert boost.exported({**split, "threshold": -1.0000000180025095e-35}, 1000).threshold == (-1,)
    assert boost.exported({"leaf_value": 0.0004}, 1000).leaves == (0,), "a tree that never split is one leaf"
    for wrong in ({"decision_type": "=="}, {"missing_type": "NaN"}, {"right_child": leaf}, {"threshold": "2.5"},
                  {"left_child": {"leaf_index": 0, "leaf_value": float("nan")}}):  # fmt: skip
        with pytest.raises(boost.BoostError):
            boost.exported({**split, **wrong}, 1000)


# ---- the same inputs, the same file --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def band():
    return dataset(shaped_weeks(1, BAND), checks=4)


def test_two_fits_of_the_same_rows_settings_and_seed_are_the_same_file_in_this_process_and_in_another(band, tmp_path):
    rows = range(len(band.rows))
    first, second = train.gbm_model(band, rows, SMALL), train.gbm_model(band, rows, SMALL)
    assert first.data() == second.data() and first.content_hash == second.content_hash
    assert parse(first.data()) == first and first.kind == "gbm" and first.dataset_hash == band.content_hash
    script = (
        "from tests.learn.test_boost import SMALL, BAND, dataset, shaped_weeks, train\n"
        "made = dataset(shaped_weeks(1, BAND), checks=4)\n"
        "print(train.gbm_model(made, range(len(made.rows)), SMALL).content_hash)\n"
    )
    environment = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
    other = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=True)
    assert other.stdout.strip() == first.content_hash, "a process that started from nothing writes the same model"
    assert ModelRegistry(tmp_path).put(first) == ModelRegistry(tmp_path).put(second) == first.content_hash


# ---- honest evaluation: something where something is, nothing where nothing is -------------------------------------


def walked(made, settings):
    """The walk-forward of a training run: each test week scored by a model fitted to the weeks before it."""
    plan, _ = train._walk(made, settings)
    fold_of = {made.rows[position].n: number for number, fold in enumerate(plan.folds) for position in fold.test}
    scorers = {}

    def decide(row):
        number = fold_of[row.n]
        if number not in scorers:
            scorers[number] = scorer_for(train.fit_model(made, plan.folds[number].train, settings))
        found = scorers[number].score(row)
        return found.side if found is not None else None

    return three_way([made.rows[p] for fold in plan.folds for p in fold.test], made.pair, decide, seed=settings.seed)


def test_a_planted_band_that_a_straight_line_cannot_find_is_found_by_the_trees(band, tmp_path):
    """Three to one: the mid follows the heavy side. Seven to one: it goes against it. Uncorrelated by construction."""
    assert {row.features[IMBALANCE] for row in band.rows} == {0, 500_000, -500_000, 750_000, -750_000}
    ridge = walked(band, load(learn_settings(tmp_path / "ridge")))
    trees = walked(band, load(learn_settings(tmp_path / "gbm", model="gbm")))
    assert ridge.oracle.total_micro == trees.oracle.total_micro > 0 and ridge.rows == trees.rows, "the same rows"
    assert ridge.model.trades == 0 and ridge.model.total_micro == 0, "no slope to find: the straight line abstains"
    assert trees.model.total_micro == trees.oracle.total_micro, "every row that pays, and the right side of it"
    assert trees.model.trades == trees.oracle.trades and trees.model.losses == 0
    assert trees.model.total_micro == trees.model.trades * WIN > 0 > trees.random.total_micro
    lower = mean_per_trade(trees.model.by_day, seed=1, draws=200, level_ppm=950_000).lower
    assert lower == WIN, "and the interval says so: every day's trades made the planted effect, exactly"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_where_nothing_is_planted_the_trees_trade_lose_their_costs_and_do_not_beat_the_coin(seed, tmp_path):
    """The same book, and a jump that goes where a coin of its own says. Whatever is found in it was made up.

    The trees do make something up: fitted to noise they expect a profit of some rows, and trade them. What is
    held here is that walk-forward shows it for what it is. The lower bound of the day-block interval of
    their mean result per trade is not above zero, and neither is the lower bound of that mean less the seeded
    coin's over the same days.

    A 95% interval is wrong one time in twenty by its own account, and one drawn from ten days somewhat more
    often. Over the seeds 1 to 40, looked at once when this test was written, the advantage's lower bound was
    above zero for three (8, 37 and 38), the trees' own lower bound for none, and their total was a loss for
    all forty. The three seeds here are the first three, chosen before that count was made.
    """
    made = dataset(shaped_weeks(seed, NOISE, 30, on=TEN_DAYS), checks=4)
    found = walked(made, load(learn_settings(tmp_path, model="gbm")))
    assert found.oracle.total_micro > 0, "hindsight would have made money: the jumps are there"
    assert found.model.trades > 500 and len(found.model.by_day) >= 9, "and the trees did trade, on most days"
    how = {"seed": 1, "draws": 400, "level_ppm": 950_000}
    own = mean_per_trade(found.model.by_day, **how)
    coin = {day: found.random.by_day[day] for day in found.model.by_day}
    over_the_coin = advantage(found.model.by_day, coin, **how)
    assert own.lower <= 0, "no edge of its own after costs"
    assert over_the_coin.lower <= 0, "and none over a coin beyond what the interval allows"
    assert found.model.total_micro < 0, "it paid for what it made up"


# ---- the weekend run, with the kind set to gbm ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    folder = tmp_path_factory.mktemp("gbm")
    record(folder / "rec", shaped_weeks(1, BAND))
    assert go(folder, model="gbm") == train.EXIT_OK
    return folder


def test_a_gbm_run_leaves_what_a_ridge_run_leaves_a_model_a_report_with_the_three_numbers_and_a_candidate(trained):
    settings = load(trained / "learn.yaml")
    (digest,) = ModelRegistry(settings.models).hashes()
    report = the_report(trained)
    model = parse((settings.models / f"{digest}.json").read_bytes())
    assert report["model"] == {"hash": digest, "kind": "gbm", "fitted_rows": report["model"]["fitted_rows"]}
    assert model.kind == "gbm" and type(scorer_for(model)) is TreeScorer
    assert report["config"]["kind"] == "gbm" and report["config"]["boosting"] == settings.boosting.as_dict()
    three = report["three_way"]
    assert three["oracle"]["hindsight"] is True and three["random"]["seed"] == 1 and three["no_trade"]["trades"] == 0
    assert three["oracle"]["total_micro"] == three["model"]["total_micro"] > 0 > three["random"]["total_micro"]
    assert report["interval"]["lower_micro"] == WIN and report["trials"] == 1 == report["deflated_sharpe"]["trials"]
    assert report["not_a_result"] == 1 and "expected_micro" in report["summary"]["columns"]
    lineup = read_lineup(settings.lineup)
    (report_path,) = settings.reports.glob("*.json")
    assert [(one.model, one.report) for one in lineup.candidates] == [(digest, report_path.stem)]
    assert lineup.champion is None and lineup.challengers == (), "a candidate serves nowhere until a decision"
    said = json.loads((settings.state_dir / train.LAST_RUN).read_text())
    assert said["model"] == "gbm" and said["memory"]["within"] is True and said["memory"]["peak_bytes"] > 0


def test_the_same_inputs_give_the_same_gbm_model_and_report_bytes_and_one_counted_trial(trained, tmp_path):
    assert go(tmp_path, store=trained / "rec", model="gbm") == train.EXIT_OK
    for folder in ("models", "reports"):
        (one,), (other,) = ((base / "learn" / folder).glob("*.json") for base in (trained, tmp_path))
        assert one.name == other.name and one.read_bytes() == other.read_bytes(), folder
    assert go(tmp_path, store=trained / "rec", model="gbm") == train.EXIT_OK, "and again, into the same folders"
    kinds = [entry["what"] for entry in read(tmp_path / "learn" / "trials.jsonl")[1]]
    assert kinds == ["walk_forward", "walk_forward"] and the_report(tmp_path)["trials"] == 1, "written twice, one trial"
    assert go(tmp_path, store=trained / "rec") == train.EXIT_OK, "the same store, the ridge kind"
    assert len(list((tmp_path / "learn" / "reports").glob("*.json"))) == 2
    counts = sorted(json.loads(path.read_bytes())["trials"] for path in (tmp_path / "learn" / "reports").glob("*.json"))
    assert counts == [1, 2], "another kind is another configuration, and the count says so"


def test_a_gbm_run_in_a_process_of_its_own_peaks_far_inside_the_hosts_cap(trained, tmp_path, capsys):
    """The library, its arrays and the fit are all in the number: it is the process's own peak, from the system."""
    config = learn_settings(tmp_path, model="gbm")
    (tmp_path / "registry.yaml").write_text("history: []\n")
    script = (
        "import sys\nfrom hftlab.core import SimClock\nfrom hftlab.learn import train\n"
        f"sys.exit(train.main({['--config', str(config), '--cost-model', str(ROOT / 'config/cost_model.yaml')]!r} + "
        f"{['--calendar', str(ROOT / 'config/calendar'), '--feasibility', str(ROOT / 'config/feasibility.yaml')]!r} + "
        f"{['--store', str(trained / 'rec'), '--trial-registry', str(tmp_path / 'registry.yaml')]!r}, "
        f"SimClock({SATURDAY})))\n"
    )
    environment = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=False)
    assert done.returncode == train.EXIT_OK, done.stderr
    said = json.loads((tmp_path / "learn" / train.LAST_RUN).read_text())
    with capsys.disabled():
        print(f"\n  gbm run: {said['events']} events, {said['rows']} rows; process peak {said['memory']['peak_bytes']} "
              f"bytes of a budget of {said['memory']['budget_bytes']}")  # fmt: skip
    assert said["model"] == "gbm" and said["memory"]["within"] is True
    assert 0 < said["memory"]["peak_bytes"] < 600_000_000, "under the slice's 1.2 GB with room: the library is small"
