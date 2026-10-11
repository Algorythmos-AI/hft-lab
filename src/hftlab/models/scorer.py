"""The scorer: a feature row in, a decision or nothing out (ADR 0022).

    score(feature_row) -> Decision | None

A decision is a side and the outcome the model expects from taking it, after costs, in whole millionths of the
quote currency, rounded down. None is an abstention: the model expects nothing above zero from either side, or
cannot tell the two apart. Whether an expectation is large enough to trade on is not the scorer's business:
the sleeve that asks has a margin of its own.

The interface is made to cross a process boundary later. A request and a reply are each one line of canonical
JSON holding integers and text only (`ask`, `answer`, `heard`), so the scorer can be moved into a process of
its own without the caller changing. In this round it runs in the caller's process.

Scoring is exact. A ridge model's weights are fractions and a row's values are whole numbers; each prediction
is one integer sum and one division, and the two sides are compared as fractions. The same row gives the same
decision on any machine, which is what lets a sleeve that asks a scorer replay byte for byte.

So is a model of boosted trees (ADR 0025). Its thresholds and its leaves are whole numbers in the model file;
a prediction is a sum of whole numbers, and the two sides are compared as whole numbers. The library that
fitted the trees is not imported here and is not needed to score them.

And so is a small sequence network (ADR 0029). Its row is a window, the last rows of the pair side by side, and
its weights are whole numbers at a fixed scale; scoring is integer sums, shifts and comparisons
(`hftlab.models.net`). The whole numbers differ from the network that was fitted by a rounding whose bound is
worked out from the file itself, and the scorer abstains wherever that bound could change the decision: where
it does decide, the fitted network decides the same.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from hftlab.core import Side
from hftlab.features import FeatureRow
from hftlab.journal import canonical
from hftlab.models.baseline import number
from hftlab.models.file import Model, ModelError
from hftlab.models.net import BITS, WEIGHT_BITS, Net, NetError
from hftlab.models.net import KIND as SEQ
from hftlab.models.ridge import Ridge
from hftlab.models.trees import KIND as GBM
from hftlab.models.trees import Trees, TreesError

BUY, SELL = "buy", "sell"  # the two targets a model is fitted to: the outcome of taking each side


class ScoreError(ValueError):
    """The row is not one this model can score: another pair, or another number of features."""


@dataclass(frozen=True, slots=True)
class Decision:
    side: Side
    expected_micro: int  # what the model expects the side to make after costs, rounded down
    model: str  # the hash of the model that said so


class Scorer(Protocol):
    @property
    def model(self) -> Model: ...

    def score(self, row: FeatureRow) -> Decision | None:
        """The side the model would take on this row and what it expects of it, or None to abstain."""
        ...


class _Linear:
    """One target's prediction as whole numbers: (constant + sum of weight x value) / divisor."""

    __slots__ = ("constant", "divisor", "weights")

    def __init__(self, intercept: Fraction, weights: tuple[Fraction, ...]) -> None:
        self.divisor = math.lcm(intercept.denominator, *(w.denominator for w in weights))
        self.constant = int(intercept * self.divisor)
        self.weights = tuple(int(w * self.divisor) for w in weights)

    def at(self, values: tuple[int, ...]) -> Fraction:
        return Fraction(self.constant + sum(w * x for w, x in zip(self.weights, values, strict=True)), self.divisor)


class _Checked:
    def __init__(self, model: Model, kind: str) -> None:
        if model.kind != kind:
            raise ModelError(f"a {model.kind} model was handed to the scorer of {kind} models")
        self._model, self._hash = model, model.content_hash

    @property
    def model(self) -> Model:
        return self._model

    def _check(self, row: FeatureRow) -> None:
        if row.pair != self._model.pair or len(row.values) != len(self._model.features):
            raise ScoreError(
                f"model {self._hash[:12]} reads {len(self._model.features)} features of {self._model.pair}, "
                f"not {len(row.values)} of {row.pair}"
            )


class RidgeScorer(_Checked):
    def __init__(self, model: Model) -> None:
        super().__init__(model, "ridge")
        fit = Ridge.from_params(model.params)
        if set(fit.weights) != {BUY, SELL} or any(len(w) != len(model.features) for w in fit.weights.values()):
            raise ModelError("a ridge model predicts the outcome of each side from every feature it names")
        self._buy = _Linear(fit.intercepts[BUY], fit.weights[BUY])
        self._sell = _Linear(fit.intercepts[SELL], fit.weights[SELL])

    def score(self, row: FeatureRow) -> Decision | None:
        self._check(row)
        buy, sell = self._buy.at(row.values), self._sell.at(row.values)
        if buy == sell or max(buy, sell) <= 0:
            return None
        return Decision(Side.BUY if buy > sell else Side.SELL, math.floor(max(buy, sell)), self._hash)


class TreeScorer(_Checked):
    """Boosted trees: per side, the sum of one leaf per tree. Whole numbers throughout."""

    def __init__(self, model: Model) -> None:
        super().__init__(model, GBM)
        try:
            fit = Trees.from_params(model.params)
        except TreesError as exc:
            raise ModelError(f"model {self._hash[:12]} does not hold boosted trees: {exc}") from exc
        if set(fit.trees) != {BUY, SELL} or fit.width != len(model.features):
            raise ModelError("a model of boosted trees predicts the outcome of each side from every feature it names")
        self._fit = fit

    def score(self, row: FeatureRow) -> Decision | None:
        self._check(row)
        try:
            buy, sell = self._fit.total(BUY, row.values), self._fit.total(SELL, row.values)
        except TreesError as exc:
            raise ScoreError(str(exc)) from exc
        if buy == sell or max(buy, sell) <= 0:
            return None
        return Decision(Side.BUY if buy > sell else Side.SELL, max(buy, sell) // self._fit.scale, self._hash)


class NetScorer(_Checked):
    """A sequence network on a window of rows: per side, one whole-number output. It abstains inside its rounding."""

    def __init__(self, model: Model) -> None:
        super().__init__(model, SEQ)
        try:
            fit = Net.from_params(model.params)
        except NetError as exc:
            raise ModelError(f"model {self._hash[:12]} does not hold a sequence network: {exc}") from exc
        if set(fit.out) != {BUY, SELL} or fit.window * fit.width != len(model.features):
            raise ModelError("a sequence network predicts the outcome of each side from every value of its window")
        self._fit, self._slack = fit, fit.slack()

    def score(self, row: FeatureRow) -> Decision | None:
        self._check(row)
        try:
            found = self._fit.totals(row.values)
        except NetError as exc:
            raise ScoreError(str(exc)) from exc
        buy, sell = found[BUY], found[SELL]
        best, slack = (buy, self._slack[BUY]) if buy > sell else (sell, self._slack[SELL])
        # The fitted network is within `slack` of each output. Unless the sides are further apart than both
        # slacks together, and the better one further above zero than its own, it might decide otherwise.
        if abs(buy - sell) <= self._slack[BUY] + self._slack[SELL] or best <= slack:
            return None
        expected = best * self._fit.target_scale >> (BITS + WEIGHT_BITS)
        return Decision(Side.BUY if buy > sell else Side.SELL, expected, self._hash)


class FlatScorer(_Checked):
    """The baseline that never trades."""

    def __init__(self, model: Model) -> None:
        super().__init__(model, "flat")

    def score(self, row: FeatureRow) -> Decision | None:
        self._check(row)
        return None


class RandomScorer(_Checked):
    """The baseline that takes a side by a seeded coin, and expects nothing of it."""

    def __init__(self, model: Model) -> None:
        super().__init__(model, "random")
        seed = model.params.get("seed")
        if type(seed) is not int:
            raise ModelError("a random model's one parameter is its seed, a whole number")
        self._seed = seed

    def score(self, row: FeatureRow) -> Decision | None:
        self._check(row)
        return Decision(Side.BUY if number(self._seed, row.n, row.known_ns) & 1 else Side.SELL, 0, self._hash)


def scorer_for(model: Model) -> Scorer:
    """The scorer of a model, by its kind."""
    made: Scorer
    if model.kind == "ridge":
        made = RidgeScorer(model)
    elif model.kind == GBM:
        made = TreeScorer(model)
    elif model.kind == SEQ:
        made = NetScorer(model)
    elif model.kind == "flat":
        made = FlatScorer(model)
    else:
        made = RandomScorer(model)
    return made


# ---- the same question and answer, as lines ------------------------------------------------------------------------


def ask(row: FeatureRow) -> str:
    """A row as a request line."""
    return canonical({"pair": row.pair, "n": row.n, "known_ns": row.known_ns, "values": list(row.values)})


def answer(scorer: Scorer, line: str) -> str:
    """The reply line to a request line: what a scorer in its own process would write back."""
    try:
        asked = json.loads(line)
        row = FeatureRow(asked["pair"], asked["n"], asked["known_ns"], tuple(asked["values"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoreError(f"not a request for a score: {exc!r}") from exc
    if not all(type(value) is int for value in (row.n, row.known_ns, *row.values)):
        raise ScoreError("a request holds whole numbers only")
    found = scorer.score(row)
    reply: dict[str, object] = {"model": scorer.model.content_hash, "side": None, "expected_micro": None}
    if found is not None:
        reply.update(side=found.side.value, expected_micro=found.expected_micro)
    return canonical(reply)


def heard(line: str) -> Decision | None:
    """A reply line as the decision it carries."""
    try:
        reply = json.loads(line)
        if reply["side"] is None:
            return None
        if type(reply["expected_micro"]) is not int or not isinstance(reply["model"], str):
            raise ValueError("a decision names its model and a whole number of millionths")
        return Decision(Side(reply["side"]), reply["expected_micro"], reply["model"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoreError(f"not a scorer's reply: {exc!r}") from exc
