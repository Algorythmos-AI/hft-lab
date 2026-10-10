"""Shared pieces for the shadow tests: synthetic quote streams and a session run to its end."""
from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from pathlib import Path

from hftlab.core import Quote
from hftlab.shadow import Journal, Shadow, run
from hftlab.shadow.journal import read
from tests.venues.kit import EURUSD, MS, T0, USDJPY, plain_costs

BIG = 10**9
TRADED = (EURUSD, USDJPY)


def constant(
    count: int,
    *,
    bid: int = 108_500,
    ask: int = 108_510,
    interval_ns: int = 100 * MS,
    pair: str = "EUR.USD",
    start_ns: int = T0,
):
    """The same book, `count` times. The sizes alternate so that each quote is a change of the top of book."""
    return [Quote(pair, bid, ask, BIG, BIG + (n % 2), start_ns + n * interval_ns, n + 1) for n in range(count)]


def walk(seed: int, count: int, *, mid: int = 108_505, spread: int = 10, step: int = 5, interval_ns: int = 100 * MS):
    """A driftless walk of the mid on the price grid, with a constant spread and more size than anyone needs."""
    rng, out = random.Random(seed), []
    for n in range(count):
        mid += rng.choice((-step, 0, step))
        bid = mid - spread // 2
        out.append(Quote("EUR.USD", bid, bid + spread, BIG, BIG, T0 + n * interval_ns, n + 1))
    return out


def session(path: Path, sleeves: Sequence, stream: Iterable, *, seed: int = 0, costs=None, **options):
    """Run a whole session and return (summary, journal entries, the shadow)."""
    shadow = Shadow(TRADED, costs or plain_costs(), sleeves, Journal(path), seed=seed, **options)
    result = run(shadow, stream)
    return result, read(path)[1], shadow


def kinds(entries) -> list[str]:
    return [str(entry["type"]) for entry in entries]
