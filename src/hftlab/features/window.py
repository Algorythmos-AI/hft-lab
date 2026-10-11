"""Windows of feature rows: the row in hand and the ones before it, side by side (ADR 0029).

A model that reads a sequence is shown the last `window_rows` rows of one pair at once. A window is made here,
one event at a time, by the same `step` offline and live, from the rows `FeatureBuilder` makes and from nothing
else. Its values are those rows' values end to end, oldest first, and its keys say so: `spread@v1[-2]` is the
spread two rows before the one in hand, and `spread@v1`, as ever, the spread of the row in hand.

The platform's rule about time holds for every row of a window (ADR 0020). A window ends in the row made on
the event in hand, so nothing in it is later than that event; it says which event its EARLIEST row was made on
and when that row was known (`first_n`, `first_ns`), and a dataset checks both ends (`hftlab.learn.dataset`).

A window never spans a time nobody could vouch for. The rows in hand are thrown away, and a window starts
again from nothing:

  gap          on an event of the pair that gives no row. That is every case in which the builder below could
               not trust the book or forgot it: a book unconfirmed for longer than `stale_after_ns`, a market
               that is shut, a crossed book, a book with no size, a history not yet as long as its windows;
  connection   on a connection change or a recorder restart;
  session      when the row in hand belongs to another trading day than the rows held: a window does not
               reach across the 17:00 New York rollover.

So the rows of a window are consecutive rows of one pair, in one trading day, with no event of that pair
between them that gave no row. Until `window_rows` such rows are in hand there is no window, and so no row:
`left_out` counts the rows that became no window, by the reason the window they would have ended was broken.
A stream begins with a gap: the builder's own history is not yet as long as its windows.

The state is a value, like the builder's, and can be written out and read back exactly.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.features.builder import FeatureBuilder, FeatureRow, FeatureState, at_ns
from hftlab.features.spec import FeatureConfig
from hftlab.record.format import Heartbeat, Marker, RecordedEvent
from hftlab.venues.port import ConnectionEvent

MOST_ROWS: Final = 64  # of one window
GAP, CONNECTION, SESSION = "gap", "connection", "session"
REASONS: Final = (GAP, CONNECTION, SESSION)


@dataclass(frozen=True, slots=True)
class WindowState(FeatureState):
    held: tuple[FeatureRow, ...] = ()  # the rows a window may be made of, oldest first: at most `window_rows`
    broken: str = GAP  # why the rows before them are not held


def key(base: str, back: int) -> str:
    """The key of a feature `back` rows before the row in hand. The row in hand keeps the key it always had."""
    return f"{base}[-{back}]" if back else base


class WindowBuilder(FeatureBuilder):
    """The last `config.window_rows` rows of one pair as one row. It holds settings only, like the builder it is."""

    def __init__(self, pair: str, config: FeatureConfig, calendar: Calendar) -> None:
        super().__init__(pair, config, calendar)
        if not 2 <= config.window_rows <= MOST_ROWS:
            raise ValueError(f"a window is between 2 rows and {MOST_ROWS}, not {config.window_rows}")
        self.count = config.window_rows
        self.single = FeatureBuilder(pair, _one_row(config), calendar)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(key(base, back) for back in reversed(range(self.count)) for base in self.single.keys)

    def initial(self) -> WindowState:
        return WindowState()

    # ---- written out and read back ----------------------------------------------------------------------------

    def settings(self) -> dict[str, object]:
        return {**self.single.settings(), "keys": list(self.keys), "window_rows": self.count}

    def dump(self, state: FeatureState) -> dict[str, object]:
        held, broken = _window(state)
        rows = [[row.n, row.known_ns, list(row.values)] for row in held]
        return {**self.single.dump(_plain(state)), "held": rows, "broken": broken}

    def load(self, saved: object) -> WindowState:
        if not isinstance(saved, Mapping) or "held" not in saved or saved.get("broken") not in REASONS:
            raise ValueError("not a saved state of a window of feature rows")
        plain = self.single.load({name: value for name, value in saved.items() if name not in ("held", "broken")})
        rows, width, held = saved["held"], len(self.single.keys), []
        if not isinstance(rows, list) or len(rows) > self.count:
            raise ValueError(f"a saved window holds at most {self.count} rows")
        for row in rows:
            if not isinstance(row, list) or len(row) != 3 or not isinstance(row[2], list) or len(row[2]) != width:
                raise ValueError(f"a saved window's row is an event, a time and {width} values")
            if any(type(value) is not int for value in (row[0], row[1], *row[2])) or not 0 < row[0] <= plain.n:
                raise ValueError("a saved window's rows hold whole numbers, and were made on events already shown")
            held.append(FeatureRow(self.pair, row[0], row[1], tuple(row[2])))
        if [row.n for row in held] != sorted({row.n for row in held}):
            raise ValueError("a saved window's rows are in the order they were made")
        return _state(plain, tuple(held), saved["broken"])

    # ---- one event ----------------------------------------------------------------------------------------------

    def advance(
        self, state: FeatureState, event: RecordedEvent, now_ns: int
    ) -> tuple[WindowState, FeatureRow | None, FeatureRow | None]:
        """The state after `event`, the single row it completes if any, and the window that row completes if any."""
        held, broken = _window(state)
        plain, row = self.single.step(_plain(state), event, now_ns)
        window = None
        if isinstance(event, ConnectionEvent | Marker):
            held, broken = (), CONNECTION
        elif isinstance(event, Quote | Heartbeat) and event.pair == self.pair:
            if row is None:
                held, broken = (), GAP
            else:
                if held and held[-1].known_ns < plain.day[0]:
                    held, broken = (), SESSION
                held = (*held, row)[-self.count :]
                if len(held) == self.count:
                    values = tuple(value for one in held for value in one.values)
                    window = FeatureRow(self.pair, row.n, row.known_ns, values, held[0].n, held[0].known_ns)
        return _state(plain, held, broken), row, window

    def step(self, state: FeatureState, event: RecordedEvent, now_ns: int) -> tuple[FeatureState, FeatureRow | None]:
        after, _, window = self.advance(state, event, now_ns)
        return after, window

    def left_out(self, events: Iterable[RecordedEvent]) -> dict[str, int]:
        """The single rows of a stream that completed no window, by why the window they would have ended was broken."""
        counts = {f"window_{reason}": 0 for reason in REASONS}
        state, now = self.initial(), 0
        for event in events:
            now = max(now, at_ns(event))
            state, row, window = self.advance(state, event, now)
            if row is not None and window is None:
                counts[f"window_{state.broken}"] += 1
        return counts


def _one_row(config: FeatureConfig) -> FeatureConfig:
    return FeatureConfig(config.lookbacks_ns, config.rate_window_ns, config.range_window_ns, config.stale_after_ns)


def _plain(state: FeatureState) -> FeatureState:
    """The single-row builder's own state, out of a window's."""
    return FeatureState(
        state.n, state.now_ns, state.times, state.mids, state.top, state.fresh_ns, state.changed_ns, state.day,
        state.week_close_ns,
    )  # fmt: skip


def _state(plain: FeatureState, held: tuple[FeatureRow, ...], broken: str) -> WindowState:
    return WindowState(
        plain.n, plain.now_ns, plain.times, plain.mids, plain.top, plain.fresh_ns, plain.changed_ns, plain.day,
        plain.week_close_ns, held, broken,
    )  # fmt: skip


def _window(state: FeatureState) -> tuple[tuple[FeatureRow, ...], str]:
    return (state.held, state.broken) if isinstance(state, WindowState) else ((), GAP)


def source(pair: str, config: FeatureConfig, calendar: Calendar) -> FeatureBuilder:
    """The builder of the rows this configuration describes: single rows, or windows of them."""
    return WindowBuilder(pair, config, calendar) if config.window_rows > 1 else FeatureBuilder(pair, config, calendar)
