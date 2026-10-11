"""Labels: what taking a side at a decision would have come to, priced as the shadow venue prices (ADR 0020).

A label is hindsight. It is computed from what happened AFTER a decision, it is never an input to one, and it
is never a strategy's profit: it is what a model is trained to predict and what an oracle is scored on.

For a decision on event `n`, received at `t`, and a horizon `H`:

  outcome   the result, after costs, of a fill-or-kill market order of the minimum size sent at `t` and an
            opposite one sent at `t + H`, each priced by `CostModel` exactly as the shadow venue would
            (ADR 0013): it arrives one latency later, meets the quote in force then, crosses the spread, pays
            the slippage and the commission, and fills only if the venue would have filled it. Money is in
            integer millionths of the pair's quote currency.
  barrier   which came first after the entry: the position's value at the price it could be closed at rose by
            the profit barrier (+1), fell by the loss barrier (-1), or the horizon ran out (0). Barriers are in
            price increments. A level is never a target.

`end_ns` is when the label's window ENDS: the moment the exit arrives, `t + H` plus the latency. A label may be
used for training only by a decision made after that. The label reads the book in force when its orders
arrive, so of the stream it uses the events after `n` and the book they left standing.

The latency is the cost model's largest: the shadow venue draws one between its smallest and its largest for
every order, and a label cannot know the draw. With the two equal, as in the tests, the label is to the unit
what the venue does. The exit is sent at exactly `t + H`; a sleeve sends its exit on the first event at or
after that, so on a quiet book the two can differ by the wait for that event.

A side that the venue would not have filled at either end has no outcome: None, never a guess.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple

from hftlab.calendar import Calendar
from hftlab.core import CASH_SCALE, Instrument, Quote, Side, _checks
from hftlab.record.format import Heartbeat, RecordedEvent
from hftlab.venues.port import ConnectionEvent
from hftlab.venues.sim.cost import NO_FILL, WIDENED, CostModel

HINDSIGHT: Final = True  # every value in this package is computed from the future of a decision
PROFIT, LOSS, TIME = 1, -1, 0


class LabelError(ValueError):
    """The label asked for cannot be priced, or names a decision the stream does not hold."""


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """A horizon, and optionally the two barriers, in price increments. Both barriers, or neither."""

    horizon_ns: int
    profit_increments: int = 0
    loss_increments: int = 0
    version: int = 1

    def __post_init__(self) -> None:
        _checks.positive("horizon_ns", self.horizon_ns)
        _checks.non_negative("profit_increments", self.profit_increments)
        _checks.non_negative("loss_increments", self.loss_increments)
        _checks.positive("version", self.version)
        if (self.profit_increments == 0) != (self.loss_increments == 0):
            raise ValueError("a triple-barrier label needs both a profit and a loss barrier")

    @property
    def barriers(self) -> bool:
        return self.profit_increments > 0

    def as_dict(self) -> dict[str, int]:
        return {
            "version": self.version,
            "horizon_ns": self.horizon_ns,
            "profit_increments": self.profit_increments,
            "loss_increments": self.loss_increments,
        }


class Barrier(NamedTuple):
    hit: int  # PROFIT, LOSS or TIME
    end_ns: int  # when it was touched, or when the horizon ran out


@dataclass(frozen=True, slots=True)
class Label:
    pair: str
    n: int  # the event the decision was made on; the label reads the stream after it
    decided_ns: int
    end_ns: int
    buy_micro: int | None
    sell_micro: int | None
    buy_barrier: Barrier | None = None
    sell_barrier: Barrier | None = None


class PairTape:
    """One pair's quotes of a whole stream, with the times a replay would give them, to look the book up by time."""

    def __init__(self, events: Sequence[RecordedEvent], pair: str) -> None:
        self.pair = _checks.pair(pair)
        self.at: list[int] = []  # the time of every event of the stream, of any pair: it never goes back
        self.quote_times: list[int] = []
        self.quotes: list[Quote] = []
        self._confirmed: list[int] = []
        now = 0
        for event in events:
            now = max(now, event.ts_ns if isinstance(event, ConnectionEvent) else event.recv_ts_ns)
            self.at.append(now)
            if isinstance(event, Quote) and event.pair == pair:
                self.quote_times.append(now)
                self.quotes.append(event)
                self._confirmed.append(now)
            elif isinstance(event, Heartbeat) and event.pair == pair and self.quotes:
                self._confirmed.append(now)

    @property
    def last_ns(self) -> int:
        return self.at[-1] if self.at else 0

    def in_force(self, ts_ns: int) -> Quote | None:
        """The pair's last quote received at or before `ts_ns`."""
        found = bisect_right(self.quote_times, ts_ns)
        return self.quotes[found - 1] if found else None

    def confirmed(self, ts_ns: int) -> int | None:
        """When the pair's book was last confirmed, by a quote or a heartbeat, at or before `ts_ns`."""
        found = bisect_right(self._confirmed, ts_ns)
        return self._confirmed[found - 1] if found else None


class Labeller:
    def __init__(self, spec: LabelSpec, instrument: Instrument, model: CostModel, calendar: Calendar) -> None:
        if instrument.quote not in model.commission_minimum or instrument.price_scale > CASH_SCALE:
            raise LabelError(f"the cost model cannot price {instrument.pair}: no minimum fee, or too fine a price")
        self.spec, self.instrument, self.model, self._calendar = spec, instrument, model, calendar
        self.size = model.minimum_size(instrument)
        self.latency_ns = model.latency_max_ns
        self._to_micro = self.size * int(10 ** (CASH_SCALE - instrument.price_scale))

    @property
    def window_ns(self) -> int:
        """From a decision to the end of its label's window: the horizon and one latency."""
        return self.spec.horizon_ns + self.latency_ns

    def label(self, tape: PairTape, n: int) -> Label:
        """The label of a decision made on event `n` (counted from 1) of the tape's stream."""
        if tape.pair != self.instrument.pair or not 1 <= n <= len(tape.at):
            raise LabelError(f"the stream of {tape.pair} has no event {n} to label for {self.instrument.pair}")
        decided = tape.at[n - 1]
        entry_at, exit_at = decided + self.latency_ns, decided + self.window_ns
        buy = self._outcome(tape, Side.BUY, entry_at, exit_at)
        sell = self._outcome(tape, Side.SELL, entry_at, exit_at)
        up = down = None
        if self.spec.barriers:
            limit = decided + self.spec.horizon_ns
            up, down = self._barrier(tape, Side.BUY, entry_at, limit), self._barrier(tape, Side.SELL, entry_at, limit)
        return Label(tape.pair, n, decided, exit_at, buy, sell, up, down)

    def _taker(self, tape: PairTape, side: Side, at_ns: int) -> int | None:
        """The price a fill-or-kill market order arriving at `at_ns` gets from the shadow venue, or None for no fill."""
        quote, confirmed = tape.in_force(at_ns), tape.confirmed(at_ns)
        if quote is None or confirmed is None:
            return None
        phase = self.model.phase(at_ns)
        if phase == NO_FILL or not self._calendar.is_open(at_ns) or at_ns - confirmed > self.model.stale_after_ns:
            return None
        if (quote.ask_size if side is Side.BUY else quote.bid_size) < self.size:
            return None
        return self.model.taker_price(side, quote.bid, quote.ask, self.instrument, widened=phase == WIDENED)

    def _fee(self, price: int) -> int:
        notional = self.model.notional_micro(price, self.size, self.instrument)
        return self.model.commission_micro(notional, self.instrument.quote)

    def _outcome(self, tape: PairTape, side: Side, entry_at: int, exit_at: int) -> int | None:
        entry, leave = self._taker(tape, side, entry_at), self._taker(tape, side.opposite, exit_at)
        if entry is None or leave is None:
            return None
        return side.sign * (leave - entry) * self._to_micro - self._fee(entry) - self._fee(leave)

    def _barrier(self, tape: PairTape, side: Side, entry_at: int, limit_ns: int) -> Barrier | None:
        entry = self._taker(tape, side, entry_at)
        if entry is None:
            return None
        step = self.instrument.price_increment
        gain, loss = self.spec.profit_increments * step, self.spec.loss_increments * step
        first = bisect_right(tape.quote_times, entry_at) - 1  # the book the entry met is the first one judged
        for at, quote in zip(tape.quote_times[first:], tape.quotes[first:], strict=True):
            if at > limit_ns:
                break
            worth = quote.bid - entry if side is Side.BUY else entry - quote.ask  # at the price it could be closed at
            if worth <= -loss:
                return Barrier(LOSS, max(at, entry_at))
            if worth >= gain:
                return Barrier(PROFIT, max(at, entry_at))
        return Barrier(TIME, limit_ns)
