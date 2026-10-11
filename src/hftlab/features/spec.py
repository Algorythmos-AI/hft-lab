"""What a feature is called, and what it is computed over (ADR 0020).

A feature is named, versioned and, where it looks back, tied to one window. Its `key` is what a dataset and a
model file keep: a model trained on `mid_change_x2:1000000000@v1` is refused a row built to any other key. A
change to what a feature computes is a new version, never an edit under the same key.
"""
from __future__ import annotations

from dataclasses import dataclass

from hftlab.core import _checks


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    version: int
    unit: str
    window_ns: int = 0  # how far it looks back; 0 for a feature of the book in hand

    def __post_init__(self) -> None:
        _checks.token("name", self.name)
        _checks.positive("version", self.version)
        _checks.non_negative("window_ns", self.window_ns)

    @property
    def key(self) -> str:
        return f"{self.name}:{self.window_ns}@v{self.version}" if self.window_ns else f"{self.name}@v{self.version}"


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    """The windows of one feature set, all in nanoseconds.

    `stale_after_ns` is how long a book may go unconfirmed, by a quote or a heartbeat, before what was known of
    it is thrown away.

    `window_rows` is how many rows, the one in hand and the ones before it, a model is shown at once. It is 1
    for a model that reads one row. Above 1 the rows are made by `hftlab.features.window.WindowBuilder`
    (ADR 0029), and `hftlab.features.source` is what picks between the two.
    """

    lookbacks_ns: tuple[int, ...]
    rate_window_ns: int
    range_window_ns: int
    stale_after_ns: int
    window_rows: int = 1

    def __post_init__(self) -> None:
        _checks.positive("window_rows", self.window_rows)
        _checks.positive("rate_window_ns", self.rate_window_ns)
        _checks.positive("range_window_ns", self.range_window_ns)
        _checks.positive("stale_after_ns", self.stale_after_ns)
        if not self.lookbacks_ns or list(self.lookbacks_ns) != sorted(set(self.lookbacks_ns)):
            raise ValueError("the look-backs are listed once each, shortest first, and there is at least one")
        for lookback in self.lookbacks_ns:
            _checks.positive("lookback", lookback)

    @property
    def longest_ns(self) -> int:
        """The longest window: how much history a row needs behind it."""
        return max(*self.lookbacks_ns, self.rate_window_ns, self.range_window_ns)

    def specs(self) -> tuple[FeatureSpec, ...]:
        """The features, in the order of a row's values."""
        return (
            FeatureSpec("spread", 1, "price units"),
            *(FeatureSpec("mid_change_x2", 1, "price units, times two", lookback) for lookback in self.lookbacks_ns),
            FeatureSpec("quote_rate", 1, "quotes in the window", self.rate_window_ns),
            FeatureSpec("size_imbalance_ppm", 1, "millionths of the displayed size, bid minus ask"),
            FeatureSpec("range_x2", 1, "price units, times two", self.range_window_ns),
            FeatureSpec("since_change_ns", 1, "nanoseconds"),
            FeatureSpec("time_of_day_s", 1, "seconds since the 17:00 New York rollover"),
            FeatureSpec("to_rollover_s", 1, "seconds to the next rollover"),
            FeatureSpec("to_week_close_s", 1, "seconds to the Friday close"),
        )
