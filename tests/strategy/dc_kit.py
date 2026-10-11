"""Shared pieces for the Directional Change tests: quote streams built from a list of mids, and a sleeve to run."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from fractions import Fraction

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.strategy.dc.sleeve import TREND, DcSleeve
from tests.venues.kit import MS, SECOND, T0

BIG = 10**9
SIZE = 20_000
SPREAD = 10
UNIT = SIZE * 10  # what one price unit on 20,000 EUR.USD is worth, in millionths of a dollar
FEE = 2_000_000  # the per-order minimum of the test cost model: 2.00 USD
ROUND_TRIP = SPREAD * UNIT + 2 * FEE
TENTH_PERCENT = Fraction(1, 1000)
LOW, HIGH = 108_000, 108_600


def quotes(
    mids: Iterable[int], *, spread: int = SPREAD, start_ns: int = T0, interval_ns: int = 100 * MS, pair: str = "EUR.USD"
) -> list[Quote]:
    """One quote a mid, with the same spread on each, an interval apart. The mids are whole price units."""
    out = []
    for number, middle in enumerate(mids):
        bid = middle - spread // 2
        out.append(Quote(pair, bid, bid + spread, BIG, BIG, start_ns + number * interval_ns, number + 1))
    return out


def zigzag(legs: int, *, low: int = LOW, high: int = HIGH, step: int = 5) -> list[int]:
    """From `low` up to `high` and back, one step a tick, `legs` straight runs in all. It starts by rising."""
    mids, at, up = [low], low, True
    for _ in range(legs):
        while (at < high) if up else (at > low):
            at += step if up else -step
            mids.append(at)
        up = not up
    return mids


def dc(name: str = "dc", **changes) -> DcSleeve:
    """A trend sleeve on a threshold of a tenth of a percent, unless the changes say otherwise."""
    settings = {
        "sizes": {"EUR.USD": SIZE}, "variant": TREND, "threshold": TENTH_PERCENT, "overshoot_multiple": None,
        "max_hold_ns": 3_600 * SECOND, "min_spread_ratio": Fraction(5), "warm_up": 2, "gap_ns": 5 * SECOND,
        "retry_ns": SECOND, "max_attempts": 3, "calendar": Calendar(), "rollover": (),
    }  # fmt: skip
    return DcSleeve(name, **{**settings, **changes})


def decided(entries: Sequence[dict], sleeve: str = "dc") -> list[tuple]:
    """Each decision of a sleeve in a journal, as (time, leg, side, attempt)."""
    return [
        (e["at_ns"], e["leg"], e["side"], e["attempt"])
        for e in entries
        if e["type"] == "decision" and e["sleeve"] == sleeve
    ]


def filled(entries: Sequence[dict], sleeve: str = "dc") -> list[tuple]:
    """Each fill of a sleeve in a journal, as (side, price)."""
    return [(e["side"], e["price"]) for e in entries if e["type"] == "fill" and e["sleeve"] == sleeve]


def net(result: dict, name: str = "dc") -> int:
    return result["sleeves"][name]["pairs"]["EUR.USD"]["net_micro"]
