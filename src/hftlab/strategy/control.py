"""Two sleeves that know nothing, on purpose (ADR 0014).

`IdleSleeve` never trades. Its result is exactly zero, which is the first thing a profit calculation has to get
right.

`ControlSleeve` trades at random: on a small, seeded share of the quotes it enters at the minimum size, long or
short by the toss of a coin, holds for a fixed time and gets out. It has no edge, so what it loses is the cost
of trading as the cost model sees it. Every other sleeve is measured against it: a sleeve that cannot beat
random entries after costs has shown nothing.

Its randomness is a hash of the seed and of the quote it is looking at, not a generator with a position: the
same seed and the same events give the same decisions, from any starting point in the stream.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from hftlab.book import SleeveView
from hftlab.core import Fill, OrderEvent, OrderState, Quote, Side
from hftlab.core.ids import check_sleeve
from hftlab.record.format import Heartbeat
from hftlab.strategy.sleeve import ENTRY, EXIT, Action, Event, Intent, Placed, Refused

MILLION = 1_000_000
FLAT, ENTERING, HOLDING, EXITING = "flat", "entering", "holding", "exiting"
_DEAD = (OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED)


class IdleSleeve:
    """Never trades."""

    def __init__(self, name: str = "idle") -> None:
        self._name = check_sleeve(name)

    @property
    def name(self) -> str:
        return self._name

    def initial(self) -> None:
        return None

    def decide(self, state: None, event: Event, now_ns: int, view: SleeveView) -> tuple[None, Sequence[Action]]:
        return None, ()


@dataclass(frozen=True, slots=True)
class PairState:
    phase: str = FLAT
    order_id: str | None = None  # the order whose outcome this pair is waiting for
    since_ns: int = 0  # when the position was taken, by the venue's time
    trips: int = 0  # round trips completed
    exits: int = 0  # exit orders sent for the current position


ControlState = tuple[PairState, ...]  # one per pair, in the sleeve's pair order


def draw(seed: int, pair: str, recv_ts_ns: int, seq: int) -> tuple[int, Side]:
    """A number below a million and a side, fixed by the seed and the quote. No generator, no hidden state."""
    digest = hashlib.blake2b(f"{seed}|{pair}|{recv_ts_ns}|{seq}".encode("ascii"), digest_size=8).digest()
    number = int.from_bytes(digest, "big")
    return number % MILLION, Side.BUY if (number >> 40) & 1 else Side.SELL


class ControlSleeve:
    def __init__(
        self, name: str, *, seed: int, sizes: Mapping[str, int], entry_per_million: int, hold_ns: int
    ) -> None:
        if not sizes or any(size <= 0 for size in sizes.values()):
            raise ValueError("the control sleeve needs a positive size for at least one pair")
        if not 0 <= entry_per_million <= MILLION or hold_ns < 0:
            raise ValueError("entry_per_million is 0 to 1,000,000 and the holding time is not negative")
        self._name = check_sleeve(name)
        self.seed, self.entry_per_million, self.hold_ns = seed, entry_per_million, hold_ns
        self.sizes = dict(sizes)
        self._slot = {pair: number for number, pair in enumerate(self.sizes)}

    @property
    def name(self) -> str:
        return self._name

    def initial(self) -> ControlState:
        return tuple(PairState() for _ in self.sizes)

    def decide(
        self, state: ControlState, event: Event, now_ns: int, view: SleeveView
    ) -> tuple[ControlState, Sequence[Action]]:
        if isinstance(event, Quote | Heartbeat):
            return self._on_market(state, event, now_ns, view)
        if isinstance(event, Placed):
            pair = event.intent.pair
            return self._put(state, pair, replace(state[self._slot[pair]], order_id=event.client_order_id)), ()
        if isinstance(event, Refused):
            return (self._back(state, event.action.pair), ()) if isinstance(event.action, Intent) else (state, ())
        if isinstance(event, Fill):
            return self._on_fill(state, event, now_ns, view), ()
        if isinstance(event, OrderEvent) and event.state in _DEAD:
            for pair, slot in self._slot.items():
                if state[slot].order_id == event.client_order_id:
                    return self._back(state, pair), ()
        return state, ()

    # ---- the market ---------------------------------------------------------------------------------------------

    def _on_market(
        self, state: ControlState, event: Quote | Heartbeat, now_ns: int, view: SleeveView
    ) -> tuple[ControlState, Sequence[Action]]:
        pair = event.pair
        if pair not in self._slot:
            return state, ()
        mine, held = state[self._slot[pair]], view.quantity(pair)
        if mine.phase == FLAT and held == 0 and isinstance(event, Quote):
            number, side = draw(self.seed, pair, event.recv_ts_ns, event.seq)
            if number < self.entry_per_million:
                waiting = replace(mine, phase=ENTERING, order_id=None)
                return self._put(state, pair, waiting), (Intent(pair, side, self.sizes[pair], ENTRY),)
        elif mine.phase == HOLDING and held != 0 and now_ns - mine.since_ns >= self.hold_ns:
            side = Side.SELL if held > 0 else Side.BUY
            waiting = replace(mine, phase=EXITING, order_id=None, exits=mine.exits + 1)
            return self._put(state, pair, waiting), (Intent(pair, side, abs(held), EXIT, attempt=mine.exits),)
        return state, ()

    # ---- news of its own orders ---------------------------------------------------------------------------------

    def _on_fill(self, state: ControlState, fill: Fill, now_ns: int, view: SleeveView) -> ControlState:
        if fill.pair not in self._slot:
            return state
        mine, held = state[self._slot[fill.pair]], view.quantity(fill.pair)
        if held == 0:
            return self._put(state, fill.pair, PairState(trips=mine.trips + 1))
        if mine.phase == ENTERING:
            since = fill.venue_ts_ns if fill.venue_ts_ns is not None else now_ns
            return self._put(state, fill.pair, replace(mine, phase=HOLDING, order_id=None, since_ns=since))
        return state

    def _back(self, state: ControlState, pair: str) -> ControlState:
        """An order came to nothing: an entry leaves the pair flat, an exit leaves it held and due."""
        mine = state[self._slot[pair]]
        if mine.phase == ENTERING:
            return self._put(state, pair, replace(mine, phase=FLAT, order_id=None))
        if mine.phase == EXITING:
            return self._put(state, pair, replace(mine, phase=HOLDING, order_id=None))
        return state

    def _put(self, state: ControlState, pair: str, new: PairState) -> ControlState:
        slot = self._slot[pair]
        return (*state[:slot], new, *state[slot + 1 :])
