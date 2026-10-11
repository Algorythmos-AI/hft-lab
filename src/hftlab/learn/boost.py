"""Gradient boosting: fitted by a library, in floats, and kept as whole numbers (ADR 0025).

This is the one module of the platform that imports the gradient-boosting library, and it does so inside the
function that fits, so that importing this module costs nothing and needs nothing. The library is in the
optional ML set (`make ml`). It is needed to train a model of this kind, and by nothing else: not by the
recorder, not by the trader, and not to score a model once it is trained.

    fit(rows, targets, settings) -> Trees        `grow`, in the library's floats, and then `keep`

`rows` and `targets` are the whole numbers of a dataset. The library sees them as floats, which hold every one
of them exactly or the fit is refused. It grows a fixed number of small trees per target, on one thread, in its
deterministic mode, with nothing subsampled. Then the floats stop:

  * a split's threshold `t` becomes the whole number `floor(t)`. A feature is a whole number, and for a whole
    number `x`, `x <= t` and `x <= floor(t)` are the same question, so the row goes the same way;
  * a leaf's value `v`, in millionths of the quote currency, becomes the whole number nearest to `v * scale`.
    That is the one rounding. It moves a prediction by at most half of `1 / scale` per tree.

What is returned is `hftlab.models.trees.Trees`: whole numbers, scored by whole-number arithmetic. Before it
is returned, a sample of the training rows is sent down both the library's trees and the exported ones, and
the fit is refused unless every row ends in the same leaf of every tree.

What it does not promise. The same rows, settings and seed give the same trees on the same build of the
library on the same machine, and a test holds that. Another build, or another processor, may round a sum
differently and grow another tree. That changes which model a training run registers there; it cannot change
what a registered model decides, because a model is served by the hash of its file and scored without floats.

Limits. Trees find a step, a band or an interaction that a straight line cannot. They also fit noise with more
enthusiasm than a straight line does, which is why the settings are small and fixed, and why a model of this
kind is judged like any other: walk-forward, beside the oracle and a coin, with every trial counted.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any, Final

from hftlab.learn.config import Boosting
from hftlab.models.trees import MOST_LEAVES, Tree, Trees, TreesError

LIBRARY: Final = "lightgbm"
INSTALL: Final = "make ml"
EXACT: Final = 2**53  # a float holds every whole number below this exactly
CHECKED_ROWS: Final = 512  # training rows sent down both sets of trees before a fit is kept
PPM: Final = 1_000_000


class BoostError(ValueError):
    """The rows cannot be fitted, or the fitted trees cannot be written as whole numbers."""


class TooFewRows(BoostError):
    """There are not two rows to fit. Nothing is wrong with the rows there are."""


class LibraryMissing(Exception):
    """The gradient-boosting library is not installed. It is optional, and only training needs it."""


def library() -> Any:
    """The library, imported now. Raises `LibraryMissing` when the optional ML set is not installed.

    Or when it is installed and cannot be loaded: its compiled part needs the system's OpenMP runtime, and
    says so with an `OSError`.
    """
    try:
        import lightgbm
    except (ImportError, OSError) as exc:
        raise LibraryMissing(
            f"a gbm model is trained with {LIBRARY}, which is in the optional ML set and is not installed: "
            f"run `{INSTALL}` ({exc})"
        ) from exc
    return lightgbm


def fitted_with() -> str:
    """The library and its version, as a model file names where its trees came from."""
    return f"{LIBRARY} {library().__version__}"


def parameters(settings: Boosting) -> dict[str, object]:
    """What the library is told. One thread, its deterministic mode, every row and every feature in every tree."""
    return {
        "objective": "regression",  # squared error
        "boosting": "gbdt",
        "num_leaves": settings.leaves,
        "max_depth": settings.max_depth,
        "learning_rate": settings.learning_rate_ppm / PPM,
        "min_data_in_leaf": settings.min_leaf_rows,
        "lambda_l2": settings.l2_ppm / PPM,
        "max_bin": settings.bins,
        "seed": settings.seed,
        "deterministic": True,
        "force_row_wise": True,  # the other layout is chosen by a timing, which is not the same twice
        "num_threads": 1,
        "bagging_fraction": 1.0,  # no rows are drawn
        "bagging_freq": 0,
        "feature_fraction": 1.0,  # no features are drawn
        "extra_trees": False,  # no thresholds are drawn
        "use_missing": False,  # a row has every feature, so a split has no third way
        "enable_bundle": False,
        "linear_tree": False,
        "boost_from_average": True,
        "verbosity": -1,
    }


def _matrix(rows: Sequence[Sequence[int]], width: int) -> Any:
    import numpy

    for position, row in enumerate(rows):
        if len(row) != width or any(type(value) is not int or abs(value) >= EXACT for value in row):
            raise BoostError(f"row {position} is not {width} whole numbers a float holds exactly")
    return numpy.array(rows, dtype=numpy.float64)


def _column(name: str, values: Sequence[int], count: int) -> Any:
    import numpy

    if len(values) != count or any(type(value) is not int or abs(value) >= EXACT for value in values):
        raise BoostError(f"target {name} is not one whole number per row that a float holds exactly")
    return numpy.array(values, dtype=numpy.float64)


def exported(structure: Mapping[str, Any], scale: int) -> Tree:
    """One of the library's trees as whole numbers. Splits are numbered parent first; leaves keep their numbers."""
    feature: list[int] = []
    threshold: list[int] = []
    left: list[int] = []
    right: list[int] = []
    leaves: dict[int, int] = {}

    def walk(node: Mapping[str, Any]) -> int:
        if "leaf_value" in node:
            place = int(node.get("leaf_index", 0))
            if place in leaves or not 0 <= place < MOST_LEAVES:
                raise BoostError("the library numbered a leaf twice, or beyond what a tree may hold")
            leaves[place] = round(Fraction(node["leaf_value"]) * scale)  # exact, then to the nearest whole number
            return ~place
        if node.get("decision_type") != "<=" or node.get("missing_type") != "None":
            raise BoostError("the library made a split that is not `value <= threshold` on a row with every feature")
        at = len(feature)
        feature.append(int(node["split_feature"]))
        threshold.append(math.floor(node["threshold"]))
        left.append(0)
        right.append(0)
        left[at] = walk(node["left_child"])
        right[at] = walk(node["right_child"])
        return at

    try:
        walk(structure)
        if sorted(leaves) != list(range(len(leaves))):
            raise BoostError("the library's leaves are not numbered from zero without a gap")
        values = tuple(leaves[place] for place in sorted(leaves))
        return Tree(tuple(feature), tuple(threshold), tuple(left), tuple(right), values)
    except BoostError:
        raise
    except (TreesError, KeyError, TypeError, OverflowError, ValueError) as exc:  # a value that is not a number
        raise BoostError(f"the library's tree cannot be kept as whole numbers: {exc!r}") from exc


def _agree(booster: Any, trees: Sequence[Tree], matrix: Any, rows: Sequence[Sequence[int]]) -> None:
    """Refuse the fit unless the exported trees send a sample of the training rows where the library sends them."""
    count = len(rows)
    sample = sorted({position * count // CHECKED_ROWS for position in range(CHECKED_ROWS)})
    theirs = booster.predict(matrix[sample], pred_leaf=True).reshape(len(sample), -1).tolist()
    for position, found in zip(sample, theirs, strict=True):
        ours = [tree.leaf(rows[position]) for tree in trees]
        if ours != [int(leaf) for leaf in found]:
            raise BoostError(f"the exported trees and the library's part ways on training row {position}")


def grow(rows: Sequence[Sequence[int]], targets: Mapping[str, Sequence[int]], settings: Boosting) -> dict[str, Any]:
    """The library's own models, one per target, fitted to the rows. Floats; what `keep` turns into whole numbers."""
    made = library()
    count = len(rows)
    width = len(rows[0]) if rows else 0
    if count < 2:
        raise TooFewRows("a fit needs two rows")
    if width < 1 or not targets:
        raise BoostError("a fit needs one feature and one target")
    matrix = _matrix(rows, width)
    told = parameters(settings)
    grown = {}
    for name in sorted(targets):
        label = _column(name, targets[name], count)
        try:
            data = made.Dataset(matrix, label=label, params=dict(told), free_raw_data=False)
            grown[name] = made.train(dict(told), data, num_boost_round=settings.trees)
        except made.basic.LightGBMError as exc:
            raise BoostError(f"the library could not fit target {name}: {exc}") from exc
    return grown


def keep(grown: Mapping[str, Any], rows: Sequence[Sequence[int]], settings: Boosting) -> Trees:
    """The library's models as whole numbers, checked against the library on a sample of the rows they were fitted to.

    `rows` are the rows `grown` was fitted to.
    """
    matrix = _matrix(rows, len(rows[0]))
    found: dict[str, tuple[Tree, ...]] = {}
    for name in sorted(grown):
        dumped = grown[name].dump_model()
        if dumped.get("num_tree_per_iteration") != 1 or dumped.get("average_output"):
            raise BoostError("the library fitted something other than one summed tree per round")
        trees = tuple(exported(tree["tree_structure"], settings.leaf_scale) for tree in dumped["tree_info"])
        if not trees:
            raise BoostError(f"the library grew no tree for target {name}")
        _agree(grown[name], trees, matrix, rows)
        found[name] = trees
    try:
        return Trees(settings.leaf_scale, len(rows), len(rows[0]), found, settings.as_dict(), fitted_with())
    except TreesError as exc:
        raise BoostError(f"the fitted trees cannot be kept: {exc}") from exc


def fit(rows: Sequence[Sequence[int]], targets: Mapping[str, Sequence[int]], settings: Boosting) -> Trees:
    """Fit every target to the same rows, and hand back the trees as whole numbers."""
    return keep(grow(rows, targets, settings), rows, settings)
