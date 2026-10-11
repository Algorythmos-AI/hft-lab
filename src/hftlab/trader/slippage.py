"""Shadow against broker: what the same orders got on the two venues (ADR 0003, ADR 0019).

Every order a sleeve places on paper goes to the shadow venue at the same moment, under the same id. For each
order that both venues filled for the same quantity, the difference is

    side x (what the shadow venue charged - what the broker charged), per unit, in price increments

so a positive number means the shadow venue filled at a worse price than the broker did. That is the direction
the cost model is meant to err in: a paper account fills at the displayed price, and the model adds latency,
slippage and a wider spread near the rollover (ADR 0013). A negative number is the model being kinder than the
broker, and is the thing to look for.

Everything is an exact integer. A difference that is not a whole number of increments (an order filled in
pieces at several prices) is rounded down, towards the model looking kinder, and counted under `inexact`.
Orders the two venues did not fill alike are counted, not compared.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from hftlab.core import Side

Fills = Sequence[tuple[int, int]]  # (quantity, price) of each fill of one order on one venue


@dataclass(frozen=True, slots=True)
class Matched:
    """One order as both venues answered it."""

    sleeve: str
    pair: str
    side: Side
    increment: int  # the pair's price increment, in price units
    broker: Fills
    shadow: Fills


def difference(order: Matched) -> tuple[int, bool] | None:
    """(the difference in whole increments, whether it was exact); None when the two fills cannot be compared."""
    filled = sum(quantity for quantity, _ in order.broker)
    if filled == 0 or filled != sum(quantity for quantity, _ in order.shadow):
        return None
    paid = [sum(quantity * price for quantity, price in fills) for fills in (order.shadow, order.broker)]
    whole, rest = divmod(order.side.sign * (paid[0] - paid[1]), filled * order.increment)
    return whole, rest == 0


def report(orders: Iterable[Matched]) -> list[dict[str, object]]:
    """One line per sleeve and pair, in a fixed order. Plain integers and text: it goes into a journal as it is."""
    lines: dict[tuple[str, str], dict[str, int]] = {}
    for order in orders:
        line = lines.setdefault((order.sleeve, order.pair), dict.fromkeys(_COUNTS, 0))
        found = difference(order)
        if found is None:
            if order.broker or order.shadow:
                only = "broker_only" if not order.shadow else "shadow_only" if not order.broker else "unequal"
                line[only] += 1
            continue
        increments, exact = found
        line["lowest_increments"] = increments if not line["count"] else min(line["lowest_increments"], increments)
        line["highest_increments"] = increments if not line["count"] else max(line["highest_increments"], increments)
        line["count"] += 1
        line["sum_increments"] += increments
        line["inexact"] += not exact
    out: list[dict[str, object]] = []
    for (sleeve, pair), line in sorted(lines.items()):
        mean = line["sum_increments"] // line["count"] if line["count"] else 0
        out.append({"sleeve": sleeve, "pair": pair, **line, "mean_increments": mean})
    return out


_COUNTS = (
    "count", "sum_increments", "lowest_increments", "highest_increments", "inexact", "broker_only", "shadow_only",
    "unequal",
)  # fmt: skip
