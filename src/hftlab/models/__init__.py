"""Models: a hashed file format, a directory that never overwrites, an exact ridge regression, boosted trees kept as
whole numbers, two baselines, a scorer."""
from hftlab.models.baseline import number
from hftlab.models.file import Model, ModelError, ModelMismatch, ModelRegistry, parse
from hftlab.models.ridge import Ridge, RidgeError, fit
from hftlab.models.scorer import (
    BUY,
    SELL,
    Decision,
    FlatScorer,
    RandomScorer,
    RidgeScorer,
    ScoreError,
    Scorer,
    TreeScorer,
    answer,
    ask,
    heard,
    scorer_for,
)
from hftlab.models.trees import Tree, Trees, TreesError

__all__ = [
    "BUY",
    "SELL",
    "Decision",
    "FlatScorer",
    "Model",
    "ModelError",
    "ModelMismatch",
    "ModelRegistry",
    "RandomScorer",
    "Ridge",
    "RidgeError",
    "RidgeScorer",
    "ScoreError",
    "Scorer",
    "Tree",
    "TreeScorer",
    "Trees",
    "TreesError",
    "answer",
    "ask",
    "fit",
    "heard",
    "number",
    "parse",
    "scorer_for",
]
