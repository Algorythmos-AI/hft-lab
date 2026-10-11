"""Ridge regression on standardised features, solved exactly (ADR 0022).

The model predicts a target as a constant plus a weighted sum of the features, each feature first centred on
its mean and divided by its standard deviation over the training rows. The weights minimise the squared error
plus `penalty` times the sum of their squares. With `n` rows the standardised features each have a sum of
squares of `n`, so a penalty of `n` halves the weight of a feature that stands alone.

The numerical method is the normal equations in exact rational arithmetic. Nothing is iterated, nothing is
rounded and there is no tolerance:

  1. One pass over the rows accumulates the sums, the products of every pair of features, and the products of
     every feature with every target, as whole numbers of any size.
  2. From them, with `S` the sums and `G` the products: `C[j][k] = n G[j][k] - S[j] S[k]` (n squared times the
     covariance) and `c[j] = n (x_j . y) - S[j] (sum of y)`.
  3. The weight on a centred, unscaled feature, `v[j] = w[j] / sd[j]`, solves `(C + (penalty / n) diag(C)) v = c`.
     The standard deviation never has to be taken: standardising and then penalising `w` is the same as
     penalising `v[j]` by the feature's variance. The system is solved by Gaussian elimination on fractions.
  4. The prediction for a row `x` is `mean(y) + sum of v[j] (x[j] - mean[j])`: a fraction, exact for whole `x`.

The means and variances are those of the training rows only. They are kept with the model and applied
unchanged to whatever it scores later.

Limits. A feature that never varies in the training rows has no standard deviation: it gets a weight of zero
and takes no part. With a penalty above zero the system always has one solution; with a penalty of zero,
features that repeat one another make it singular, and that is refused. Step 1 costs rows times features
squared; step 3 costs features cubed operations on fractions whose size grows with the number of features. It
is meant for tens of features, not thousands. Exactness is not accuracy: the model is linear, and an exact
solution of the wrong model is exactly wrong.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction


class RidgeError(ValueError):
    """The rows cannot be fitted: too few, not whole numbers, ragged, or a system without one solution."""


@dataclass(frozen=True, slots=True)
class Ridge:
    penalty: Fraction
    rows: int
    means: tuple[Fraction, ...]
    variances: tuple[Fraction, ...]
    intercepts: Mapping[str, Fraction]  # per target: the prediction for a row of zeros
    weights: Mapping[str, tuple[Fraction, ...]]  # per target: one weight per feature, as it is, unscaled

    def predict(self, target: str, values: Sequence[int]) -> Fraction:
        weights = self.weights[target]
        if len(values) != len(weights):
            raise RidgeError(f"the model reads {len(weights)} features and was given {len(values)}")
        return self.intercepts[target] + sum((w * x for w, x in zip(weights, values, strict=True)), Fraction(0))

    def params(self) -> dict[str, object]:
        """The fit as a model file keeps it: every fraction as text."""
        return {
            "penalty": str(self.penalty),
            "rows": self.rows,
            "means": [str(mean) for mean in self.means],
            "variances": [str(variance) for variance in self.variances],
            "targets": {
                name: {"intercept": str(self.intercepts[name]), "weights": [str(w) for w in self.weights[name]]}
                for name in sorted(self.weights)
            },
        }

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> Ridge:
        try:
            targets = params["targets"]
            means, variances = params["means"], params["variances"]
            assert isinstance(targets, dict) and isinstance(means, list) and isinstance(variances, list)
            rows = params["rows"]
            assert type(rows) is int
            return cls(
                Fraction(str(params["penalty"])),
                rows,
                tuple(Fraction(str(mean)) for mean in means),
                tuple(Fraction(str(variance)) for variance in variances),
                {name: Fraction(str(target["intercept"])) for name, target in targets.items()},
                {name: tuple(Fraction(str(w)) for w in target["weights"]) for name, target in targets.items()},
            )
        except (AssertionError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise RidgeError(f"these are not the parameters of a ridge model: {exc!r}") from exc


def solve(matrix: list[list[Fraction]], sides: list[list[Fraction]]) -> list[list[Fraction]]:
    """The solutions of `matrix . v = side` for each side, by Gaussian elimination. Exact, or `RidgeError`."""
    size = len(matrix)
    rows = [[*matrix[i], *(side[i] for side in sides)] for i in range(size)]
    for column in range(size):
        pivot = next((r for r in range(column, size) if rows[r][column] != 0), None)
        if pivot is None:
            raise RidgeError("the features repeat one another and there is no penalty: no single solution")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        lead = rows[column][column]
        rows[column] = [value / lead for value in rows[column]]
        for r in range(size):
            if r != column and rows[r][column] != 0:
                factor = rows[r][column]
                rows[r] = [value - factor * top for value, top in zip(rows[r], rows[column], strict=True)]
    return [[rows[i][size + s] for i in range(size)] for s in range(len(sides))]


def fit(rows: Sequence[Sequence[int]], targets: Mapping[str, Sequence[int]], penalty: Fraction) -> Ridge:
    """Fit every target to the same rows. The rows and the targets are whole numbers; the fit is exact."""
    count = len(rows)
    width = len(rows[0]) if rows else 0
    if count < 2 or width < 1 or not targets or penalty < 0:
        raise RidgeError("a fit needs two rows, one feature, one target and a penalty that is not negative")
    if any(len(target) != count for target in targets.values()):
        raise RidgeError("every target has one value per row")
    names = sorted(targets)
    sums = [0] * width
    products = [[0] * width for _ in range(width)]
    with_target = {name: [0] * width for name in names}
    for position, row in enumerate(rows):
        if len(row) != width or any(type(value) is not int for value in row):
            raise RidgeError(f"row {position} is not {width} whole numbers")
        for j, value in enumerate(row):
            sums[j] += value
            line = products[j]
            for k in range(j, width):
                line[k] += value * row[k]
        for name in names:
            y = targets[name][position]
            if type(y) is not int:
                raise RidgeError(f"target {name} of row {position} is not a whole number")
            line = with_target[name]
            for j, value in enumerate(row):
                line[j] += value * y
    totals = {name: sum(targets[name]) for name in names}

    def spread(j: int, k: int) -> int:
        return count * products[min(j, k)][max(j, k)] - sums[j] * sums[k]

    varying = [j for j in range(width) if spread(j, j) > 0]
    matrix = [
        [Fraction(spread(j, k)) + (penalty * spread(j, j) / count if j == k else 0) for k in varying] for j in varying
    ]
    sides = [[Fraction(count * with_target[name][j] - sums[j] * totals[name]) for j in varying] for name in names]
    solved = solve(matrix, sides) if varying else [[] for _ in names]
    means = tuple(Fraction(total, count) for total in sums)
    weights, intercepts = {}, {}
    for name, found in zip(names, solved, strict=True):
        full = [Fraction(0)] * width
        for j, weight in zip(varying, found, strict=True):
            full[j] = weight
        weights[name] = tuple(full)
        intercepts[name] = Fraction(totals[name], count) - sum((w * m for w, m in zip(full, means, strict=True)), Fraction(0))
    variances = tuple(Fraction(spread(j, j), count * count) for j in range(width))
    return Ridge(penalty, count, means, variances, intercepts, weights)
