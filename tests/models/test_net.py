"""A sequence network as whole numbers: integer sums and shifts, a bound on its rounding, and no value that can overflow."""
from __future__ import annotations

import dataclasses
import json
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.core import Side
from hftlab.features import FeatureRow
from hftlab.learn.train import flat_model
from hftlab.models import (
    BUY, SELL, Decision, ModelError, ModelRegistry, Net, NetError, NetScorer, ScoreError, answer, ask, heard, parse,
    scorer_for,
)  # fmt: skip
from hftlab.models.net import BITS, LIMIT, MOST_SPREAD, WEIGHT_BITS, read
from tests.learn.kit import dataset, planted
from tests.venues.kit import T0

W = 1 << WEIGHT_BITS  # a weight of one
A = 1 << BITS  # an activation of one
ONE = W * A  # an output of one
SPREAD = 1 << 10
SETTINGS = {"window": 2, "kernel": 1}


def by_hand(sell_bias: int = 3 * ONE, **changes) -> Net:
    """Two rows of one value. Buy is the older row's value over its spread, if above zero; sell is a constant.

    One filter of one row with a weight of one, one hidden unit that reads the older place alone, and a buy
    output with a weight of one: every number on the way can be said beforehand.
    """
    parts = {
        "window": 2, "width": 1, "kernel": 1, "clip": 8, "cap": 8, "centre": (100,), "spread": (SPREAD,),
        "conv": ((W,),), "conv_bias": (0,), "hidden": ((W, 0),), "hidden_bias": (0,),
        "out": {BUY: (W,), SELL: (0,)}, "out_bias": {BUY: 0, SELL: sell_bias}, "target_scale": 1_000_000, "rows": 10,
        "settings": SETTINGS, "fitted_with": "by hand",
    }  # fmt: skip
    return Net(**{**parts, **changes})


def a_model(fit: Net | None = None, **changes):
    """A model file of a network, written by hand: nothing that fits one is involved."""
    fit = fit or by_hand()
    keys = tuple(f"x{place}[-{fit.window - 1 - row}]" for row in range(fit.window) for place in range(fit.width))
    made = flat_model(dataset(planted(1, 2)))
    return dataclasses.replace(made, **{"kind": "seq", "features": keys, "params": fit.params(), **changes})


def shown(values, n: int = 1) -> FeatureRow:
    return FeatureRow("EUR.USD", n, T0, tuple(values))


# ---- the arithmetic ------------------------------------------------------------------------------------------------


def test_an_input_is_centred_scaled_cut_off_and_rounded_down_to_a_whole_number_of_the_scale():
    assert read((100, 100 + SPREAD, 100 - SPREAD // 2, 101), (100,), (SPREAD,), 8) == [0, A, -A // 2, A // SPREAD]
    assert read((100 + 9 * SPREAD, 100 - 10**30 % LIMIT), (100,), (SPREAD,), 8) == [8 * A, -8 * A], "cut off at 8"
    assert read((3, 3), (0, 0), (7, 2), 8) == [3 * A // 7, 3 * A // 2], "each value by its own feature's spread"
    assert read((-1,), (0,), (3,), 8) == [-(A // 3) - 1], "rounded down, below zero too"
    for wrong in ((0.5,), (True,), (LIMIT,), (-LIMIT,)):
        with pytest.raises(NetError):
            read(wrong, (0,), (1,), 8)


def test_the_outputs_are_integer_sums_of_the_weights_and_the_activations_and_a_prediction_is_exact():
    net = by_hand()
    assert net.totals((100 + 5 * SPREAD, 7)) == {BUY: 5 * ONE, SELL: 3 * ONE}, "the older row, over its spread"
    assert net.totals((100 - 5 * SPREAD, 7)) == {BUY: 0, SELL: 3 * ONE}, "nothing below zero passes"
    assert net.totals((100 + 99 * SPREAD, 7)) == {BUY: 8 * ONE, SELL: 3 * ONE}, "and nothing above the cap"
    assert net.totals((101, 7))[BUY] == (A // SPREAD) * W and net.predict(BUY, (100 + SPREAD // 2, 0)) == 500_000
    assert net.predict(SELL, (0, 0)) == Fraction(3_000_000) and type(net.predict(BUY, (101, 0))) is Fraction
    assert net.places == 2 and net.parameters == 2 + 3 + 2 + 2
    for wrong in ((1,), (1, 2, 3), (1, 0.5), (1, LIMIT)):
        with pytest.raises(NetError):
            net.totals(wrong)


def test_a_shift_rounds_to_the_nearest_and_a_bias_is_at_the_scale_of_the_product():
    half = by_hand(conv=((W // 2,),), conv_bias=(ONE // 4,))  # half the input, and a quarter
    assert half.totals((100 + SPREAD, 0))[BUY] == 3 * ONE // 4
    odd = by_hand(conv=((W // 2,),))
    assert odd.totals((100 + 3, 0))[BUY] == ((3 * A // SPREAD) // 2) * W, "192 halves to 96: nothing to round"
    assert by_hand(conv=((W // 2 + 1,),)).totals((100 + SPREAD, 0))[BUY] == (A // 2) * W, "32768.004 to the nearest"
    assert by_hand(conv=((3 * W // 4 // A,),)).totals((100 + 2 * SPREAD, 0))[BUY] == 2 * W, "1.5 rounds up to 2"


# ---- the bound, and the scorer that abstains inside it -------------------------------------------------------------


def test_the_slack_is_worked_out_from_the_whole_numbers_as_the_record_derives_it():
    """By hand, for the network above: e = 0 and m = 8 x 2**16 for an input; D = sum|w|e + (sum(m + e) + 1) / 2."""
    net = by_hand()
    top = 8 * A
    conv_apart = Fraction(top + 1, 2)  # one input under the kernel
    conv_off = conv_apart / W + Fraction(1, 2)
    hidden_apart = W * conv_off + (2 * top + 2 * conv_off + 1) / 2  # weights of 1 and 0 on two places
    hidden_off = hidden_apart / W + Fraction(1, 2)
    buy = W * hidden_off + (top + hidden_off + 1) / 2
    sell = (top + hidden_off + 1) / 2
    assert net.slack() == {BUY: -(-buy.numerator // buy.denominator), SELL: -(-sell.numerator // sell.denominator)}
    assert net.slack()[BUY] > net.slack()[SELL] > 0 and sum(net.slack().values()) < ONE >> 14, "far below one"
    assert net.reach() == max(8 * SPREAD * A, top * W + W // 2), "the largest product or sum any row can give"
    assert by_hand(sell_bias=5 * ONE).slack() == net.slack(), "a bias is rounded once, whatever it is"


def test_the_scorer_takes_the_better_side_abstains_within_the_slack_and_expects_whole_millionths_rounded_down():
    net = by_hand()
    both = sum(net.slack().values())
    scorer = scorer_for(a_model(net))
    assert type(scorer) is NetScorer
    assert scorer.score(shown((100 + 5 * SPREAD, 0))) == Decision(Side.BUY, 5_000_000, scorer.model.content_hash)
    assert scorer.score(shown((100 + SPREAD, 0))) == Decision(Side.SELL, 3_000_000, scorer.model.content_hash)
    assert scorer.score(shown((100 + 3 * SPREAD, 0))) is None, "level: neither side is taken"
    assert scorer.score(shown((100 + 3 * SPREAD + 1, 0))).side is Side.BUY, "one step of the input is 2**30: far apart"
    on_the_edge = scorer_for(a_model(by_hand(sell_bias=3 * ONE - both)))
    just_off = scorer_for(a_model(by_hand(sell_bias=3 * ONE - both - 1)))
    assert on_the_edge.score(shown((100 + 3 * SPREAD, 0))) is None, "apart by exactly the two slacks: it could be either"
    assert just_off.score(shown((100 + 3 * SPREAD, 0))) == Decision(Side.BUY, 3_000_000, just_off.model.content_hash)
    losing = scorer_for(a_model(by_hand(sell_bias=-ONE)))
    assert losing.score(shown((100 - SPREAD, 0))) is None, "nothing above zero is expected of either side"
    barely = scorer_for(a_model(by_hand(sell_bias=net.slack()[SELL], out={BUY: (0,), SELL: (0,)})))
    assert barely.score(shown((0, 0))) is None, "above zero by no more than its own slack is not above zero"
    odd = scorer_for(a_model(by_hand(target_scale=999_999)))
    assert odd.score(shown((100 + 5 * SPREAD + 1, 0))).expected_micro == (5 * ONE + (1 << 30)) * 999_999 >> 40


def test_a_row_the_model_cannot_read_is_refused_and_the_request_and_reply_lines_carry_a_window():
    scorer = scorer_for(a_model())
    for wrong in (shown((1,)), shown((1, 2, 3)), FeatureRow("USD.JPY", 1, T0, (1, 2)), shown((1, LIMIT))):
        with pytest.raises(ScoreError):
            scorer.score(wrong)
    row = shown((100 + 5 * SPREAD, 0))
    line = ask(row)
    assert json.loads(line) == {"pair": "EUR.USD", "n": 1, "known_ns": T0, "values": [100 + 5 * SPREAD, 0]}
    assert heard(answer(scorer, line)) == scorer.score(row), "the window is the row: nothing else crosses"
    with pytest.raises(ScoreError):
        answer(scorer, line.replace(str(100 + 5 * SPREAD), "0.5"))


# ---- the file ------------------------------------------------------------------------------------------------------


def test_a_network_round_trips_through_a_model_file_and_the_registry_as_integers_and_text(tmp_path):
    model = a_model()
    assert parse(model.data()) == model and Net.from_params(json.loads(json.dumps(by_hand().params()))) == by_hand()
    assert b"." not in model.data().replace(b"EUR.USD", b"").replace(b'"by hand"', b""), "no float in the file"
    registry = ModelRegistry(tmp_path)
    digest = registry.put(model)
    loaded = registry.load(digest, pair="EUR.USD", features=model.features, label=model.label)
    assert scorer_for(loaded).score(shown((100 + 5 * SPREAD, 0))).expected_micro == 5_000_000
    assert by_hand().params()["bits"] == 16 and by_hand().params()["weight_bits"] == 24


@pytest.mark.parametrize(
    "wrong",
    [
        {"window": 0}, {"window": 65}, {"kernel": 3}, {"clip": 0}, {"cap": 0}, {"target_scale": 0}, {"rows": 0},
        {"centre": (0, 0)}, {"spread": (0,)}, {"spread": (MOST_SPREAD,)}, {"centre": (LIMIT,)}, {"conv": ((W, W),)},
        {"conv": ()}, {"conv_bias": (0, 0)}, {"hidden": ((W,),)}, {"hidden": ()}, {"hidden_bias": ()},
        {"out": {BUY: (W, W), SELL: (0,)}}, {"out": {}}, {"out_bias": {BUY: 0}}, {"window": 1.0},
        {"conv": ((W << 40,),)}, {"out_bias": {BUY: LIMIT, SELL: 0}},
    ],
)  # fmt: skip
def test_a_network_whose_shape_does_not_fit_or_that_could_overflow_is_refused(wrong):
    with pytest.raises(NetError):
        by_hand(**wrong)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.pop("clip"), lambda p: p.update(extra=1), lambda p: p.update(bits=8), lambda p: p.update(weight_bits=16),
        lambda p: p.update(conv=[[0.5]]), lambda p: p.update(conv="1"), lambda p: p.update(centre=[True]),
        lambda p: p.update(out=[1]), lambda p: p.update(settings={"window": 0.5}), lambda p: p.update(window="2"),
        lambda p: p.update(out_bias={"buy": 0.5, "sell": 0}), lambda p: p.update(out={"buy": [W]}),
    ],
)  # fmt: skip
def test_parameters_that_are_not_a_network_are_refused_by_the_file_and_by_the_scorer(change):
    params = by_hand().params()
    change(params)
    with pytest.raises((NetError, ModelError)):
        scorer_for(a_model(params=params))
    with pytest.raises(ModelError):
        scorer_for(a_model(kind="gbm"))
    with pytest.raises(ModelError, match="every value of its window"):
        scorer_for(a_model(features=("only", "two", "three")))


# ---- nothing overflows, whatever the row ---------------------------------------------------------------------------

WEIGHTS = st.integers(-4 * W, 4 * W)
BIASES = st.integers(-4 * ONE, 4 * ONE)
VALUES = st.one_of(st.integers(-(LIMIT - 1), LIMIT - 1), st.integers(-(10**6), 10**6), st.sampled_from([0, LIMIT - 1, 1 - LIMIT]))


@st.composite
def nets(draw):
    window, width = draw(st.integers(2, 5)), draw(st.integers(1, 3))
    kernel, filters, hidden = draw(st.integers(1, window)), draw(st.integers(1, 3)), draw(st.integers(1, 3))
    lists = lambda count, of: draw(st.lists(of, min_size=count, max_size=count))  # noqa: E731
    spreads = st.one_of(st.integers(1, 1_000), st.sampled_from([1, MOST_SPREAD - 1]))
    net = Net(
        window, width, kernel, draw(st.integers(1, 16)), draw(st.integers(1, 16)),
        tuple(lists(width, st.integers(-(LIMIT - 1), LIMIT - 1))), tuple(lists(width, spreads)),
        tuple(tuple(lists(kernel * width, WEIGHTS)) for _ in range(filters)), tuple(lists(filters, BIASES)),
        tuple(tuple(lists((window - kernel + 1) * filters, WEIGHTS)) for _ in range(hidden)),
        tuple(lists(hidden, BIASES)), {BUY: tuple(lists(hidden, WEIGHTS)), SELL: tuple(lists(hidden, WEIGHTS))},
        {BUY: draw(BIASES), SELL: draw(BIASES)}, draw(st.integers(1, 10**9)), 1, {}, "drawn",
    )  # fmt: skip
    return net, tuple(lists(window * width, VALUES))


@given(nets())
@settings(max_examples=300, deadline=None)
def test_no_intermediate_value_can_overflow_for_any_row_within_the_documented_range(drawn):
    """Every product and every running sum of a score, against the bound the file is accepted under.

    The weights, biases, centres, spreads, clips and caps are drawn up to the most a file may hold, and the
    row's values up to the most a row may: far beyond any feature of the platform, whose largest are
    nanoseconds within a week.
    """
    net, values = drawn
    peak = net.peak(values)
    assert peak <= max(net.reach(), 2 * (LIMIT - 1)) < 2**63, "inside a signed 64-bit integer, with the bound holding"
    assert all(abs(total) <= net.reach() for total in net.totals(values).values())
    top = net.clip << BITS
    assert all(-top <= value <= top for value in net.inputs(values)), "an input is cut off whatever the row holds"
    assert all(type(total) is int for total in net.totals(values).values())
