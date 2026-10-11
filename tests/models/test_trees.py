"""Boosted trees as whole numbers: a row goes the way the thresholds say, and nothing in the file or the sum floats."""
from __future__ import annotations

import dataclasses
import json
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import Side
from hftlab.features import FeatureRow
from hftlab.learn.train import flat_model
from hftlab.models import (
    BUY, SELL, Decision, ModelError, ModelRegistry, ScoreError, Tree, Trees, TreeScorer, TreesError, answer, ask, heard,
    parse, scorer_for,
)  # fmt: skip
from tests.learn.kit import dataset, planted
from tests.venues.kit import T0

# Two splits: is feature 0 at most 4? If so, is feature 1 at most -1? Leaves 0, 1, 2 from the left.
#            f0 <= 4
#           /       \
#      f1 <= -1     leaf 2
#      /      \
#   leaf 0   leaf 1
TREE = Tree(feature=(0, 1), threshold=(4, -1), left=(1, ~0), right=(~2, ~1), leaves=(7_500, -2_000, 300))
STUMP = Tree((), (), (), (), (1_250,))
SETTINGS = {"trees": 2, "leaf_scale": 1000}


def trees(buy=(TREE, STUMP), sell=(STUMP,), scale: int = 1000, width: int = 2) -> Trees:
    return Trees(scale, 10, width, {BUY: tuple(buy), SELL: tuple(sell)}, SETTINGS, "by hand")


def a_model(fit: Trees | None = None, **changes):
    """A model file of boosted trees over the planted dataset's ten features, written by hand: no library is involved."""
    made = dataset(planted(1, 2))
    fit = fit or trees(width=len(made.features))
    return dataclasses.replace(flat_model(made), kind="gbm", **{"params": fit.params(), **changes})


def shown(values, n: int = 1) -> FeatureRow:
    return FeatureRow("EUR.USD", n, T0, tuple(values))


# ---- the arithmetic ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "leaf"), [((4, -1), 0), ((4, 0), 1), ((5, -9), 2), ((-(10**30), -(10**30)), 0), ((10**30, 0), 2)]
)
def test_a_row_goes_left_when_its_value_is_at_most_the_threshold_and_right_when_it_is_above(values, leaf):
    assert TREE.leaf(values) == leaf and STUMP.leaf(values) == 0, "a tree without a split is its one leaf"


def test_a_prediction_is_the_sum_of_one_leaf_per_tree_over_the_scale_and_it_is_exact():
    fit = trees()
    assert fit.total(BUY, (4, -1)) == 7_500 + 1_250 and fit.total(SELL, (4, -1)) == 1_250
    assert fit.predict(BUY, (4, -1)) == Fraction(35, 4) and type(fit.total(BUY, (9, 9))) is int
    assert trees(scale=3).predict(BUY, (4, -1)) == Fraction(8_750, 3), "a fraction, never a rounded quotient"


def nested(depth: int):
    """A tree as nested tuples: a leaf value, or (feature, threshold, left, right)."""
    leaf = st.integers(-(10**9), 10**9)
    if depth == 0:
        return leaf
    split = st.tuples(st.integers(0, 3), st.integers(-50, 50), nested(depth - 1), nested(depth - 1))
    return st.one_of(leaf, split)


def flattened(node) -> Tree:
    feature, threshold, left, right, leaves = [], [], [], [], []

    def walk(node) -> int:
        if isinstance(node, int):
            leaves.append(node)
            return ~(len(leaves) - 1)
        at = len(feature)
        feature.append(node[0]), threshold.append(node[1]), left.append(0), right.append(0)
        left[at], right[at] = walk(node[2]), walk(node[3])
        return at

    walk(node)
    return Tree(tuple(feature), tuple(threshold), tuple(left), tuple(right), tuple(leaves))


def by_the_definition(node, values) -> int:
    while not isinstance(node, int):
        node = node[2] if values[node[0]] <= node[1] else node[3]
    return node


@given(nested(4), st.lists(st.integers(-60, 60), min_size=4, max_size=4))
def test_any_tree_written_flat_gives_the_leaf_its_definition_gives(node, values):
    tree = flattened(node)
    assert tree.leaves[tree.leaf(values)] == by_the_definition(node, values)
    assert Tree.from_plain(json.loads(json.dumps(tree.plain()))) == tree


# ---- what is not a tree --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrong",
    [
        dict(left=(0, ~0)),  # a split that leads back to itself: a row would never arrive
        dict(left=(1, ~0), right=(1, ~1)),  # a split reached twice, and a leaf never
        dict(right=(~3, ~1)),  # a leaf the tree does not have
        dict(leaves=(1, 2)),  # one leaf too few
        dict(threshold=(4,)),  # ragged
        dict(left=(2, ~0)),  # a split the tree does not have
    ],
)
def test_a_tree_whose_splits_do_not_lead_every_row_to_one_leaf_is_refused(wrong):
    with pytest.raises(TreesError):
        dataclasses.replace(TREE, **wrong)


def test_trees_that_ask_about_a_feature_the_row_lacks_or_hold_a_number_that_is_not_whole_are_refused():
    with pytest.raises(TreesError, match="a feature the row does not have"):
        trees(width=1)
    with pytest.raises(TreesError, match="whole numbers above zero"):
        trees(scale=0)
    plain = trees().params()
    for damage in (
        lambda p: p["targets"]["buy"][0].__setitem__("threshold", [4, -0.5]),
        lambda p: p["targets"]["buy"][0].__setitem__("leaves", [7_500, -2_000, 300.0]),
        lambda p: p["targets"]["buy"][0].pop("left"),
        lambda p: p.__setitem__("scale", 1000.0),
        lambda p: p.pop("width"),
        lambda p: p["settings"].__setitem__("trees", 2.5),
        lambda p: p.__setitem__("targets", {"buy": "none"}),
    ):
        damaged = json.loads(json.dumps(plain))
        damage(damaged)
        with pytest.raises(TreesError):
            Trees.from_params(damaged)
    assert Trees.from_params(json.loads(json.dumps(plain))) == trees()
    with pytest.raises(TreesError, match="whole numbers"):
        trees().total(BUY, (4.0, -1))
    with pytest.raises(TreesError, match="whole numbers"):
        trees().total(BUY, (4,))


# ---- the model file and the scorer ---------------------------------------------------------------------------------


def test_the_model_file_holds_whole_numbers_and_text_only_and_comes_back_as_it_was(tmp_path):
    model = a_model()
    data = model.data()
    assert b"." not in data.replace(b"EUR.USD", b"") and b"e+" not in data.lower(), "no float is written"

    def flat(value):
        if isinstance(value, dict):
            return [leaf for inner in value.values() for leaf in flat(inner)]
        return [leaf for inner in value for leaf in flat(inner)] if isinstance(value, list) else [value]

    assert {type(leaf) for leaf in flat(json.loads(data))} <= {int, str, bool}
    assert parse(data) == model and parse(data).content_hash == model.content_hash
    registry = ModelRegistry(tmp_path)
    digest = registry.put(model)
    assert registry.load(digest, pair="EUR.USD", features=model.features, label=model.label) == model
    assert a_model(trees(scale=999, width=10)).content_hash != model.content_hash, "the scale is part of what it is"


def test_the_scorer_takes_the_better_side_in_whole_millionths_rounded_down_or_abstains():
    model = a_model()
    scorer = scorer_for(model)
    assert type(scorer) is TreeScorer and scorer.model is model
    row = [0] * 10
    assert scorer.score(shown([4, -1, *row[2:]])) == Decision(Side.BUY, 8, model.content_hash), "8750 / 1000, down"
    assert scorer.score(shown([4, 0, *row[2:]])) == Decision(Side.SELL, 1, model.content_hash), "-750 against 1250"
    even = a_model(trees(buy=(STUMP,), sell=(STUMP,), width=10))
    assert scorer_for(even).score(shown(row)) is None, "the sides cannot be told apart"
    losing = Tree((), (), (), (), (-5,))
    small = a_model(trees(buy=(losing,), sell=(STUMP, losing, losing), width=10))
    assert scorer_for(small).score(shown(row)) == Decision(Side.SELL, 1, small.content_hash), "1240 / 1000, down"
    nothing = a_model(trees(buy=(losing,), sell=(Tree((), (), (), (), (0,)),), width=10))
    assert scorer_for(nothing).score(shown(row)) is None, "nothing above zero is expected of either side"


def test_the_scorer_refuses_a_row_or_a_model_that_is_not_its_own():
    scorer = scorer_for(a_model())
    with pytest.raises(ScoreError):
        scorer.score(shown([0] * 9))
    with pytest.raises(ScoreError, match="whole numbers"):
        scorer.score(shown([0.5, *[0] * 9]))
    with pytest.raises(ModelError, match="handed to the scorer of gbm"):
        TreeScorer(flat_model(dataset(planted(1, 2))))
    with pytest.raises(ModelError, match="the outcome of each side from every feature"):
        TreeScorer(a_model(trees(width=4)))
    with pytest.raises(ModelError, match="does not hold boosted trees"):
        TreeScorer(a_model(params={"scale": 1000}))
    one_sided = Trees(1000, 10, 10, {BUY: (STUMP,)}, SETTINGS, "by hand")
    with pytest.raises(ModelError, match="the outcome of each side"):
        TreeScorer(a_model(one_sided))


def test_asked_as_a_line_it_answers_as_it_does_asked_directly():
    scorer = scorer_for(a_model())
    for values in ([4, -1, *[0] * 8], [4, 0, *[0] * 8], [9, 9, *[3] * 8]):
        assert heard(answer(scorer, ask(shown(values)))) == scorer.score(shown(values))
