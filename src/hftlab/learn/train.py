"""Training: a dataset's rows into a model file. One real model and the two baselines every report needs.

The model is fitted to the rows it is handed and to nothing else: the positions come from a fold of
`hftlab.learn.validate`, which has already removed every row whose label had not ended. What the model file
records is taken from the dataset, so a model cannot part from the features, the label and the costs it was
fitted under.
"""
from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from hftlab.learn.dataset import Dataset
from hftlab.models.file import Model
from hftlab.models.ridge import fit
from hftlab.models.scorer import BUY, SELL


def _model(dataset: Dataset, kind: str, params: dict[str, object]) -> Model:
    return Model(
        kind, dataset.pair, dataset.features, dict(dataset.label), dict(dataset.cost_model), dataset.size,
        dataset.content_hash, params,
    )  # fmt: skip


def ridge_model(dataset: Dataset, positions: Sequence[int], penalty: Fraction) -> Model:
    """A ridge regression of the outcome of each side on the features, fitted exactly to the rows at `positions`."""
    rows = [dataset.rows[position] for position in positions]
    targets = {BUY: [row.buy_micro for row in rows], SELL: [row.sell_micro for row in rows]}
    return _model(dataset, "ridge", fit([row.features for row in rows], targets, penalty).params())


def flat_model(dataset: Dataset) -> Model:
    """The baseline that never trades."""
    return _model(dataset, "flat", {})


def random_model(dataset: Dataset, seed: int) -> Model:
    """The baseline that takes a side by a seeded coin."""
    return _model(dataset, "random", {"seed": seed})
