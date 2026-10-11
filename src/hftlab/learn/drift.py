"""Drift: has the market the model serves in moved away from the one it was trained on? (ADR 0008, ADR 0023)

Three monitors, each a pure function of exact counts:

  feature shift   training stores, for every feature, bin edges taken from its own values and the count in each
                  bin (`summarise`). Live rows are counted into the same edges. The statistic is the share of
                  the live rows that would have to change bins for the two to agree, in millionths: half the
                  sum of the absolute differences of the two shares. It is a fraction of whole numbers. The
                  index usually reported under the name of population stability needs a logarithm, and so a
                  float; nothing is decided on a float here, so that index is not what is compared.
  calibration     what the model expected of each trade it took against what the trade made in the shadow
                  book, by bucket of the expectation. The gap of a bucket is its mean expectation less its mean
                  result, rounded up: a model whose expectations have stopped coming true shows it here.
  spread regime   the median live spread against the median spread of the training rows, in hundredths. The
                  outcomes a model was fitted to were priced at the spreads of its training weeks.

Each has a threshold in config/learn.yaml, null as shipped. A breach has one consequence, which is not taken
here: the command (`python -m hftlab.learn.drift`) asks the trader, through its request files, to take the
sleeve down to shadow, and pages. With a threshold null the monitor is computed, reported and breaches nothing.
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final

MILLION: Final = 1_000_000
FEATURE_SHIFT, CALIBRATION, SPREAD = "feature_shift", "calibration_decay", "spread_regime"
SPREAD_KEY: Final = "spread@v1"
EXPECTED_KEY: Final = "expected_micro"  # the model's own expectation, summarised like a feature


class DriftError(ValueError):
    """A summary that is not one, or live values that cannot be compared with it."""


def edges_of(values: Sequence[int], bins: int) -> list[int]:
    """Up to `bins - 1` edges taken from the values themselves, in order and each once: bin `k` holds what is
    above edge `k - 1` and not above edge `k`."""
    if bins < 2 or not values:
        raise DriftError("a summary needs at least two bins and one value")
    ordered = sorted(values)
    return sorted({ordered[len(ordered) * k // bins - 1 if len(ordered) * k >= bins else 0] for k in range(1, bins)})


def counts_in(edges: Sequence[int], values: Sequence[int]) -> list[int]:
    """How many of the values fall in each bin of fixed edges. Exact, and the same for the same values."""
    counts = [0] * (len(edges) + 1)
    for value in values:
        counts[bisect_left(edges, value)] += 1
    return counts


def median(values: Sequence[int]) -> int:
    """The lower median: a value that was seen."""
    if not values:
        raise DriftError("no values, no median")
    return sorted(values)[(len(values) - 1) // 2]


def summarise(columns: Mapping[str, Sequence[int]], bins: int, *, rows: int) -> dict[str, object]:
    """What training keeps of its rows for the drift monitor: per column, the edges, the counts and the median."""
    kept: dict[str, object] = {}
    for key, values in columns.items():
        edges = edges_of(values, bins)
        kept[key] = {"edges": edges, "counts": counts_in(edges, values), "median": median(values)}
    return {"rows": rows, "bins": bins, "columns": kept}


def by_column(keys: Sequence[str], rows: Sequence[Sequence[int]]) -> dict[str, list[int]]:
    """Rows of values as one list per key, in key order."""
    return {key: [row[position] for row in rows] for position, key in enumerate(keys)}


def shift_ppm(train: Sequence[int], served: Sequence[int]) -> int:
    """The share of live rows in other bins than training had them, in millionths, rounded up."""
    total_train, total_served = sum(train), sum(served)
    if len(train) != len(served) or total_train <= 0 or total_served <= 0 or min(*train, *served) < 0:
        raise DriftError("two counts over the same bins, each of at least one row")
    moved = sum(abs(seen * total_train - was * total_served) for was, seen in zip(train, served, strict=True))
    return -(-moved * MILLION // (2 * total_train * total_served))


@dataclass(frozen=True, slots=True)
class Finding:
    monitor: str
    subject: str  # the feature, the bucket or the spread
    value: int | None  # the statistic; None: too little to judge
    threshold: int | None  # None: not in force
    breached: bool

    def plain(self) -> dict[str, object]:
        return {
            "monitor": self.monitor, "subject": self.subject, "value": self.value, "threshold": self.threshold,
            "breached": int(self.breached),
        }  # fmt: skip


def _column(summary: Mapping[str, object], key: str) -> tuple[list[int], list[int], int]:
    columns = summary.get("columns")
    column = columns.get(key) if isinstance(columns, dict) else None
    if not isinstance(column, dict):
        raise DriftError(f"the training summary has no column {key}")
    edges, counts, middle = column.get("edges"), column.get("counts"), column.get("median")
    if not isinstance(edges, list) or not isinstance(counts, list) or type(middle) is not int:
        raise DriftError(f"column {key} of the training summary is not edges, counts and a median")
    if len(counts) != len(edges) + 1 or any(type(value) is not int for value in (*edges, *counts)):
        raise DriftError(f"column {key} of the training summary does not hold one count per bin")
    return edges, counts, middle


def feature_shift(
    summary: Mapping[str, object], keys: Sequence[str], rows: Sequence[Sequence[int]], *, threshold_ppm: int | None,
    min_rows: int | None,
) -> list[Finding]:  # fmt: skip
    """One finding per feature: the live rows counted into the stored edges, against the stored counts."""
    enough = min_rows is not None and len(rows) >= min_rows
    found = []
    for position, key in enumerate(keys):
        edges, counts, _ = _column(summary, key)
        value = shift_ppm(counts, counts_in(edges, [row[position] for row in rows])) if rows else None
        breached = enough and threshold_ppm is not None and value is not None and value > threshold_ppm
        found.append(Finding(FEATURE_SHIFT, key, value, threshold_ppm, breached))
    return found


def calibration(
    summary: Mapping[str, object], trades: Sequence[tuple[int, int]], *, threshold_micro: int | None,
    min_rows: int | None,
) -> list[Finding]:  # fmt: skip
    """One finding per bucket of the model's expectation: mean expected less mean realised, over (expected, made)."""
    edges, _, _ = _column(summary, EXPECTED_KEY)
    buckets: list[list[tuple[int, int]]] = [[] for _ in range(len(edges) + 1)]
    for expected, made in trades:
        buckets[bisect_left(edges, expected)].append((expected, made))
    found = []
    for number, bucket in enumerate(buckets):
        gap = Fraction(sum(e - m for e, m in bucket), len(bucket)) if bucket else None
        value = None if gap is None else -(-gap.numerator // gap.denominator)  # rounded up
        enough = min_rows is not None and len(bucket) >= min_rows
        breached = enough and threshold_micro is not None and value is not None and value > threshold_micro
        found.append(Finding(CALIBRATION, f"bucket-{number}", value, threshold_micro, breached))
    return found


def spread_regime(
    summary: Mapping[str, object], spreads: Sequence[int], *, threshold_x100: int | None, min_rows: int | None
) -> Finding:
    """The live median spread over the training median, in hundredths, rounded up."""
    _, _, trained = _column(summary, SPREAD_KEY)
    if not spreads or trained <= 0:
        return Finding(SPREAD, SPREAD_KEY, None, threshold_x100, False)
    value = -(-median(spreads) * 100 // trained)
    enough = min_rows is not None and len(spreads) >= min_rows
    breached = enough and threshold_x100 is not None and value > threshold_x100
    return Finding(SPREAD, SPREAD_KEY, value, threshold_x100, breached)


@dataclass(frozen=True, slots=True)
class Watch:
    """What one look at one sleeve found."""

    sleeve: str
    model: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def breached(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.breached)

    def plain(self) -> dict[str, object]:
        return {
            "sleeve": self.sleeve, "model": self.model, "breached": [f"{f.monitor}:{f.subject}" for f in self.breached],
            "findings": [finding.plain() for finding in self.findings],
        }  # fmt: skip
