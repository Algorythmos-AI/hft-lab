"""Every silence in the recording gets exactly one reason; "unexplained" is what is left when none applies."""
from __future__ import annotations

from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from hftlab.calendar import Calendar, Closure
from hftlab.record import format as fmt
from hftlab.record.gaps import CONNECTION_LOST, MARKET_CLOSED, PROCESS_DOWN, UNEXPLAINED, Gap, GapTracker
from hftlab.venues.port import ConnectionState
from tests.record.kit import HOUR, PAIRS, SECOND, T0

THRESHOLD = 5 * SECOND
FRIDAY_CLOSE = 1_792_184_400 * SECOND  # 2026-10-16 21:00 UTC, 17:00 in New York
SUNDAY_OPEN = FRIDAY_CLOSE + 48 * HOUR + 900 * SECOND
UP, DOWN = fmt.STATE_CODES[ConnectionState.RECONCILING], fmt.STATE_CODES[ConnectionState.DISCONNECTED]


class Feed:
    """Builds records in order and runs them through a tracker."""

    def __init__(self, calendar: Calendar | None = None, **options) -> None:
        self.tracker = GapTracker(PAIRS, calendar or Calendar(), THRESHOLD, **options)
        self.index, self.gaps = 0, []

    def _take(self, kind: int, at: int, pair: int = fmt.NO_PAIR, a: int = 0) -> None:
        self.gaps += self.tracker.observe(fmt.Record(self.index, at, self.index, kind, pair, a), PAIRS)
        self.index += 1

    def quote(self, at: int, pair: int = 0) -> None:
        self._take(fmt.QUOTE, at, pair)

    def heartbeat(self, at: int, pair: int = 0) -> None:
        self._take(fmt.HEARTBEAT, at, pair)

    def state(self, at: int, code: int) -> None:
        self._take(fmt.CONNECTION, at, a=code)

    def start(self, at: int) -> None:
        self._take(fmt.START, at)


def connected() -> Feed:
    feed = Feed()
    feed.start(T0)
    feed.state(T0, UP)
    return feed


def test_a_quiet_book_with_heartbeats_is_not_a_gap():
    feed = connected()
    feed.quote(T0)
    for second in range(1, 600):
        feed.heartbeat(T0 + second * SECOND)
    feed.quote(T0 + 600 * SECOND)
    assert feed.gaps == []


def test_a_silence_up_to_the_threshold_is_not_a_gap_and_one_nanosecond_more_is():
    feed = connected()
    feed.quote(T0)
    feed.quote(T0 + THRESHOLD)
    assert feed.gaps == []
    feed.quote(T0 + 2 * THRESHOLD + 1)
    assert feed.gaps == [Gap("EUR.USD", T0 + THRESHOLD, T0 + 2 * THRESHOLD + 1, UNEXPLAINED)]


def test_a_silence_with_nothing_to_explain_it_is_unexplained():
    feed = connected()
    feed.quote(T0)
    feed.quote(T0 + 60 * SECOND)
    assert [(g.pair, g.reason) for g in feed.gaps] == [("EUR.USD", UNEXPLAINED)]


def test_a_lost_connection_explains_the_silence_it_caused_and_no_later_one():
    feed = connected()
    feed.quote(T0)
    feed.state(T0 + 2 * SECOND, DOWN)
    feed.state(T0 + 40 * SECOND, UP)
    feed.quote(T0 + 41 * SECOND)
    feed.quote(T0 + 120 * SECOND)
    assert [g.reason for g in feed.gaps] == [CONNECTION_LOST, UNEXPLAINED]


def test_a_restart_of_the_recorder_explains_the_silence_as_process_down():
    feed = connected()
    feed.quote(T0)
    feed.start(T0 + 300 * SECOND)
    feed.state(T0 + 301 * SECOND, UP)
    feed.quote(T0 + 302 * SECOND)
    assert feed.gaps == [Gap("EUR.USD", T0, T0 + 302 * SECOND, PROCESS_DOWN)]


def test_the_weekend_is_market_closed_whatever_else_happened_during_it():
    feed = connected()
    feed.quote(FRIDAY_CLOSE - SECOND)
    feed.state(FRIDAY_CLOSE + HOUR, DOWN)
    feed.start(SUNDAY_OPEN - HOUR)
    feed.state(SUNDAY_OPEN - HOUR, UP)
    feed.quote(SUNDAY_OPEN + 2 * SECOND)
    assert feed.gaps == [Gap("EUR.USD", FRIDAY_CLOSE - SECOND, SUNDAY_OPEN + 2 * SECOND, MARKET_CLOSED)]


def test_a_silence_that_runs_well_into_the_open_market_is_not_excused_by_the_weekend():
    feed = connected()
    feed.quote(FRIDAY_CLOSE - SECOND)
    feed.quote(SUNDAY_OPEN + 10 * 60 * SECOND)
    assert [g.reason for g in feed.gaps] == [UNEXPLAINED]


def test_a_holiday_closure_counts_as_market_closed():
    holiday = Calendar(closures=(Closure("Example closure", T0, T0 + 6 * HOUR),))
    feed = Feed(holiday, connected=True)
    feed.quote(T0 - SECOND)
    feed.quote(T0 + 6 * HOUR + SECOND)
    assert [g.reason for g in feed.gaps] == [MARKET_CLOSED]


def test_each_pair_has_its_own_silences():
    feed = connected()
    feed.quote(T0, pair=0)
    feed.quote(T0, pair=1)
    for second in range(1, 61):
        feed.quote(T0 + second * SECOND, pair=0)
    feed.quote(T0 + 61 * SECOND, pair=1)
    assert feed.gaps == [Gap("USD.JPY", T0, T0 + 61 * SECOND, UNEXPLAINED)]


def test_the_first_data_of_a_pair_closes_no_gap_and_a_silence_still_open_can_be_asked_for():
    feed = connected()
    feed.quote(T0 + HOUR)
    assert feed.gaps == [] and feed.tracker.silent(T0 + HOUR + THRESHOLD) == []
    later = T0 + HOUR + 60 * SECOND
    assert feed.tracker.silent(later) == [Gap("EUR.USD", T0 + HOUR, later, UNEXPLAINED)]


ACTIONS = ["quote0", "quote1", "heartbeat0", "down", "up", "start"]
steps = st.lists(st.tuples(st.sampled_from(ACTIONS), st.integers(0, 30)), max_size=40)


def play(feed: Feed, script, at: int) -> int:
    for what, wait in script:
        at += wait * SECOND
        if what == "down":
            feed.state(at, DOWN)
        elif what == "up":
            feed.state(at, UP)
        elif what == "start":
            feed.start(at)
        elif what == "heartbeat0":
            feed.heartbeat(at)
        else:
            feed.quote(at, pair=int(what[-1]))
    return at


@given(steps, steps)
def test_a_tracker_resumed_from_its_carry_finds_the_same_gaps_as_one_that_never_stopped(first, second):
    whole = Feed()
    end = play(whole, first, T0)
    carried = whole.tracker.carry()
    before = len(whole.gaps)
    play(whole, second, end)

    resumed = Feed()
    resumed.tracker = GapTracker.resume(PAIRS, Calendar(), THRESHOLD, carried)
    play(resumed, second, end)
    assert resumed.gaps == whole.gaps[before:]


@given(steps)
def test_every_gap_is_longer_than_the_threshold_has_one_known_reason_and_gaps_of_a_pair_do_not_overlap(script):
    feed = Feed()
    play(feed, script, T0)
    for gap in feed.gaps:
        assert gap.end_ns - gap.start_ns > THRESHOLD
        assert gap.reason in (MARKET_CLOSED, PROCESS_DOWN, CONNECTION_LOST, UNEXPLAINED)
    for pair in PAIRS:
        mine = [g for g in feed.gaps if g.pair == pair]
        assert all(earlier.end_ns <= later.start_ns for earlier, later in pairwise(mine))
