"""Shared pieces for the tests of the review commands: a sleeve that trades by a script, and a recording of a stream."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from hftlab.core import Quote, Side
from hftlab.record.store import days
from hftlab.strategy import Intent
from tests.learn.kit import record
from tests.venues.kit import MS, T0

BIG = 10**9
SIZE = 20_000
UNIT = SIZE * 10  # what one price unit on 20,000 EUR.USD is worth, in millionths of a dollar
FEE = 2_000_000  # the per-order minimum of the test cost model
BUY, SELL = Side.BUY, Side.SELL


class Scripted:
    """A sleeve that sends the orders it was told to, on the quotes it was told to send them on, and knows nothing."""

    def __init__(self, plan: dict[int, tuple[Side, int]], name: str = "control", pair: str = "EUR.USD") -> None:
        self.name, self._plan, self._pair = name, plan, pair

    def initial(self) -> None:
        return None

    def decide(self, state, event, now_ns, view):
        if isinstance(event, Quote) and event.pair == self._pair and event.seq in self._plan:
            side, quantity = self._plan[event.seq]
            return None, (Intent(self._pair, side, quantity),)
        return None, ()


def book(mids: Sequence[int], *, spread: int = 10, start_ns: int = T0, pair: str = "EUR.USD") -> list[Quote]:
    """One quote a mid, a tenth of a second apart, numbered from 1. The mids are whole price units."""
    return [
        Quote(pair, mid - spread // 2, mid - spread // 2 + spread, BIG, BIG + n % 2, start_ns + n * 100 * MS, n + 1)
        for n, mid in enumerate(mids)
    ]


def recording(root: Path, stream) -> list[Path]:
    """Write the stream into a recorder's store and return its day folders."""
    return days(record(root, stream))
