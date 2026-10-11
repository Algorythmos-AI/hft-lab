"""A small sequence network: fitted here, in floats, and kept as whole numbers (ADR 0029).

This is the one module of the platform that fits a network, and the one beside `hftlab.learn.boost` that
imports the array library. It does so inside the functions that fit, so that importing this module costs
nothing and needs nothing. No new dependency came with it: the arrays are numpy's, which the base lock holds
already, and the network, its gradients and its optimiser are written out below.

    fit(rows, targets, settings) -> Net        `grow`, in floats, and then `keep`

`rows` are windows: the last `settings.window` feature rows of one pair, side by side, oldest first, as
`hftlab.features.window` makes them. `targets` are the outcome of each side, in millionths.

  * Each feature is centred and scaled by the whole-number mean and standard deviation of the training rows,
    worked out exactly, and the scaled value is cut off and rounded down to a whole number of `1 / 2**BITS` by
    the function the scorer uses (`hftlab.models.net.read`). That whole number over `2**BITS` is the input of
    the float network: a float holds it exactly, so the fitted network and the scored one read the same thing.
  * The network is one 1-D convolution over the rows, one hidden layer and one output per target, with
    `min(max(x, 0), cap)` between them. It is fitted to the targets over one whole number (`target_scale`)
    by Adam on the squared error, for a fixed number of passes over batches taken in a fixed order, with a
    step that shrinks evenly to nothing over the run.
  * Then the floats stop. A weight `w` becomes the whole number nearest `w * 2**WEIGHT_BITS`, a bias the one
    nearest `b * 2**(BITS + WEIGHT_BITS)`. Those roundings are the only difference between what was fitted and
    what is scored, and `hftlab.models.net` bounds what they can do to an output. Before the network is
    returned a sample of the training rows is put through both, and the fit is refused unless they agree to
    within that bound.

**The same inputs give the same file, on any machine with IEEE 754 doubles.** Nothing here is drawn from a
generator: the starting weights are a hash of the seed and of each weight's place. Every float operation is an
addition, a subtraction, a multiplication, a division, a square root or a comparison of doubles, element by
element, and each of those has one correctly rounded answer. No sum is left to a library to order: a sum over
a batch or over a unit's inputs is taken pairwise by `_total`, in an order written there. No routine of a
linear-algebra library is called, so there is no thread count to set and no build of such a library to differ.
A test holds the same hash across two fits and across a process started from nothing; what holds it across
machines is this paragraph and a scan of this file, and the first run on another processor is the check.

**Memory.** `needs` is an estimate of what a fit holds beyond the rows it is handed, and a run whose estimate
does not fit under its budget is refused before it starts (`hftlab.learn.train`).

Limits. A network finds an order of rows that neither a straight line nor trees on single rows can see. It
also fits noise, more readily than either. It is small and fixed for that reason, nothing searches its
settings, and it is judged like any other model: walk-forward, beside the oracle and a coin, every trial counted.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any, Final

from hftlab.learn.config import Network
from hftlab.models.baseline import number
from hftlab.models.net import BITS, MOST_SPREAD, WEIGHT_BITS, Net, NetError, read
from hftlab.models.scorer import BUY, SELL

LIBRARY: Final = "numpy"
EXACT: Final = 2**53  # a float holds every whole number below this exactly
CHECKED_ROWS: Final = 512  # training rows put through both networks before a fit is kept
PPM: Final = 1_000_000
WEIGHT_MOST: Final = 4.0  # no weight or bias is let beyond this: it bounds what a whole-number sum can reach
BETA_1, BETA_2, EPSILON = 0.9, 0.999, 1e-8  # Adam's own constants
DRAW: Final = 2**53


class SequenceError(ValueError):
    """The rows cannot be fitted, or the fitted network cannot be written as whole numbers."""


class TooFewRows(SequenceError):
    """There are not two rows to fit. Nothing is wrong with the rows there are."""


class LibraryMissing(Exception):
    """The array library cannot be imported. It is in the base lock; only fitting a network here needs it."""


def library() -> Any:
    """The array library, imported now."""
    try:
        import numpy
    except ImportError as exc:
        raise LibraryMissing(
            f"a seq model is trained with {LIBRARY}, which the base requirements hold and this environment "
            f"does not: run `make bootstrap` ({exc})"
        ) from exc
    return numpy


def fitted_with() -> str:
    """What fitted a network and its version, as a model file names where its weights came from."""
    return f"{LIBRARY} {library().__version__}"


def needs(count: int, width: int, settings: Network) -> int:
    """An estimate, in bytes, of what a fit of `count` windows of `width` values holds beyond the rows themselves.

    The inputs and the targets as doubles, a few copies of one batch's activations, and four copies of the
    weights (themselves, their gradient and Adam's two averages). It is an estimate for a refusal, not a
    measurement: the peak a run reached is measured afterwards (`hftlab.learn.train`).
    """
    places = settings.window - settings.kernel + 1
    batch = min(count, settings.batch_rows)
    held = 8 * count * (width + 2)
    per_row = width // settings.window
    widest = max(places * settings.filters * settings.kernel * per_row, settings.hidden * places * settings.filters)
    one_batch = 8 * batch * (2 * width + 4 * widest + 8 * places * settings.filters + 8 * settings.hidden)
    weights = settings.filters * (settings.kernel * per_row + 1) + settings.hidden * (places * settings.filters + 1)
    return held + one_batch + 4 * 8 * (weights + 2 * (settings.hidden + 1))


# ---- whole numbers in: the scale of every feature and of the targets ---------------------------------------------


def scales(rows: Sequence[Sequence[int]], per_row: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Per feature, over every row of every window: the mean to the nearest whole number and the standard deviation
    rounded down, at least 1. Exact: sums of whole numbers and one integer square root."""
    count = [0] * per_row
    total = [0] * per_row
    squares = [0] * per_row
    for row in rows:
        for position, value in enumerate(row):
            place = position % per_row
            count[place] += 1
            total[place] += value
            squares[place] += value * value
    centre = tuple((2 * t + n) // (2 * n) for t, n in zip(total, count, strict=True))
    spread = tuple(
        max(1, math.isqrt((n * s - t * t) // (n * n))) for t, s, n in zip(total, squares, count, strict=True)
    )
    return centre, spread


def target_scale(targets: Mapping[str, Sequence[int]]) -> int:
    """The root of the mean square of every target, rounded down, at least 1: what an output of 1 stands for."""
    count = sum(len(values) for values in targets.values())
    return max(1, math.isqrt(sum(value * value for values in targets.values() for value in values) // count))


# ---- floats: the network, its gradients, and Adam ----------------------------------------------------------------


def _total(values: Any) -> Any:
    """The sum along the first axis, taken pairwise: neighbours added, then neighbours of those, until one is left.

    The order is written here and nowhere else, and every step of it is an addition element by element, so
    there is no room for a library to choose another.
    """
    numpy = library()
    while len(values) > 1:
        even = len(values) - len(values) % 2
        paired = values[0:even:2] + values[1:even:2]
        values = paired if even == len(values) else numpy.concatenate([paired, values[even:]])
    return values[0]


def start(settings: Network, per_row: int) -> dict[str, Any]:
    """The weights before the first step: uniform within a bound set by the inputs of a unit, by a hash of the seed."""
    numpy = library()
    places = settings.window - settings.kernel + 1
    shapes = {
        "conv": (settings.filters, settings.kernel * per_row), "hidden": (settings.hidden, places * settings.filters),
        "out": (2, settings.hidden),
    }  # fmt: skip
    made: dict[str, Any] = {}
    for layer, name in enumerate(sorted(shapes)):
        units, fan_in = shapes[name]
        bound = math.sqrt(6.0 / fan_in)
        drawn = [
            (2.0 * (number(settings.seed, layer, place) % DRAW) / DRAW - 1.0) * bound for place in range(units * fan_in)
        ]
        made[name] = numpy.array(drawn, dtype=numpy.float64).reshape(units, fan_in)
        made[name + "_bias"] = numpy.zeros(units, dtype=numpy.float64)
    return made


def _layer(weights: Any, bias: Any, inputs: Any) -> Any:
    """The sum over a unit's inputs of weight x input, and then the bias: (rows, inputs) -> (rows, units)."""
    return _total(inputs.T[:, :, None] * weights.T[:, None, :]) + bias


def _under(inputs: Any, settings: Network, per_row: int) -> Any:
    """Windows as what each place of the kernel reads: (rows, window x per_row) -> (rows x places, kernel x per_row)."""
    numpy = library()
    places, span = settings.window - settings.kernel + 1, settings.kernel * per_row
    cut = [inputs[:, place * per_row : place * per_row + span] for place in range(places)]
    return numpy.stack(cut, axis=1).reshape(inputs.shape[0] * places, span)


def forward(weights: Mapping[str, Any], inputs: Any, settings: Network, per_row: int) -> tuple[Any, ...]:
    """The outputs for a batch of windows, with everything the gradients need on the way: floats."""
    numpy = library()
    cap = float(settings.cap)
    under = _under(inputs, settings, per_row)
    before_1 = _layer(weights["conv"], weights["conv_bias"], under)
    after_1 = numpy.minimum(numpy.maximum(before_1, 0.0), cap).reshape(inputs.shape[0], -1)  # place first
    before_2 = _layer(weights["hidden"], weights["hidden_bias"], after_1)
    after_2 = numpy.minimum(numpy.maximum(before_2, 0.0), cap)
    return under, before_1, after_1, before_2, after_2, _layer(weights["out"], weights["out_bias"], after_2)


def _back(weights: Any, error: Any) -> Any:
    """An error at a layer's outputs as the error at its inputs: (rows, units) -> (rows, inputs)."""
    return _total(error.T[:, :, None] * weights[:, None, :])


def _slopes(error: Any, inputs: Any) -> Any:
    """The gradient of a layer's weights: per unit and input, the sum over the rows of error x input."""
    return _total(error[:, :, None] * inputs[:, None, :])


def loss(weights: Mapping[str, Any], inputs: Any, wanted: Any, settings: Network, per_row: int) -> Any:
    """Half the squared error per row, summed over the outputs and averaged over the rows, plus the weight penalty."""
    apart = forward(weights, inputs, settings, per_row)[-1] - wanted
    penalty = sum(_total(_total(weights[name] * weights[name])) for name in ("conv", "hidden", "out"))
    return _total(_total(apart * apart)) / (2 * len(inputs)) + settings.l2_ppm / PPM / 2 * penalty


def gradients(weights: Mapping[str, Any], inputs: Any, wanted: Any, settings: Network, per_row: int) -> dict[str, Any]:
    """The gradient of `loss` in every weight and bias, by the chain rule written out. Checked by differences."""
    cap, decay = float(settings.cap), settings.l2_ppm / PPM
    under, before_1, after_1, before_2, after_2, out = forward(weights, inputs, settings, per_row)
    error_3 = (out - wanted) / len(inputs)
    error_2 = _back(weights["out"], error_3) * ((before_2 > 0.0) & (before_2 < cap))
    error_1 = _back(weights["hidden"], error_2).reshape(before_1.shape) * ((before_1 > 0.0) & (before_1 < cap))
    return {
        "out": _slopes(error_3, after_2) + decay * weights["out"], "out_bias": _total(error_3),
        "hidden": _slopes(error_2, after_1) + decay * weights["hidden"], "hidden_bias": _total(error_2),
        "conv": _slopes(error_1, under) + decay * weights["conv"], "conv_bias": _total(error_1),
    }  # fmt: skip


def _checked(rows: Sequence[Sequence[int]], targets: Mapping[str, Sequence[int]], settings: Network) -> int:
    """The values in one row of a window, after refusing what cannot be fitted."""
    count = len(rows)
    width = len(rows[0]) if rows else 0
    if count < 2:
        raise TooFewRows("a fit needs two rows")
    if width < settings.window or width % settings.window or sorted(targets) != sorted((BUY, SELL)):
        raise SequenceError(f"a fit needs windows of {settings.window} whole rows and the outcome of each side")
    for position, row in enumerate(rows):
        if len(row) != width or set(map(type, row)) != {int} or max(row) >= EXACT or min(row) <= -EXACT:
            raise SequenceError(f"row {position} is not {width} whole numbers a float holds exactly")
    for name, values in targets.items():
        if len(values) != count or set(map(type, values)) != {int} or max(values) >= EXACT or min(values) <= -EXACT:
            raise SequenceError(f"target {name} is not one whole number per row that a float holds exactly")
    return width // settings.window


def grow(
    rows: Sequence[Sequence[int]], targets: Mapping[str, Sequence[int]], settings: Network
) -> tuple[dict[str, Any], tuple[int, ...], tuple[int, ...], int]:
    """The fitted float weights, with the centres, the spreads and the target scale they were fitted under."""
    numpy = library()
    per_row = _checked(rows, targets, settings)
    centre, spread = scales(rows, per_row)
    if max(spread) >= MOST_SPREAD:
        raise SequenceError("a feature varies by more than a network of this kind can be scored on")
    scale = target_scale(targets)
    try:
        inputs = numpy.array([read(row, centre, spread, settings.clip) for row in rows], dtype=numpy.float64) / 2**BITS
    except NetError as exc:
        raise SequenceError(str(exc)) from exc
    wanted = numpy.array([targets[BUY], targets[SELL]], dtype=numpy.float64).T / float(scale)
    weights = start(settings, per_row)
    mean = {name: numpy.zeros_like(value) for name, value in weights.items()}
    square = {name: numpy.zeros_like(value) for name, value in weights.items()}
    batches = -(-len(rows) // settings.batch_rows)
    decay_1 = decay_2 = 1.0
    steps = settings.epochs * batches
    for epoch in range(settings.epochs):
        for batch in range(batches):  # every row once a pass, in the same batches every pass: nothing is drawn
            slopes = gradients(weights, inputs[batch::batches], wanted[batch::batches], settings, per_row)
            decay_1, decay_2 = decay_1 * BETA_1, decay_2 * BETA_2
            rate = settings.learning_rate_ppm / PPM * (steps - epoch * batches - batch) / steps  # down to nothing
            for name in sorted(weights):
                mean[name] = BETA_1 * mean[name] + (1.0 - BETA_1) * slopes[name]
                square[name] = BETA_2 * square[name] + (1.0 - BETA_2) * slopes[name] * slopes[name]
                step = rate * (mean[name] / (1.0 - decay_1)) / (numpy.sqrt(square[name] / (1.0 - decay_2)) + EPSILON)
                weights[name] = numpy.minimum(numpy.maximum(weights[name] - step, -WEIGHT_MOST), WEIGHT_MOST)
    return weights, centre, spread, scale


# ---- whole numbers out ----------------------------------------------------------------------------------------------


def _kept(values: Any, bits: int) -> tuple[int, ...]:
    """Floats as the whole numbers nearest them at a scale of 2**bits: exact, then rounded once."""
    out = []
    for value in values.tolist():
        if not math.isfinite(value):
            raise SequenceError("the fit did not end in numbers: a weight is not finite")
        out.append(round(Fraction(value) * 2**bits))
    return tuple(out)


def keep(
    grown: tuple[dict[str, Any], tuple[int, ...], tuple[int, ...], int], rows: Sequence[Sequence[int]],
    settings: Network,
) -> Net:  # fmt: skip
    """The fitted network as whole numbers, checked against the floats on a sample of the rows it was fitted to."""
    numpy = library()
    weights, centre, spread, scale = grown
    both = BITS + WEIGHT_BITS
    out, out_bias = _kept(weights["out"].reshape(-1), WEIGHT_BITS), _kept(weights["out_bias"], both)
    hidden = settings.hidden
    try:
        net = Net(
            settings.window, len(centre), settings.kernel, settings.clip, settings.cap, centre, spread,
            tuple(_kept(row, WEIGHT_BITS) for row in weights["conv"]), _kept(weights["conv_bias"], both),
            tuple(_kept(row, WEIGHT_BITS) for row in weights["hidden"]), _kept(weights["hidden_bias"], both),
            {BUY: out[:hidden], SELL: out[hidden:]}, {BUY: out_bias[0], SELL: out_bias[1]}, scale, len(rows),
            settings.as_dict(), fitted_with(),
        )  # fmt: skip
    except NetError as exc:
        raise SequenceError(f"the fitted network cannot be kept: {exc}") from exc
    count, slack = len(rows), net.slack()
    for position in sorted({place * count // CHECKED_ROWS for place in range(CHECKED_ROWS)}):
        inputs = numpy.array([net.inputs(rows[position])], dtype=numpy.float64) / 2**BITS
        theirs = forward(weights, inputs, settings, len(centre))[-1][0].tolist()
        ours = net.totals(rows[position])
        for name, value in zip((BUY, SELL), theirs, strict=True):
            if abs(Fraction(ours[name]) - Fraction(value) * 2**both) > slack[name] + 1:
                raise SequenceError(f"the whole-number network and the fitted one part ways on training row {position}")
    return net


def fit(rows: Sequence[Sequence[int]], targets: Mapping[str, Sequence[int]], settings: Network) -> Net:
    """Fit the outcome of each side to the same windows, and hand back the network as whole numbers."""
    return keep(grow(rows, targets, settings), rows, settings)
