"""The Directional Change sleeve (ADR 0024): a rule-based baseline, in two variants, to be judged and not believed.

The published method describes events and gives no rule to trade them by, so the two rules here are ours, and
they are the plainest there are. One sleeve is one variant on one threshold in each of its pairs:

  trend        enters in the direction of a confirmed event;
  contrarian   enters against the last event once its provisional overshoot is `overshoot_multiple`
               thresholds or more.

Both leave on the next confirmed event the other way, or when the position is `max_hold_ns` old, whichever
comes first. One position a pair, at the minimum size, with fill-or-kill market orders.

What it reads is what the detector may show at that tick: confirmed events that are not warm-up, and the
provisional overshoot. A completed overshoot is on the event that completes it and is not read here at all.

  * An event is good for one position: it can be entered on while it is the last confirmed event and no
    entry for it has filled. A warm-up event causes no order, entry or exit.
  * It does not enter when the threshold is small against the spread: the threshold's share of the mid has to
    be at least `min_spread_ratio` quoted spreads on the quote in hand. A ratio below one is refused as a
    setting, and a crossed book is never entered on.
  * It does not enter inside the rollover windows it is given (the cost model's widened and no-fill windows).
    It does not enter while the calendar says the market is shut either: a shut market is a gap (below), and
    after a gap there is no event to enter on. It still leaves in both: an exit is never held back.
  * An order that came to nothing is not sent again at once: the control sleeve's waiting time, kept in the
    state. On top of that an event gets at most `max_attempts` entry orders. An exit is sent again, one
    order a wait, until the book is flat.
  * The series is interrupted, and the detector told so, when the book went unconfirmed for longer than
    `gap_ns` or when the market was shut at some moment since the tick before. The events that follow are
    warm-up again.

Like every sleeve it is a pure function of the events (ADR 0014). The calendar and the windows are settings;
the detector's state is part of the sleeve's, so a replay decides the same, byte for byte.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Final

from hftlab.book import SleeveView
from hftlab.calendar import Calendar, in_rollover_window, next_close
from hftlab.core import Fill, OrderEvent, OrderState, Quote, Side
from hftlab.core.ids import check_sleeve
from hftlab.record.format import Heartbeat
from hftlab.strategy.dc.detector import MID, UP, Detector, DetectorState, mid
from hftlab.strategy.sleeve import ENTRY, EXIT, Action, Event, Intent, Placed, Refused

TREND, CONTRARIAN = "trend", "contrarian"
VARIANTS: Final = (TREND, CONTRARIAN)
FLAT, ENTERING, HOLDING, EXITING = "flat", "entering", "holding", "exiting"
_DEAD = (OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED)


@dataclass(frozen=True, slots=True)
class PairState:
    detector: DetectorState
    seen_ns: int | None = None  # when the book was last confirmed: a quote or a heartbeat
    open_until_ns: int = 0  # the market is open from the last tick until here; 0 when that is not known
    live: int = 0  # the number of the last confirmed event, while it may be traded on; else 0
    live_up: bool = False  # and whether it was an event upwards
    tried: int = 0  # the event the last entry order was sent for
    attempts: int = 0  # entry orders sent for that event
    entered: int = 0  # the last event an entry filled for: it is good for no second position
    phase: str = FLAT
    order_id: str | None = None  # the order whose outcome this pair is waiting for
    keyed_up: bool = False  # the direction of the event the position in hand was entered on
    since_ns: int = 0  # when the position was taken, by the venue's time
    leave: bool = False  # the event that ends the position in hand has confirmed
    trips: int = 0  # round trips completed
    exits: int = 0  # exit orders sent for the current position
    tried_ns: int | None = None  # when the order on its way, or the last that came to nothing, was sent


DcState = tuple[PairState, ...]  # one per pair, in the sleeve's pair order


class DcSleeve:
    def __init__(
        self,
        name: str,
        *,
        sizes: Mapping[str, int],
        variant: str,
        threshold: Fraction,
        overshoot_multiple: Fraction | None,
        max_hold_ns: int,
        min_spread_ratio: Fraction,
        warm_up: int,
        gap_ns: int,
        retry_ns: int,
        max_attempts: int,
        calendar: Calendar,
        rollover: Sequence[tuple[int, int]],
        price: str = MID,
    ) -> None:
        if not sizes or any(type(size) is not int or size <= 0 for size in sizes.values()):
            raise ValueError("the sleeve needs a positive size for at least one pair")
        if variant not in VARIANTS:
            raise ValueError(f"the variant is {TREND} or {CONTRARIAN}, not {variant!r}")
        if (variant == CONTRARIAN) != (overshoot_multiple is not None):
            raise ValueError("the overshoot multiple belongs to the contrarian variant, and to it alone")
        if overshoot_multiple is not None and (type(overshoot_multiple) is not Fraction or overshoot_multiple <= 0):
            raise ValueError("the overshoot multiple is an exact number of thresholds above zero")
        if type(min_spread_ratio) is not Fraction or min_spread_ratio < 1:
            raise ValueError("a threshold below one quoted spread is not traded: the least ratio is at least 1")
        if any(type(value) is not int or value < 0 for value in (max_hold_ns, gap_ns, retry_ns)) or gap_ns == 0:
            raise ValueError("the holding time and the wait are not negative, and the gap time is above zero")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("an event gets at least one entry order")
        if any(len(window) != 2 or min(window) < 0 for window in rollover):
            raise ValueError("a rollover window is a time before and a time after, neither negative")
        self._name = check_sleeve(name)
        self._detector = Detector([threshold], warm_up=warm_up, price=price)
        self._calendar = calendar
        self.variant, self.threshold, self.overshoot_multiple = variant, threshold, overshoot_multiple
        self.max_hold_ns, self.min_spread_ratio, self.gap_ns = max_hold_ns, min_spread_ratio, gap_ns
        self.retry_ns, self.max_attempts = retry_ns, max_attempts
        self.rollover: tuple[tuple[int, int], ...] = tuple((before, after) for before, after in rollover)
        self.sizes = dict(sizes)
        self._slot = {pair: number for number, pair in enumerate(self.sizes)}

    @property
    def name(self) -> str:
        return self._name

    def settings(self) -> dict[str, object]:
        """Every setting as a plain value, for a log line beside a run's results."""
        multiple = self.overshoot_multiple
        return {
            **self._detector.settings(),
            "variant": self.variant,
            "overshoot_multiple": None if multiple is None else [multiple.numerator, multiple.denominator],
            "max_hold_ns": self.max_hold_ns,
            "min_spread_ratio": [self.min_spread_ratio.numerator, self.min_spread_ratio.denominator],
            "gap_ns": self.gap_ns,
            "retry_ns": self.retry_ns,
            "max_attempts": self.max_attempts,
            "rollover_ns": [list(window) for window in self.rollover],
            "sizes": dict(self.sizes),
        }

    def initial(self) -> DcState:
        return tuple(PairState(self._detector.initial()) for _ in self.sizes)

    def decide(self, state: DcState, event: Event, now_ns: int, view: SleeveView) -> tuple[DcState, Sequence[Action]]:
        if isinstance(event, Quote | Heartbeat):
            return self._on_market(state, event, now_ns, view)
        if isinstance(event, Placed):
            pair = event.intent.pair
            if pair not in self._slot:
                return state, ()
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
        self, state: DcState, event: Quote | Heartbeat, now_ns: int, view: SleeveView
    ) -> tuple[DcState, Sequence[Action]]:
        pair = event.pair
        if pair not in self._slot:
            return state, ()
        mine = self._read(state[self._slot[pair]], event, now_ns)
        intent, mine = self._act(mine, event, now_ns, view.quantity(pair))
        return self._put(state, pair, mine), (() if intent is None else (intent,))

    def _read(self, mine: PairState, event: Quote | Heartbeat, now_ns: int) -> PairState:
        """Bring what is known of the pair up to this event: the gap rule, then the detector, then its event."""
        known, live, until = mine.detector, mine.live, mine.open_until_ns
        quiet = mine.seen_ns is not None and now_ns - mine.seen_ns > self.gap_ns
        if quiet or now_ns >= until:
            # Unconfirmed for too long, or the market was shut at some moment since the tick before (or is now).
            known, live = self._detector.gap(known), 0
            until = self._open_until(now_ns) if now_ns >= until else until
        mine = replace(mine, detector=known, seen_ns=now_ns, open_until_ns=until, live=live)
        if not isinstance(event, Quote):
            return mine
        known, confirmed = self._detector.step(known, mid(event), now_ns)
        mine = replace(mine, detector=known)
        for found in confirmed:
            if found.warm_up:
                continue  # not traded on, either way
            rising = found.direction == UP
            ends = mine.phase != FLAT and rising != mine.keyed_up
            mine = replace(mine, live=found.number, live_up=rising, leave=mine.leave or ends)
        return mine

    def _act(
        self, mine: PairState, event: Quote | Heartbeat, now_ns: int, held: int
    ) -> tuple[Intent | None, PairState]:
        if mine.tried_ns is not None and now_ns - mine.tried_ns < self.retry_ns:
            return None, mine  # the last attempt came to nothing too short a time ago
        if mine.phase == HOLDING and held != 0 and (mine.leave or now_ns - mine.since_ns >= self.max_hold_ns):
            side = Side.SELL if held > 0 else Side.BUY
            waiting = replace(mine, phase=EXITING, order_id=None, exits=mine.exits + 1, tried_ns=now_ns)
            return Intent(event.pair, side, abs(held), EXIT, attempt=mine.exits), waiting
        if mine.phase != FLAT or held != 0 or not isinstance(event, Quote):
            return None, mine
        number = mine.live
        attempt = mine.attempts if number == mine.tried else 0
        if number == 0 or number <= mine.entered or attempt >= self.max_attempts:
            return None, mine  # no event to trade on, or this one has had its position or its orders
        if not self._signalled(mine) or not self._may_enter(event, now_ns):
            return None, mine
        buying = mine.live_up if self.variant == TREND else not mine.live_up
        waiting = replace(
            mine, phase=ENTERING, order_id=None, keyed_up=mine.live_up, leave=False, tried=number,
            attempts=attempt + 1, tried_ns=now_ns,
        )  # fmt: skip
        side = Side.BUY if buying else Side.SELL
        return Intent(event.pair, side, self.sizes[event.pair], ENTRY, attempt=attempt), waiting

    def _signalled(self, mine: PairState) -> bool:
        """Is the last event one to enter on now? Always for the trend variant; for the other, far enough on."""
        if self.overshoot_multiple is None:
            return True
        # Asked only while an event is live, so the overshoot in hand is that event's, and it is not warm-up.
        so_far = self._detector.provisional(mine.detector)
        return so_far is not None and so_far.overshoot >= self.overshoot_multiple * self.threshold

    def _may_enter(self, quote: Quote, now_ns: int) -> bool:
        """The reasons not to enter that are the quote's and the hour's: the spread and the rollover.

        A shut market needs no test here: `_read` has made it a gap, and after a gap no event is live.
        """
        if quote.spread < 0 or self.threshold * mid(quote) < self.min_spread_ratio * quote.spread:
            return False
        return not any(in_rollover_window(now_ns, before, after) for before, after in self.rollover)

    def _open_until(self, now_ns: int) -> int:
        """The first moment after `now_ns` at which the market shuts; 0 when it is shut now."""
        if not self._calendar.is_open(now_ns):
            return 0
        holidays = [closure.start_ns for closure in self._calendar.closures if closure.start_ns > now_ns]
        return min([next_close(now_ns), *holidays])

    # ---- news of its own orders ---------------------------------------------------------------------------------

    def _on_fill(self, state: DcState, fill: Fill, now_ns: int, view: SleeveView) -> DcState:
        if fill.pair not in self._slot:
            return state
        mine, held = state[self._slot[fill.pair]], view.quantity(fill.pair)
        if held == 0:
            flat = replace(
                mine, phase=FLAT, order_id=None, since_ns=0, leave=False, trips=mine.trips + 1, exits=0, tried_ns=None
            )
            return self._put(state, fill.pair, flat)
        if mine.phase == ENTERING:
            since = fill.venue_ts_ns if fill.venue_ts_ns is not None else now_ns
            held_now = replace(mine, phase=HOLDING, order_id=None, entered=mine.tried, since_ns=since, tried_ns=None)
            return self._put(state, fill.pair, held_now)
        return state

    def _back(self, state: DcState, pair: str) -> DcState:
        """An order came to nothing: an entry leaves the pair flat, an exit leaves it held and due.

        `tried_ns` is kept, and it is what makes the next attempt wait.
        """
        if pair not in self._slot:
            return state
        mine = state[self._slot[pair]]
        if mine.phase == ENTERING:
            return self._put(state, pair, replace(mine, phase=FLAT, order_id=None, leave=False))
        if mine.phase == EXITING:
            return self._put(state, pair, replace(mine, phase=HOLDING, order_id=None))
        return state

    def _put(self, state: DcState, pair: str, new: PairState) -> DcState:
        slot = self._slot[pair]
        return (*state[:slot], new, *state[slot + 1 :])
