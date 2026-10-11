"""The sequence network: right gradients, whole numbers within a derived bound of the fit, and an honest evaluation.

These tests fit a network, which needs the array library of the base requirements and nothing else. They are
skipped where it cannot be imported. What a model of this kind needs in order to be SCORED is tested without
it, in tests/models/.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction

import pytest

numpy = pytest.importorskip(
    "numpy", reason="the array library is not installed (make bootstrap): fitting a sequence network needs it"
)

from hftlab.core import Side  # noqa: E402  (after the skip: without the library this module is not collected)
from hftlab.features import FeatureRow  # noqa: E402
from hftlab.journal import read  # noqa: E402
from hftlab.learn import sequence, train  # noqa: E402
from hftlab.learn.config import Network, load  # noqa: E402
from hftlab.learn.dataset import build  # noqa: E402
from hftlab.learn.validate import advantage, mean_per_trade, three_way  # noqa: E402
from hftlab.lineup import read as read_lineup  # noqa: E402
from hftlab.models import BUY, SELL, ModelRegistry, Net, NetScorer, parse, scorer_for  # noqa: E402
from hftlab.models.net import BITS, WEIGHT_BITS  # noqa: E402
from tests.learn.kit import CONFIG, SATURDAY, WIN, dataset, features, labels, learn_settings, record  # noqa: E402
from tests.learn.shapes import NOISE, ORDER, ordered_weeks  # noqa: E402
from tests.learn.test_train import go, the_report  # noqa: E402
from tests.models.test_net import a_model  # noqa: E402
from tests.support import ROOT  # noqa: E402

TINY = Network(window=3, kernel=2, filters=2, hidden=3, epochs=2, batch_rows=8, learning_rate_ppm=10_000, l2_ppm=5_000,
               clip=3, cap=2, seed=1)  # fmt: skip
SMALL = Network(window=6, kernel=3, filters=4, hidden=8, epochs=12, batch_rows=64, learning_rate_ppm=10_000,
                l2_ppm=1_000, clip=3, cap=8, seed=1)  # fmt: skip
# What the tests of the planted order are fitted with: small, fixed, and searched by nothing.
READS = {"window": 8, "kernel": 3, "filters": 8, "hidden": 16, "epochs": 100, "batch_rows": 128,
         "learning_rate_ppm": 10_000, "l2_ppm": 1_000, "clip": 3, "cap": 8, "seed": 2}  # fmt: skip
QUICK = {**READS, "epochs": 20}  # for the tests of what a run leaves behind, which ask nothing of how well it fits
WINDOWS = replace(CONFIG, window_rows=READS["window"])
TEN_DAYS = (0, 1, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15)  # two trading days to train on first, then two whole weeks
ONE = 1 << (BITS + WEIGHT_BITS)  # an output of 1, as the whole-number network writes it


def table(seed: int, count: int, settings: Network = SMALL) -> tuple[list[tuple[int, ...]], dict[str, list[int]]]:
    """Windows of five whole numbers a row, on very different scales, with an order of rows in the targets and noise."""
    rng = random.Random(seed)
    rows, buy, sell = [], [], []
    for _ in range(count):
        window = [(rng.randrange(-6, 7), rng.randrange(0, 2_000), rng.randrange(-3, 4) * 250_000, rng.randrange(-1, 2),
                   rng.randrange(-(10**12), 10**12)) for _ in range(settings.window)]  # fmt: skip
        signal = 900_000 if window[-2][0] > window[-1][0] else -400_000
        signal += 350_000 if window[-1][2] > 0 and window[-3][3] == 0 else 0
        rows.append(tuple(value for row in window for value in row))
        buy.append(signal + rng.randrange(-200_000, 200_000) + window[-1][1] * 37)
        sell.append(-signal + rng.randrange(-200_000, 200_000))
    return rows, {BUY: buy, SELL: sell}


def floats(grown, settings: Network, net: Net, rows) -> list[list[float]]:
    """The fitted float network's two outputs for each window, from the inputs the scorer reads."""
    inputs = numpy.array([net.inputs(row) for row in rows], dtype=numpy.float64) / 2**BITS
    return sequence.forward(grown[0], inputs, settings, net.width)[-1].tolist()


# ---- the gradients, written by hand, against differences ----------------------------------------------------------


def test_the_hand_written_gradient_of_every_weight_and_bias_agrees_with_a_central_difference():
    rng = random.Random(3)
    per_row, count = 4, 12
    inputs = numpy.array([[rng.uniform(-2, 2) for _ in range(TINY.window * per_row)] for _ in range(count)])
    wanted = numpy.array([[rng.uniform(-1, 1), rng.uniform(-1, 1)] for _ in range(count)])
    weights = sequence.start(TINY, per_row)
    for name in sorted(weights):  # away from the start, so that the biases and every clip have something to say
        weights[name] = weights[name] + numpy.array(
            [rng.uniform(-0.3, 0.3) for _ in range(weights[name].size)]
        ).reshape(weights[name].shape)
    found = sequence.gradients(weights, inputs, wanted, TINY, per_row)
    step, checked, worst = 1e-6, 0, 0.0
    for name in sorted(weights):
        flat = weights[name].reshape(-1)
        for place in range(flat.size):
            kept = flat[place]
            flat[place] = kept + step
            above = float(sequence.loss(weights, inputs, wanted, TINY, per_row))
            flat[place] = kept - step
            below = float(sequence.loss(weights, inputs, wanted, TINY, per_row))
            flat[place] = kept
            worst = max(worst, abs((above - below) / (2 * step) - float(found[name].reshape(-1)[place])))
            checked += 1
    assert checked == 2 * (2 * 4 + 1) + 3 * (2 * 2 + 1) + 2 * (3 + 1) == 41, "every weight and every bias"
    assert worst < 1e-7, f"the chain rule written out is the slope of the loss: off by {worst}"
    assert max(float(abs(found[name]).max()) for name in found) > 1e-3, "and the slopes are not all nothing"


def test_the_starting_weights_are_a_hash_of_the_seed_and_a_pairwise_sum_has_one_order():
    first, again, other = sequence.start(SMALL, 5), sequence.start(SMALL, 5), sequence.start(replace(SMALL, seed=2), 5)
    assert all((first[name] == again[name]).all() for name in first), "the same seed, the same weights"
    assert not (first["conv"] == other["conv"]).all() and abs(first["conv"]).max() <= (6 / 15) ** 0.5
    assert sorted(first) == ["conv", "conv_bias", "hidden", "hidden_bias", "out", "out_bias"]
    values = numpy.array([[1e16, 1.0, -1e16, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0, 5.0]])
    assert sequence._total(values.T).tolist() == [((1e16 + 1.0) + (-1e16 + 1.0)) + 1.0, 15.0], "neighbours, then theirs"


# ---- the boundary: floats on one side, whole numbers on the other ---------------------------------------------------


@pytest.fixture(scope="module")
def fitted():
    rows, targets = table(1, 1_500)
    grown = sequence.grow(rows, targets, SMALL)
    return grown, sequence.keep(grown, rows, SMALL)


def test_the_whole_number_network_is_within_its_own_derived_bound_of_the_floats_on_thousands_of_unseen_windows(fitted):
    """Rows the fit never saw. The bound is `Net.slack`: worked out from the whole numbers, for any row at all."""
    grown, net = fitted
    unseen, _ = table(2, 5_000)
    slack, worst = net.slack(), Fraction(0)
    theirs = floats(grown, SMALL, net, unseen)
    for row, their in zip(unseen, theirs, strict=True):
        ours = net.totals(row)
        for name, value in zip((BUY, SELL), their, strict=True):
            apart = abs(Fraction(ours[name]) - Fraction(value) * ONE)
            worst = max(worst, apart)
            assert apart <= slack[name] + abs(Fraction(value)) * ONE / 10**12, f"{name}: {float(apart)} apart"
    assert 1 < worst <= max(slack.values()), "they do differ: by the rounding of the weights, and by no more"
    in_micro = Fraction(max(slack.values()) * net.target_scale, ONE)
    assert in_micro < 5_000 < net.target_scale // 100, "under half a cent on 20,000, and far under a typical target"
    assert net.parameters == 4 * 16 + 8 * 17 + 2 * 9 == 218 and net.reach() < 2**58


def test_where_the_scorer_decides_the_float_network_decides_the_same_and_where_it_could_not_tell_it_abstains(fitted):
    """Thousands of windows, and windows moved onto the margin on purpose. A row within the bound is counted."""
    grown, net = fitted
    model = a_model(net)
    scorer = scorer_for(model)
    assert type(scorer) is NetScorer
    unseen, _ = table(3, 4_000)
    slack = net.slack()
    both = slack[BUY] + slack[SELL]

    def apart(row) -> int:
        found = net.totals(row)
        return found[BUY] - found[SELL]

    on_the_margin = []  # the widest feature of the row in hand, moved to where the two sides cross
    for row in unseen[:1_500]:
        low, high = -(10**12), 10**12
        if (apart((*row[:-1], low)) > 0) == (apart((*row[:-1], high)) > 0):
            continue
        rising = apart((*row[:-1], high)) > 0
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (low, middle) if (apart((*row[:-1], middle)) > 0) == rising else (middle, high)
        on_the_margin += [(*row[:-1], value) for value in (low - 1, low, high, high + 1)]
    assert len(on_the_margin) > 40
    decided = same = within = quiet = 0
    tested = [*unseen, *on_the_margin]
    for row, (buy, sell) in zip(tested, floats(grown, SMALL, net, tested), strict=True):
        found = scorer.score(FeatureRow("EUR.USD", 1, 2, row))
        theirs = None if buy == sell or max(buy, sell) <= 0 else Side.BUY if buy > sell else Side.SELL
        if found is not None:
            decided += 1
            same += found.side is theirs
            expected = Fraction(max(buy, sell)) * net.target_scale
            assert abs(found.expected_micro - expected) <= Fraction(max(slack.values()) * net.target_scale, ONE) + 1
        elif theirs is None:
            quiet += 1
        else:  # the floats would trade and the whole numbers abstain: only ever inside the bound
            within += 1
            margin, best = abs(Fraction(buy) - Fraction(sell)) * ONE, Fraction(max(buy, sell)) * ONE
            assert margin <= 2 * both + 1 or best <= 2 * max(slack.values()) + 1, "abstained with room to decide"
    assert decided == same > 3_000, "the same side on every row the scorer decided"
    assert decided + quiet + within == len(tested)
    assert all(scorer.score(FeatureRow("EUR.USD", 1, 2, row)) is None for row in on_the_margin), "too close to call"
    assert 20 < within <= len(on_the_margin) + 10, f"{within} rows within the bound of the margin: counted"
    assert within + quiet >= len(on_the_margin)


def test_what_is_kept_is_whole_small_and_says_what_it_was_fitted_with(fitted):
    _, net = fitted
    assert (net.window, net.width, net.kernel, net.clip, net.cap, net.rows) == (6, 5, 3, 3, 8, 1_500)
    assert net.settings == SMALL.as_dict() and net.fitted_with == f"numpy {numpy.__version__}"
    assert Net.from_params(json.loads(json.dumps(net.params()))) == net
    assert all(type(value) is int for row in (*net.conv, *net.hidden, *net.out.values()) for value in row)
    assert max(abs(value) for row in (*net.conv, *net.hidden) for value in row) <= 4 << WEIGHT_BITS, "clipped at 4"
    assert net.spread[1] in range(570, 585) and net.spread[4] > 10**11, "each feature on its own scale"
    assert net.target_scale > 100_000


def test_rows_that_cannot_be_fitted_are_refused():
    rows, targets = table(1, 40)
    with pytest.raises(sequence.SequenceError, match="holds exactly"):
        sequence.fit([(2**53, *rows[0][1:]), *rows[1:]], targets, SMALL)
    with pytest.raises(sequence.SequenceError, match="holds exactly"):
        sequence.fit(rows, {BUY: [2**60] * 40, SELL: targets[SELL]}, SMALL)
    with pytest.raises(sequence.SequenceError):
        sequence.fit([(0.5, *rows[0][1:]), *rows[1:]], targets, SMALL)
    with pytest.raises(sequence.SequenceError, match="windows of 6 whole rows"):
        sequence.fit([row[:-1] for row in rows], targets, SMALL)
    with pytest.raises(sequence.TooFewRows):
        sequence.fit(rows[:1], {BUY: targets[BUY][:1], SELL: targets[SELL][:1]}, SMALL)
    wide = [tuple(value * 2**12 if place % 5 == 4 else value for place, value in enumerate(row)) for row in rows]
    with pytest.raises(sequence.SequenceError, match="varies by more"):
        sequence.fit(wide, targets, SMALL)


def test_the_estimate_of_a_fit_grows_with_its_rows_and_is_far_under_the_hosts_cap_for_a_week():
    shipped = load(ROOT / "config/learn.yaml").network
    week = sequence.needs(1_000_000, shipped.window * 11, shipped)
    assert sequence.needs(2_000_000, shipped.window * 11, shipped) - week == 8 * 1_000_000 * (88 + 2), "the arrays"
    assert 700_000_000 < week < 760_000_000, "a million windows of eight rows of eleven: under the 1 GB budget alone"
    assert sequence.needs(10_000, shipped.window * 11, shipped) < 30_000_000


# ---- the same inputs, the same file --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def order():
    return build(ordered_weeks(1, ORDER), features(WINDOWS), labels(), checks=4)


def test_two_fits_of_the_same_rows_settings_and_seed_are_the_same_file_in_this_process_and_in_another(order, tmp_path):
    quick = Network(**{**READS, "epochs": 3})
    rows = range(2_000)
    first, second = train.seq_model(order, rows, quick), train.seq_model(order, rows, quick)
    assert first.data() == second.data() and first.content_hash == second.content_hash
    assert parse(first.data()) == first and first.kind == "seq" and first.dataset_hash == order.content_hash
    assert train.seq_model(order, rows, replace(quick, seed=3)).content_hash != first.content_hash, "the seed is read"
    script = (
        "from tests.learn.test_sequence import *\n"
        "made = build(ordered_weeks(1, ORDER), features(WINDOWS), labels(), checks=4)\n"
        "print(train.seq_model(made, range(2_000), Network(**{**READS, 'epochs': 3})).content_hash)\n"
    )
    environment = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin",
                   "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4", "VECLIB_MAXIMUM_THREADS": "4"}  # fmt: skip
    other = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=True)
    assert other.stdout.strip() == first.content_hash, "a process that started from nothing, told to use four threads"
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


def test_a_planted_order_of_rows_that_no_model_of_one_row_can_find_is_found_by_the_network(order, tmp_path):
    """State 1 after state 0, the mid goes up; state 0 after state 1, it goes down. One row shows one state."""
    single = dataset(ordered_weeks(1, ORDER), checks=4)
    ridge = walked(single, load(learn_settings(tmp_path / "ridge")))
    assert ridge.oracle.total_micro > 0 and ridge.model.trades == 0, "nothing in any one row: the line abstains"
    paying = [row for row in single.rows if max(row.buy_micro, row.sell_micro) > 0]
    by_state: dict[int, list[bool]] = {}
    for row in paying:
        by_state.setdefault(row.features[4], []).append(row.buy_micro > 0)
    assert sorted(by_state) == [-500_000, 500_000, 950_000] and len({len(found) for found in by_state.values()}) == 1
    assert all(2 * sum(found) == len(found) for found in by_state.values()), "whatever a row shows: up as often as down"
    found = walked(order, load(learn_settings(tmp_path / "seq", model="seq", sequence=READS)))
    assert found.rows > 7_000 and 830 < found.oracle.trades <= 4 * 42 * 5, "the five rows before each jump pay"
    assert found.model.trades > 0 and found.model.total_micro > 0 > found.random.total_micro
    assert found.model.total_micro * 100 >= found.oracle.total_micro * 95, "within a twentieth of hindsight"
    assert found.model.losses * 50 <= found.model.trades, "and hardly a trade on the wrong side"
    lower = mean_per_trade(found.model.by_day, seed=1, draws=200, level_ppm=950_000).lower
    assert lower > WIN * 9 // 10, "the interval says so too: nearly every trade made the planted effect"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_where_nothing_is_planted_the_network_does_not_beat_the_coin_beyond_the_interval(seed, tmp_path):
    """The same book and the same states, and a jump that follows a draw of its own. Whatever is found was made up.

    A network fits noise. What is held here is that walk-forward shows it for what it is: the lower bound of the
    day-block interval of its mean result per trade is not above zero, and neither is the lower bound of that
    mean less the seeded coin's over the same days. The three seeds are the first three, chosen before any was run.

    Counted once over the seeds 1 to 24 with these settings, when this test was written: neither bound was
    above zero for any of them, and the total was a loss for all twenty-four. A smaller stream with fewer
    passes (18 blocks a day, 50 passes) was tried to make the test quicker and was not kept: there the second
    bound was above zero for seed 2, with a loss in total. A 95% interval of ten days is wrong about that often.
    """
    made = build(ordered_weeks(seed, NOISE, 27, on=TEN_DAYS), features(WINDOWS), labels(), checks=4)
    found = walked(made, load(learn_settings(tmp_path, model="seq", sequence={**READS, "epochs": 60})))
    assert found.oracle.total_micro > 0, "hindsight would have made money: the jumps are there"
    how = {"seed": 1, "draws": 400, "level_ppm": 950_000}
    if found.model.trades:
        coin = {day: found.random.by_day.get(day, []) for day in found.model.by_day}
        assert mean_per_trade(found.model.by_day, **how).lower <= 0, "no edge of its own after costs"
        if all(coin.values()):
            assert advantage(found.model.by_day, coin, **how).lower <= 0, "and none over a coin beyond the interval"
    assert found.model.total_micro <= 0, "it paid for whatever it made up, or made nothing up and traded nothing"


# ---- the weekend run, with the kind set to seq ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    folder = tmp_path_factory.mktemp("seq")
    record(folder / "rec", ordered_weeks(1, ORDER, 36))
    assert go(folder, model="seq", sequence=QUICK) == train.EXIT_OK
    return folder


def test_a_seq_run_leaves_what_any_run_leaves_a_model_a_report_with_the_three_numbers_and_a_candidate(trained):
    settings = load(trained / "learn.yaml")
    (digest,) = ModelRegistry(settings.models).hashes()
    report = the_report(trained)
    model = parse((settings.models / f"{digest}.json").read_bytes())
    assert report["model"] == {"hash": digest, "kind": "seq", "fitted_rows": report["model"]["fitted_rows"]}
    assert model.kind == "seq" and type(scorer_for(model)) is NetScorer
    assert report["config"]["kind"] == "seq" and report["config"]["sequence"] == settings.network.as_dict()
    assert report["config"]["features"]["window_rows"] == 8 == report["serving"]["features"]["window_rows"]
    assert len(model.features) == 8 * 10 and model.features[-10] == "spread@v1" and model.features[0] == "spread@v1[-7]"
    dropped = report["rows"]["dropped"]
    assert set(dropped) == {"window_not_ended", "window_gap", "window_connection", "window_session"}
    assert dropped["window_gap"] == 6 * 7 and dropped["window_session"] == 0, "seven rows a day before a whole window"
    three = report["three_way"]
    assert three["oracle"]["hindsight"] is True and three["random"]["seed"] == 1 and three["no_trade"]["trades"] == 0
    assert three["oracle"]["total_micro"] > three["model"]["total_micro"] > 0 > three["random"]["total_micro"]
    assert three["model"]["trades"] > 100, "twenty passes find some of the order; the test of how much is above"
    assert report["trials"] == 1 == report["deflated_sharpe"]["trials"] and report["not_a_result"] == 1
    assert "expected_micro" in report["summary"]["columns"] and "spread@v1[-3]" in report["summary"]["columns"]
    lineup = read_lineup(settings.lineup)
    (report_path,) = settings.reports.glob("*.json")
    assert [(one.model, one.report) for one in lineup.candidates] == [(digest, report_path.stem)]
    assert lineup.champion is None and lineup.challengers == (), "a candidate serves nowhere until a decision"
    said = json.loads((settings.state_dir / train.LAST_RUN).read_text())
    assert said["model"] == "seq" and said["memory"]["within"] is True and said["memory"]["peak_bytes"] > 0
    assert set(said["took"]) == {"wall_ns", "processor_ms"} and said["took"]["processor_ms"] > 0


def test_the_same_inputs_give_the_same_seq_model_and_report_bytes_and_every_fit_is_one_counted_trial(trained, tmp_path):
    assert go(tmp_path, store=trained / "rec", model="seq", sequence=QUICK) == train.EXIT_OK
    for folder in ("models", "reports"):
        (one,), (other,) = ((base / "learn" / folder).glob("*.json") for base in (trained, tmp_path))
        assert one.name == other.name and one.read_bytes() == other.read_bytes(), folder
    kinds = [entry["what"] for entry in read(tmp_path / "learn" / "trials.jsonl")[1]]
    assert kinds == ["walk_forward"] and the_report(tmp_path)["trials"] == 1
    other_seed = {**READS, "epochs": 2, "seed": 9}
    assert go(tmp_path, store=trained / "rec", model="seq", sequence=other_seed) == train.EXIT_OK
    counts = sorted(json.loads(path.read_bytes())["trials"] for path in (tmp_path / "learn" / "reports").glob("*.json"))
    assert counts == [1, 2] and len(read(tmp_path / "learn" / "trials.jsonl")[1]) == 2, "another seed is another trial"


def test_a_fit_estimated_over_the_budget_is_refused_before_a_trial_is_counted_or_a_weight_is_drawn(trained, tmp_path):
    held = 300_000_000
    code = go(tmp_path, store=trained / "rec", model="seq", sequence=QUICK, memory_budget_mb=301, measure=lambda: held)
    assert code == train.EXIT_REFUSED
    assert not (tmp_path / "learn").exists(), "no trial in the ledger, no model, no report, no run file"
    settings = load(tmp_path / "learn.yaml")
    needed = sequence.needs(the_report(trained)["model"]["fitted_rows"], 80, settings.network)
    assert 1_000_000 < needed < 20_000_000 and held + needed > settings.memory_budget_bytes


def test_a_seq_run_in_a_process_of_its_own_says_what_it_peaked_at_and_how_long_it_took(trained, tmp_path, capsys):
    """The array library, the dataset and the fits are all in the number: the process's own peak, from the system."""
    config = learn_settings(tmp_path, model="seq", sequence=QUICK)
    (tmp_path / "registry.yaml").write_text("history: []\n")
    script = (
        "import sys\nfrom hftlab.core import WallClock\nfrom hftlab.learn import train\n"
        "class Weekend(WallClock):\n    def now_ns(self):\n"
        f"        return {SATURDAY} + super().now_ns() % 10**12\n"
        f"sys.exit(train.main({['--config', str(config), '--cost-model', str(ROOT / 'config/cost_model.yaml')]!r} + "
        f"{['--calendar', str(ROOT / 'config/calendar'), '--feasibility', str(ROOT / 'config/feasibility.yaml')]!r} + "
        f"{['--store', str(trained / 'rec'), '--trial-registry', str(tmp_path / 'registry.yaml')]!r}, Weekend()))\n"
    )
    environment = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=False)
    assert done.returncode == train.EXIT_OK, done.stderr
    said = json.loads((tmp_path / "learn" / train.LAST_RUN).read_text())
    with capsys.disabled():
        took = said["took"]
        print(f"\n  seq run: {said['events']} events, {said['rows']} windows; process peak "
              f"{said['memory']['peak_bytes']} bytes of a budget of {said['memory']['budget_bytes']}; "
              f"{took['wall_ns'] // 10**6} ms by the clock, {took['processor_ms']} ms of processor")  # fmt: skip
    assert said["model"] == "seq" and said["memory"]["within"] is True
    assert 0 < said["memory"]["peak_bytes"] < 600_000_000, "under the slice's 1.2 GB with room"
    assert 0 < said["took"]["wall_ns"] < 300 * 10**9 and said["took"]["processor_ms"] > 100
