"""Boosted regression trees kept as whole numbers, and the arithmetic that scores them (ADR 0025).

A model of this kind is a list of small trees per target. A tree asks, at each split, whether one feature of
the row is at most a threshold, goes left if so and right if not, and ends in a leaf. The prediction for a
target is the sum of the leaves the row ends in, one per tree.

Everything here is a whole number:

  * a threshold is on the feature's own integer scale, so the question is `value <= threshold` between two
    whole numbers;
  * a leaf is a whole number of `1 / scale` millionths of the quote currency, and `scale` is in the file;
  * a sum of leaves is a whole number, and a prediction is that sum over `scale`: a fraction.

So the same row gives the same sum on any machine, and a sleeve that asks this model replays byte for byte.
The trees were fitted elsewhere, by a library that works in floats (`hftlab.learn.boost`); what is kept is the
result of that fit, rounded once to the scale and written down. Nothing here imports that library, and nothing
here can train.

A split is numbered, and a split's children that are splits have higher numbers than it, so following a row
down a tree always ends. A child below zero is a leaf: `~child` is its place among the leaves.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

KIND: Final = "gbm"
MOST_TREES: Final = 500  # per target
MOST_LEAVES: Final = 64  # per tree
_SHAPE: Final = ("feature", "threshold", "left", "right", "leaves")


class TreesError(ValueError):
    """These are not trees this module can score: a split that leads nowhere, a number that is not whole."""


def _whole(values: object, what: str) -> tuple[int, ...]:
    if not isinstance(values, list | tuple) or any(type(value) is not int for value in values):
        raise TreesError(f"a tree's {what} is a list of whole numbers")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class Tree:
    feature: tuple[int, ...]  # per split: the place in the row of the feature it asks about
    threshold: tuple[int, ...]  # per split: left when the value is at most this
    left: tuple[int, ...]  # per split: a later split's number, or ~leaf
    right: tuple[int, ...]
    leaves: tuple[int, ...]  # in 1 / scale millionths of the quote currency

    def __post_init__(self) -> None:
        splits = len(self.feature)
        if not len(self.threshold) == len(self.left) == len(self.right) == splits:
            raise TreesError("a tree names a feature, a threshold and two children for every split")
        if len(self.leaves) != splits + 1 or len(self.leaves) > MOST_LEAVES:
            raise TreesError(f"a tree has one leaf more than it has splits, and at most {MOST_LEAVES}")
        reached_splits, reached_leaves = [0] * splits, [0] * len(self.leaves)
        for parent in range(splits):
            for child in (self.left[parent], self.right[parent]):
                if child >= 0:
                    if not parent < child < splits:
                        raise TreesError("a split leads to a later split of the same tree, or to a leaf")
                    reached_splits[child] += 1
                else:
                    if ~child >= len(self.leaves):
                        raise TreesError("a split leads to a leaf the tree does not have")
                    reached_leaves[~child] += 1
        if any(count != 1 for count in reached_splits[1:]) or (splits and any(count != 1 for count in reached_leaves)):
            raise TreesError("every split but the first, and every leaf, is reached from exactly one split")

    def leaf(self, values: Sequence[int]) -> int:
        """The place of the leaf this row ends in."""
        if not self.feature:
            return 0
        at = 0
        while at >= 0:
            at = self.left[at] if values[self.feature[at]] <= self.threshold[at] else self.right[at]
        return ~at

    def plain(self) -> dict[str, object]:
        return {
            "feature": list(self.feature), "threshold": list(self.threshold), "left": list(self.left),
            "right": list(self.right), "leaves": list(self.leaves),
        }  # fmt: skip

    @classmethod
    def from_plain(cls, plain: object) -> Tree:
        if not isinstance(plain, dict) or set(plain) != set(_SHAPE):
            raise TreesError(f"a tree is written as {', '.join(_SHAPE)}")
        return cls(*(_whole(plain[name], name) for name in _SHAPE))


@dataclass(frozen=True, slots=True)
class Trees:
    scale: int  # a leaf is this many to the millionth
    rows: int  # the rows it was fitted to
    width: int  # the features a row has
    trees: Mapping[str, tuple[Tree, ...]]  # per target
    settings: Mapping[str, int]  # the numbers it was fitted with, as the learning settings wrote them
    fitted_with: str  # the library and its version: where the trees came from, never what scores them

    def __post_init__(self) -> None:
        if type(self.scale) is not int or self.scale < 1 or type(self.width) is not int or self.width < 1:
            raise TreesError("the scale of the leaves and the number of features are whole numbers above zero")
        if type(self.rows) is not int or self.rows < 1 or not self.trees:
            raise TreesError("trees were fitted to at least one row, for at least one target")
        for found in self.trees.values():
            if not 1 <= len(found) <= MOST_TREES:
                raise TreesError(f"a target has between one tree and {MOST_TREES}")
            for tree in found:
                if any(not 0 <= feature < self.width for feature in tree.feature):
                    raise TreesError(f"a split asks about a feature the row does not have: it has {self.width}")

    def total(self, target: str, values: Sequence[int]) -> int:
        """The sum of the leaves this row ends in, in 1 / scale millionths. Whole numbers in, a whole number out."""
        if len(values) != self.width or any(type(value) is not int for value in values):
            raise TreesError(f"the model reads {self.width} whole numbers")
        return sum(tree.leaves[tree.leaf(values)] for tree in self.trees[target])

    def predict(self, target: str, values: Sequence[int]) -> Fraction:
        """What the target is expected to be for this row, in millionths: exact."""
        return Fraction(self.total(target, values), self.scale)

    def params(self) -> dict[str, object]:
        """The trees as a model file keeps them: whole numbers and text."""
        return {
            "scale": self.scale,
            "rows": self.rows,
            "width": self.width,
            "fitted_with": self.fitted_with,
            "settings": {name: self.settings[name] for name in sorted(self.settings)},
            "targets": {name: [tree.plain() for tree in self.trees[name]] for name in sorted(self.trees)},
        }

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> Trees:
        try:
            targets, settings = params["targets"], params["settings"]
            if not isinstance(targets, dict) or not isinstance(settings, dict) or len(params) != 6:
                raise TreesError("the parameters of boosted trees are a scale, the rows, the width and the trees")
            if any(type(value) is not int for value in settings.values()):
                raise TreesError("the numbers the trees were fitted with are whole")
            scale, rows, width = params["scale"], params["rows"], params["width"]
            if type(scale) is not int or type(rows) is not int or type(width) is not int:
                raise TreesError("the scale, the rows and the width are whole numbers")
            trees = {}
            for name, found in targets.items():
                if not isinstance(found, list):
                    raise TreesError("a target has a list of trees")
                trees[str(name)] = tuple(Tree.from_plain(tree) for tree in found)
            return cls(scale, rows, width, trees, dict(settings), str(params["fitted_with"]))
        except KeyError as exc:
            raise TreesError(f"these are not the parameters of boosted trees: {exc!r} is missing") from exc
