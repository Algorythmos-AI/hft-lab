"""Windows of feature rows: the last rows side by side, never across a time nobody could vouch for (ADR 0029)."""
from __future__ import annotations

import dataclasses
import json

import pytest

from hftlab.calendar import Calendar, Closure, last_rollover, next_rollover
from hftlab.core import Quote
from hftlab.features import FeatureBuilder, FeatureConfig, FeatureRow, WindowBuilder, WindowState, source
from hftlab.features.window import MOST_ROWS
from hftlab.record.format import Heartbeat, Marker
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.venues.kit import MS, SECOND, T0

SINGLE = FeatureConfig(lookbacks_ns=(SECOND,), rate_window_ns=SECOND, range_window_ns=SECOND, stale_after_ns=5 * SECOND)
THREE = dataclasses.replace(SINGLE, window_rows=3)
WIDTH = 9  # the values of one row under these windows


def windows(config: FeatureConfig = THREE, calendar: Calendar | None = None) -> WindowBuilder:
    return WindowBuilder("EUR.USD", config, calendar or Calendar())


def quote(at_ns: int, bid: int = 108_500, ask: int = 108_510, pair: str = "EUR.USD", sizes=(3_000_000, 1_000_000)):
    return Quote(pair, bid, ask, *sizes, at_ns, at_ns // MS)


def steady(count: int, start_ns: int = T0, every_ns: int = 500 * MS) -> list[Quote]:
    """A quote every half second, the bid a unit higher each time so that no two rows are alike."""
    return [quote(start_ns + number * every_ns, 108_500 + number, 108_510 + number) for number in range(count)]


def singles(stream, calendar: Calendar | None = None) -> list[FeatureRow]:
    return list(FeatureBuilder("EUR.USD", SINGLE, calendar or Calendar()).rows(stream))


def check(stream, calendar: Calendar | None = None, config: FeatureConfig = THREE) -> list[FeatureRow]:
    """The windows of a stream, after holding each to what a window is: consecutive single rows, oldest first."""
    made = list(windows(config, calendar).rows(stream))
    plain = singles(stream, calendar)
    place = {row.n: position for position, row in enumerate(plain)}
    for row in made:
        held = plain[place[row.n] - config.window_rows + 1 : place[row.n] + 1]
        assert row.values == tuple(value for one in held for value in one.values)
        assert (row.first_n, row.first_ns, row.known_ns) == (held[0].n, held[0].known_ns, held[-1].known_ns)
        assert row.earliest_ns == row.first_ns < row.known_ns
        assert last_rollover(row.first_ns) == last_rollover(row.known_ns), "one trading day"
    left = windows(config, calendar).left_out(stream)
    assert len(plain) == len(made) + sum(left.values()), "every single row is in a window's end or counted"
    return made


# ---- what a window is ----------------------------------------------------------------------------------------------


def test_a_window_is_the_last_rows_side_by_side_oldest_first_and_its_keys_say_how_far_back_each_is():
    builder = windows()
    plain = FeatureBuilder("EUR.USD", SINGLE, Calendar()).keys
    assert len(plain) == WIDTH and len(builder.keys) == 3 * WIDTH == len(set(builder.keys))
    assert builder.keys[:2] == ("spread@v1[-2]", "mid_change_x2:1000000000@v1[-2]")
    assert builder.keys[WIDTH] == "spread@v1[-1]"
    assert builder.keys[-WIDTH:] == plain, "the row in hand keeps the keys it always had"
    stream = steady(12)
    made, plain_rows = check(stream), singles(stream)
    assert [row.n for row in made] == [row.n for row in plain_rows[2:]], "from the third single row on, one each"
    assert made[0].values[:WIDTH] == plain_rows[0].values and made[0].values[-WIDTH:] == plain_rows[2].values
    assert made[0].first_n == plain_rows[0].n and made[-1].first_ns == plain_rows[-3].known_ns
    assert plain_rows[0].first_n == 0 == plain_rows[0].first_ns and plain_rows[0].earliest_ns == plain_rows[0].known_ns


def test_the_builder_is_picked_by_the_configuration_and_a_window_is_two_rows_to_sixty_four():
    assert type(source("EUR.USD", SINGLE, Calendar())) is FeatureBuilder
    assert type(source("EUR.USD", THREE, Calendar())) is WindowBuilder
    with pytest.raises(ValueError, match="WindowBuilder"):
        FeatureBuilder("EUR.USD", THREE, Calendar())
    for rows in (1, MOST_ROWS + 1):
        with pytest.raises(ValueError, match="between 2 rows"):
            windows(dataclasses.replace(SINGLE, window_rows=rows))
    for rows in (0, -1, 1.5):
        with pytest.raises((ValueError, TypeError)):
            dataclasses.replace(SINGLE, window_rows=rows)
    assert windows().settings()["window_rows"] == 3 and windows().settings()["keys"] == list(windows().keys)
    assert windows().single.config.window_rows == 1


def test_an_event_of_another_pair_neither_enters_a_window_nor_breaks_one():
    stream = steady(10)
    mixed = [event for mine in stream for event in (mine, quote(mine.recv_ts_ns, 151_230, 151_240, "USD.JPY"))]
    alone, among = check(stream), check(mixed)
    assert [row.values for row in among] == [row.values for row in alone] and len(alone) == 6
    assert [row.n for row in among] == [2 * row.n - 1 for row in alone]


# ---- what breaks one ------------------------------------------------------------------------------------------------


def test_a_window_never_spans_a_book_that_went_unconfirmed_for_too_long():
    before, after = steady(8), steady(8, T0 + 10 * SECOND)  # more than five seconds of nothing between them
    made = check([*before, *after])
    gap = (before[-1].recv_ts_ns, after[0].recv_ts_ns)
    assert made and not [row for row in made if row.first_ns <= gap[0] and row.known_ns >= gap[1]]
    assert windows().left_out([*before, *after]) == {"window_gap": 4, "window_connection": 0, "window_session": 0}
    assert len(made) == 2 * (8 - 2 - 2), "each side: two rows of history, then two rows short of a window"


@pytest.mark.parametrize(
    "interruption", [ConnectionEvent(ConnectionState.READY, T0 + 4_250 * MS, 9), Marker(True, T0 + 4_250 * MS, 2)]
)
def test_a_window_never_spans_a_connection_change_or_a_recorder_restart(interruption):
    stream = steady(20)
    cut = [*stream[:9], interruption, *stream[9:]]
    made = check(cut)
    assert made and all(row.first_n > 10 or row.n < 10 for row in made), "no window has the event inside it"
    left = windows().left_out(cut)
    assert left["window_connection"] == 0 and left["window_gap"] == 4, "the builder forgot its history: a gap again"
    assert len(made) < len(check(stream))


def test_a_crossed_book_gives_no_row_and_so_breaks_the_window_it_stands_in():
    stream = steady(14)
    crossed = dataclasses.replace(stream[8], bid=stream[8].ask + 5)
    made = check([*stream[:8], crossed, *stream[9:]])
    assert all(row.first_n > 9 or row.n < 9 for row in made) and len(made) == len(check(stream)) - 3
    assert windows().left_out([*stream[:8], crossed, *stream[9:]])["window_gap"] == 2 + 2


def test_a_window_never_spans_a_closed_market():
    holiday = Calendar(closures=(Closure("holiday", T0 + 4 * SECOND, T0 + 6 * SECOND),))
    stream = steady(24)
    made = check(stream, holiday)
    shut = [quote_ for quote_ in stream if T0 + 4 * SECOND <= quote_.recv_ts_ns < T0 + 6 * SECOND]
    assert shut and made and len(made) < len(check(stream))
    assert not [row for row in made if row.first_ns < T0 + 4 * SECOND <= row.known_ns]
    assert not [row for row in made if T0 + 4 * SECOND <= row.known_ns < T0 + 6 * SECOND], "and none is made while shut"


def test_a_window_never_reaches_across_the_rollover_that_ends_a_trading_day():
    turn = next_rollover(T0)
    stream = steady(40, turn - 10 * SECOND)
    plain = singles(stream)
    assert [row for row in plain if row.known_ns < turn] and [row for row in plain if row.known_ns >= turn]
    made = check(stream)
    assert [row for row in made if row.known_ns < turn] and [row for row in made if row.first_ns >= turn]
    assert not [row for row in made if row.first_ns < turn <= row.known_ns], "yesterday is in no window of today"
    assert windows().left_out(stream)["window_session"] == 2, "the first two rows of the new day end no window"


# ---- the same offline and live, and across a restart ---------------------------------------------------------------


def test_one_event_at_a_time_gives_the_rows_a_recording_gives_and_a_heartbeat_ends_a_window_too():
    stream = [*steady(8), Heartbeat("EUR.USD", T0 + 4 * SECOND, T0 + 4 * SECOND, 0), *steady(4, T0 + 4_500 * MS)]
    builder = windows()
    state, live = builder.initial(), []
    for event in stream:
        state, row = builder.step(state, event, event.recv_ts_ns)
        if row is not None:
            live.append(row)
    assert live == check(stream) and type(state) is WindowState and len(state.held) == 3
    assert any(row.n == 9 for row in live), "a heartbeat confirms the book, and its row ends a window like any other"


def test_a_state_written_out_and_read_back_goes_on_to_the_same_windows():
    stream = steady(30)
    builder = windows()
    state = builder.initial()
    for event in stream[:17]:
        state, _ = builder.step(state, event, event.recv_ts_ns)
    saved = json.loads(json.dumps(builder.dump(state)))
    back = builder.load(saved)
    assert back == state and len(saved["held"]) == 3 and saved["broken"] == "gap"
    rest = []
    for event in stream[17:]:
        state, one = builder.step(state, event, event.recv_ts_ns)
        back, other = builder.step(back, event, event.recv_ts_ns)
        rest.append((one, other))
    assert all(one == other and one is not None for one, other in rest) and back == state
    for spoil in (
        lambda s: s.pop("held"), lambda s: s.update(broken="bored"), lambda s: s.update(held=[[1, 2, [3]]]),
        lambda s: s.update(held=s["held"] * 2), lambda s: s.update(held=[[1, 2.5, s["held"][0][2]]]),
        lambda s: s.update(held=list(reversed(s["held"]))), lambda s: s.update(held=[[10**9, 2, s["held"][0][2]]]),
        lambda s: s.pop("mids"),
    ):  # fmt: skip
        wrong = json.loads(json.dumps(saved))
        spoil(wrong)
        with pytest.raises(ValueError):
            builder.load(wrong)
    with pytest.raises(ValueError):
        plain = FeatureBuilder("EUR.USD", SINGLE, Calendar())
        builder.load(plain.dump(plain.initial()))
