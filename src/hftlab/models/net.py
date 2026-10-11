"""A small sequence network kept as whole numbers, and the fixed-point arithmetic that scores it (ADR 0029).

A model of this kind reads a window: the last `window` feature rows of one pair, `width` values each, oldest
first. It is one 1-D convolution over the rows, one hidden layer and one output per target:

    input     each value, less its feature's centre, over its feature's spread, cut off at `clip` spreads
    conv      `filters` filters of `kernel` rows, at every place the kernel fits: (window - kernel + 1) x filters
    hidden    every output of the convolution into each of `hidden` units
    out       one number per target: the outcome of taking that side, in units of `target_scale` millionths

and between the layers the one activation is `min(max(x, 0), cap)`: nothing below zero, nothing above `cap`.

Everything here is a whole number. An activation `a` is kept as the integer nearest `a * 2**BITS`, a weight
`w` as the integer nearest `w * 2**WEIGHT_BITS`, a bias at the product of the two. Scoring is:

    input     t = min(max(x - centre, -clip * spread), clip * spread);  q = (t * 2**BITS) // spread
    a layer   z = bias + sum(weight * q);  q' = min(max((z + 2**(WEIGHT_BITS - 1)) >> WEIGHT_BITS, 0), cap * 2**BITS)
    out       z = bias + sum(weight * q), kept whole: z / 2**(BITS + WEIGHT_BITS) is the output

Additions, multiplications, comparisons and shifts of integers: the same row gives the same numbers on any
machine. The network was fitted elsewhere, in floats (`hftlab.learn.sequence`); what is kept is that fit with
every weight rounded once. Nothing here imports what fitted it, and nothing here can train.

**How far the whole numbers are from the network that was fitted** is worked out here, from the whole numbers
alone (`Net.slack`). The fitted network takes the very same input `q / 2**BITS`, so the input adds nothing.
Write `e` for how far a unit's integer can be from `2**BITS` times its real activation, and `m` for the most
its integer can be. A kept weight is within a half of the real one at its scale, and so is a kept bias, so
before the shift a unit is off by at most

    D = sum(|weight| * e) + (sum(m + e) + 1) / 2

and after it by `e' = D / 2**WEIGHT_BITS + 1/2`: the shift rounds to the nearest, and the activation moves
nothing further apart. For an input `e = 0` and `m = clip * 2**BITS`. `slack` is `D` of an output, rounded up.
That bound holds for every row, whatever its values; a scorer abstains wherever it could change the decision.

**Nothing overflows.** Python's integers cannot, but the arithmetic is meant to be portable, so `Net` refuses
parameters with which any intermediate value could reach `2**62` for any row at all, and a row with a value
at or beyond `2**62` is refused before anything is computed (`LIMIT`). No feature of the platform comes near:
the largest are times in nanoseconds within one trading week, below `2**50`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from operator import mul
from typing import Final

KIND: Final = "seq"
BITS: Final = 16  # an activation is a whole number of 1 / 2**16
WEIGHT_BITS: Final = 24  # a weight is a whole number of 1 / 2**24
LIMIT: Final = 2**62  # no value of a row and no intermediate value reaches this
MOST_SPREAD: Final = 2**40
MOST_WINDOW: Final = 64
MOST_WIDTH: Final = 64
MOST_FILTERS: Final = 32
MOST_HIDDEN: Final = 64
MOST_PARAMETERS: Final = 20_000
_SHAPE: Final = frozenset(
    {
        "bits", "weight_bits", "window", "width", "kernel", "clip", "cap", "centre", "spread", "conv", "conv_bias",
        "hidden", "hidden_bias", "out", "out_bias", "target_scale", "rows", "settings", "fitted_with",
    }
)  # fmt: skip


_SIZES: Final = ("window", "width", "kernel", "clip", "cap", "target_scale", "rows")


class NetError(ValueError):
    """This is not a network this module can score: a shape that does not fit, a number that is not whole."""


def _whole(values: object, what: str) -> tuple[int, ...]:
    if not isinstance(values, list | tuple) or any(type(value) is not int for value in values):
        raise NetError(f"a network's {what} is a list of whole numbers")
    return tuple(values)


def _grid(values: object, what: str) -> tuple[tuple[int, ...], ...]:
    if not isinstance(values, list | tuple):
        raise NetError(f"a network's {what} is a list of lists of whole numbers")
    return tuple(_whole(row, what) for row in values)


def read(values: Sequence[int], centre: Sequence[int], spread: Sequence[int], clip: int) -> list[int]:
    """Rows of values as the whole numbers a network reads: (value - centre) / spread, cut off, times 2**BITS.

    The fit calls this too, so the network that is fitted and the one that is scored read the same numbers.
    """
    rows, rest = divmod(len(values), len(centre))
    if rest or not values or set(map(type, values)) != {int} or max(values) >= LIMIT or min(values) <= -LIMIT:
        raise NetError("a window is whole rows of whole numbers, none beyond anything a feature of the platform can be")
    edges = [clip * one for one in spread]
    each = zip(values, list(centre) * rows, edges * rows, list(spread) * rows, strict=True)
    return [(min(max(value - middle, -edge), edge) << BITS) // one for value, middle, edge, one in each]


def _up(value: Fraction) -> int:
    """The smallest whole number that is not below `value`."""
    return -((-value.numerator) // value.denominator)


@dataclass(frozen=True, slots=True)
class Net:
    window: int  # rows in a window
    width: int  # values in a row
    kernel: int  # rows under one filter
    clip: int  # an input is cut off at this many spreads from its centre
    cap: int  # an activation is at most this
    centre: tuple[int, ...]  # per feature, on the feature's own scale
    spread: tuple[int, ...]  # per feature, at least 1
    conv: tuple[tuple[int, ...], ...]  # per filter: kernel x width weights, oldest row first
    conv_bias: tuple[int, ...]
    hidden: tuple[tuple[int, ...], ...]  # per unit: one weight per place of the kernel and filter, place first
    hidden_bias: tuple[int, ...]
    out: Mapping[str, tuple[int, ...]]  # per target: one weight per hidden unit
    out_bias: Mapping[str, int]
    target_scale: int  # an output of 1 is this many millionths of the quote currency
    rows: int  # the windows it was fitted to
    settings: Mapping[str, int]  # the numbers it was fitted with, as the learning settings wrote them
    fitted_with: str  # what fitted it and its version: where the weights came from, never what scores them

    def __post_init__(self) -> None:
        sizes = (self.window, self.width, self.kernel, self.clip, self.cap, self.target_scale, self.rows)
        if any(type(size) is not int or size < 1 for size in sizes):
            raise NetError("the sizes of a network, its clip, its cap, its target scale and its rows are whole and above 0")
        if self.window > MOST_WINDOW or self.width > MOST_WIDTH or not self.kernel <= self.window:
            raise NetError(f"a window is at most {MOST_WINDOW} rows of at most {MOST_WIDTH} values, and holds the kernel")
        if not 1 <= len(self.conv) <= MOST_FILTERS or not 1 <= len(self.hidden) <= MOST_HIDDEN:
            raise NetError(f"a network has 1 to {MOST_FILTERS} filters and 1 to {MOST_HIDDEN} hidden units")
        if len(self.centre) != self.width or len(self.spread) != self.width:
            raise NetError("a network has a centre and a spread for every value of a row")
        if any(not 1 <= spread < MOST_SPREAD for spread in self.spread) or any(abs(c) >= LIMIT for c in self.centre):
            raise NetError("a spread is between 1 and 2**40, and a centre is within what a row may hold")
        if any(len(one) != self.kernel * self.width for one in self.conv) or len(self.conv_bias) != len(self.conv):
            raise NetError("a filter has a weight for every value under its kernel, and one bias")
        if any(len(one) != self.places * len(self.conv) for one in self.hidden) or len(self.hidden_bias) != len(self.hidden):
            raise NetError("a hidden unit has a weight for every output of the convolution, and one bias")
        if not self.out or set(self.out) != set(self.out_bias):
            raise NetError("a network has an output, with one bias, for every target")
        if any(len(one) != len(self.hidden) for one in self.out.values()):
            raise NetError("an output has a weight for every hidden unit")
        if any(type(bias) is not int for bias in self.out_bias.values()):
            raise NetError("an output's bias is a whole number")
        if self.parameters > MOST_PARAMETERS:
            raise NetError(f"a network has at most {MOST_PARAMETERS} parameters, not {self.parameters}")
        if self.reach() >= LIMIT:
            raise NetError("with these weights an intermediate value could reach 2**62")

    @property
    def places(self) -> int:
        """Where the kernel fits in a window."""
        return self.window - self.kernel + 1

    @property
    def parameters(self) -> int:
        counts = [len(one) + 1 for one in (*self.conv, *self.hidden, *self.out.values())]
        return sum(counts)

    # ---- what the whole numbers can reach, and how far they can be from the fitted network -------------------------

    def _walk(self) -> tuple[int, dict[str, Fraction]]:
        """The most any intermediate value can be, for any row; and `D` of each output (see the module's text)."""
        top, half, one = self.cap << BITS, 1 << (WEIGHT_BITS - 1), Fraction(1)
        most = [self.clip << BITS] * (self.kernel * self.width)  # m of the inputs under a kernel
        off = [Fraction(0)] * len(most)  # e of them
        reach = max(self.clip * spread << BITS for spread in self.spread)  # the input's own product
        # The convolution's outputs are read place first: the same filters, and so the same bounds, at every place.
        layers = ((self.conv, self.conv_bias, self.places), (self.hidden, self.hidden_bias, 1))
        for weights, biases, repeat in layers:
            after_most, after_off = [], []
            for row, bias in zip(weights, biases, strict=True):
                size = sum(abs(w) * m for w, m in zip(row, most, strict=True)) + abs(bias) + half
                reach = max(reach, size)
                apart = sum(abs(w) * e for w, e in zip(row, off, strict=True)) + (sum(most) + sum(off) + one) / 2
                after_most.append(min(top, size >> WEIGHT_BITS))
                after_off.append(apart / (1 << WEIGHT_BITS) + one / 2)
            most, off = after_most * repeat, after_off * repeat
        apart_by = {}
        for name in sorted(self.out):
            row = self.out[name]
            reach = max(reach, sum(abs(w) * m for w, m in zip(row, most, strict=True)) + abs(self.out_bias[name]))
            apart_by[name] = sum(abs(w) * e for w, e in zip(row, off, strict=True)) + (sum(most) + sum(off) + one) / 2
        return reach, apart_by

    def reach(self) -> int:
        """The most the size of any intermediate value can be, whatever the row."""
        return self._walk()[0]

    def slack(self) -> dict[str, int]:
        """Per target: the most its whole-number output can differ from the fitted network's, at the output's scale."""
        return {name: _up(apart) for name, apart in self._walk()[1].items()}

    # ---- scoring ---------------------------------------------------------------------------------------------------

    def inputs(self, values: Sequence[int]) -> list[int]:
        """A window as the whole numbers the first layer reads: each value centred, scaled and cut off."""
        if len(values) != self.window * self.width:
            raise NetError(f"the model reads {self.window} rows of {self.width} whole numbers")
        return read(values, self.centre, self.spread, self.clip)

    def totals(self, values: Sequence[int]) -> dict[str, int]:
        """Per target, the output for this window as a whole number of 1 / 2**(BITS + WEIGHT_BITS)."""
        first = self.inputs(values)
        top, half, span = self.cap << BITS, 1 << (WEIGHT_BITS - 1), self.kernel * self.width
        found: list[int] = []
        for place in range(self.places):
            under = first[place * self.width : place * self.width + span]
            for row, bias in zip(self.conv, self.conv_bias, strict=True):
                found.append(min(max((bias + sum(map(mul, row, under)) + half) >> WEIGHT_BITS, 0), top))
        units = [
            min(max((bias + sum(map(mul, row, found)) + half) >> WEIGHT_BITS, 0), top)
            for row, bias in zip(self.hidden, self.hidden_bias, strict=True)
        ]
        return {name: self.out_bias[name] + sum(map(mul, self.out[name], units)) for name in sorted(self.out)}

    def peak(self, values: Sequence[int]) -> int:
        """The largest size of anything computed on the way to `totals`: every product and every running sum."""
        first = self.inputs(values)
        top, half, span = self.cap << BITS, 1 << (WEIGHT_BITS - 1), self.kernel * self.width
        seen = 0
        for position, value in enumerate(values):
            apart = value - self.centre[position % self.width]
            edge = self.clip * self.spread[position % self.width]
            seen = max(seen, abs(apart), abs(min(max(apart, -edge), edge) << BITS))

        def run(bias: int, row: Sequence[int], under: Sequence[int]) -> int:
            nonlocal seen
            total = bias
            for weight, value in zip(row, under, strict=True):
                total += weight * value
                seen = max(seen, abs(weight * value), abs(total))
            return total

        found = []
        for place in range(self.places):
            under = first[place * self.width : place * self.width + span]
            for row, bias in zip(self.conv, self.conv_bias, strict=True):
                total = run(bias, row, under) + half
                seen = max(seen, abs(total))
                found.append(min(max(total >> WEIGHT_BITS, 0), top))
        units = []
        for row, bias in zip(self.hidden, self.hidden_bias, strict=True):
            total = run(bias, row, found) + half
            seen = max(seen, abs(total))
            units.append(min(max(total >> WEIGHT_BITS, 0), top))
        for name in sorted(self.out):
            run(self.out_bias[name], self.out[name], units)
        return seen

    def predict(self, target: str, values: Sequence[int]) -> Fraction:
        """What the target is expected to be for this window, in millionths: the whole-number output, exactly."""
        return Fraction(self.totals(values)[target] * self.target_scale, 1 << (BITS + WEIGHT_BITS))

    # ---- as a model file keeps it ----------------------------------------------------------------------------------

    def params(self) -> dict[str, object]:
        """The network as a model file keeps it: whole numbers and text."""
        return {
            "bits": BITS,
            "weight_bits": WEIGHT_BITS,
            "window": self.window,
            "width": self.width,
            "kernel": self.kernel,
            "clip": self.clip,
            "cap": self.cap,
            "centre": list(self.centre),
            "spread": list(self.spread),
            "conv": [list(row) for row in self.conv],
            "conv_bias": list(self.conv_bias),
            "hidden": [list(row) for row in self.hidden],
            "hidden_bias": list(self.hidden_bias),
            "out": {name: list(self.out[name]) for name in sorted(self.out)},
            "out_bias": {name: self.out_bias[name] for name in sorted(self.out_bias)},
            "target_scale": self.target_scale,
            "rows": self.rows,
            "settings": {name: self.settings[name] for name in sorted(self.settings)},
            "fitted_with": self.fitted_with,
        }

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> Net:
        if set(params) != _SHAPE:
            raise NetError("these are not the parameters of a sequence network")
        if params["bits"] != BITS or params["weight_bits"] != WEIGHT_BITS:
            raise NetError(f"this module scores activations of {BITS} bits and weights of {WEIGHT_BITS}")
        out, out_bias, settings = params["out"], params["out_bias"], params["settings"]
        if not isinstance(out, dict) or not isinstance(out_bias, dict) or not isinstance(settings, dict):
            raise NetError("a network's outputs, their biases and its settings are mappings")
        if any(type(value) is not int for value in (*settings.values(), *out_bias.values())):
            raise NetError("the numbers a network was fitted with, and its outputs' biases, are whole")
        window, width, kernel, clip, cap, target_scale, rows = _whole([params[name] for name in _SIZES], "sizes")
        return cls(
            window, width, kernel, clip, cap, _whole(params["centre"], "centres"), _whole(params["spread"], "spreads"),
            _grid(params["conv"], "filters"), _whole(params["conv_bias"], "filter biases"),
            _grid(params["hidden"], "hidden units"), _whole(params["hidden_bias"], "hidden biases"),
            {str(name): _whole(row, "outputs") for name, row in out.items()},
            {str(name): bias for name, bias in out_bias.items()}, target_scale, rows, dict(settings),
            str(params["fitted_with"]),
        )  # fmt: skip
