"""Causal features of one pair's quote stream, one event at a time (ADR 0020).

    step(state, event, now_ns) -> (state, row or None)

The same function serves a recording and a live stream: `rows` is nothing but `step` folded over a list, and a
sleeve calls `step` with the event it was just shown. A row is made only from events up to and including the
one in hand, and says so: `known_ns` is that event's receive time and `n` its number in the stream. Nothing
after it can have entered, because nothing after it has been shown.

Every value is an exact integer:

  spread               ask minus bid, in price units
  mid_change_x2        twice the mid now, less twice the mid in force one look-back ago
  quote_rate           quotes of the pair received within the window, the one in hand included
  size_imbalance_ppm   (bid size - ask size) / (bid size + ask size), in millionths, rounded down
  range_x2             highest less lowest of twice the mid over the window, the book in force at its start included
  since_change_ns      how long the bid and the ask had stood still when this event arrived
  time_of_day_s        seconds since the 17:00 New York rollover that began the trading day
  to_rollover_s        seconds to the rollover that ends it
  to_week_close_s      seconds to the Friday close

There is no row, rather than a made-up value, when the book is crossed, when it shows no size on either side,
while the market is shut, and until the history reaches back as far as the longest window. What was known is
thrown away, and the history starts again, after a book went unconfirmed for longer than `stale_after_ns`,
after a connection change or a recorder restart, and while the market is shut: a feature never spans a time
nobody was watching.

The state is a value. Its history is a pair of tuples no longer than the quotes of the longest window, copied
on every quote: simple and replayable, and slow for windows of minutes on a feed of thousands of quotes a
second. That is a limit of this implementation, not of the definitions.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Final

from hftlab.calendar import Calendar, last_rollover, next_close, next_rollover
from hftlab.core import Quote, _checks
from hftlab.features.spec import FeatureConfig, FeatureSpec
from hftlab.record.format import Heartbeat, Marker, RecordedEvent
from hftlab.venues.port import ConnectionEvent

SECOND: Final = 1_000_000_000
MILLION: Final = 1_000_000


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """One row: the pair, the event it was made on, when that event was received, and the values in spec order."""

    pair: str
    n: int
    known_ns: int
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FeatureState:
    n: int = 0  # events shown so far, of any kind and any pair
    now_ns: int = 0  # the time reached; it never goes back
    times: tuple[int, ...] = ()  # when each quote still in the history was received
    mids: tuple[int, ...] = ()  # twice its mid
    top: tuple[int, int, int, int] | None = None  # bid, ask, bid size, ask size of the book in hand
    fresh_ns: int = 0  # when that book was last confirmed, by a quote or a heartbeat
    changed_ns: int | None = None  # when the bid or the ask last moved
    day: tuple[int, int] = (0, 0)  # the trading day in hand: from one rollover up to the next
    week_close_ns: int = 0


def at_ns(event: RecordedEvent) -> int:
    """When the live process received this event."""
    return event.ts_ns if isinstance(event, ConnectionEvent) else event.recv_ts_ns


class FeatureBuilder:
    """The features of one pair. It holds settings only; everything it remembers is in the state it returns."""

    def __init__(self, pair: str, config: FeatureConfig, calendar: Calendar) -> None:
        self.pair = _checks.pair(pair)
        self.config = config
        self.specs: tuple[FeatureSpec, ...] = config.specs()
        self._calendar = calendar

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(spec.key for spec in self.specs)

    def initial(self) -> FeatureState:
        return FeatureState()

    def step(self, state: FeatureState, event: RecordedEvent, now_ns: int) -> tuple[FeatureState, FeatureRow | None]:
        """The state after `event`, and the row it completes, if the book can be trusted to give one."""
        now, n = max(state.now_ns, now_ns), state.n + 1
        if isinstance(event, ConnectionEvent | Marker):
            return FeatureState(n=n, now_ns=now), None
        if not isinstance(event, Quote | Heartbeat) or event.pair != self.pair:
            return replace(state, n=n, now_ns=now), None
        if not self._calendar.is_open(now):
            return FeatureState(n=n, now_ns=now), None
        if state.top is not None and now - state.fresh_ns > self.config.stale_after_ns:
            state = FeatureState()
        still = now - state.changed_ns if state.changed_ns is not None else None
        times, mids, top, changed = state.times, state.mids, state.top, state.changed_ns
        if isinstance(event, Quote):
            if top is None or (event.bid, event.ask) != top[:2]:
                changed = now
            top = (event.bid, event.ask, event.bid_size, event.ask_size)
            times, mids = (*times, now), (*mids, event.mid_x2)
        elif top is None:
            return replace(state, n=n, now_ns=now), None
        oldest = bisect_right(times, now - self.config.longest_ns) - 1  # the quote in force where the windows begin
        if oldest > 0:
            times, mids = times[oldest:], mids[oldest:]
        day, week_close = state.day, state.week_close_ns
        if not day[0] <= now < day[1]:
            day = (last_rollover(now), next_rollover(now))
        if now >= week_close:
            week_close = next_close(now)
        after = FeatureState(n, now, times, mids, top, now, changed, day, week_close)
        bid, ask, bid_size, ask_size = top
        if oldest < 0 or still is None or bid > ask or bid_size + ask_size == 0:
            return after, None

        def back(window_ns: int) -> int:
            """Where in the history the quote in force one window ago is."""
            return bisect_right(times, now - window_ns) - 1

        over = mids[back(self.config.range_window_ns) :]
        values = (
            ask - bid,
            *(mids[-1] - mids[back(lookback)] for lookback in self.config.lookbacks_ns),
            len(times) - 1 - back(self.config.rate_window_ns),
            (bid_size - ask_size) * MILLION // (bid_size + ask_size),
            max(over) - min(over),
            still,
            (now - day[0]) // SECOND,
            (day[1] - now) // SECOND,
            (week_close - now) // SECOND,
        )
        return after, FeatureRow(self.pair, n, now, values)

    def rows(self, events: Iterable[RecordedEvent]) -> Iterator[FeatureRow]:
        """Every row of a stream: `step` folded over it, with the time taken from the events as a replay takes it."""
        state, now = self.initial(), 0
        for event in events:
            now = max(now, at_ns(event))
            state, row = self.step(state, event, now)
            if row is not None:
                yield row
