"""Features: exact, causal, and the same whether a recording is folded or a live stream is taken one event at a time."""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.calendar import Calendar, Closure, last_rollover, next_close, next_rollover
from hftlab.core import Quote
from hftlab.features import FeatureBuilder, FeatureConfig, FeatureSpec
from hftlab.record.format import Heartbeat, Marker
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.venues.kit import MS, SECOND, T0

CONFIG = FeatureConfig(lookbacks_ns=(SECOND, 3 * SECOND), rate_window_ns=2 * SECOND, range_window_ns=3 * SECOND,
                       stale_after_ns=5 * SECOND)  # fmt: skip
NAMES = ["spread", "mid_change_x2", "mid_change_x2", "quote_rate", "size_imbalance_ppm", "range_x2",
         "since_change_ns", "time_of_day_s", "to_rollover_s", "to_week_close_s"]  # fmt: skip


def builder(config: FeatureConfig = CONFIG, calendar: Calendar | None = None) -> FeatureBuilder:
    return FeatureBuilder("EUR.USD", config, calendar or Calendar())


def quote(at_s: float, bid: int = 108_500, ask: int = 108_510, sizes=(3_000_000, 1_000_000), pair: str = "EUR.USD"):
    at = T0 + int(at_s * 1000) * MS
    return Quote(pair, bid, ask, *sizes, at, at // MS)


def beat(at_s: float, pair: str = "EUR.USD") -> Heartbeat:
    at = T0 + int(at_s * 1000) * MS
    return Heartbeat(pair, at, at, 0)


def named(row) -> dict[str, int]:
    return {spec.key: value for spec, value in zip(builder().specs, row.values, strict=True)}


# ---- what a feature is called ---------------------------------------------------------------------------------------


def test_every_feature_has_a_name_a_version_and_a_key_that_says_its_window():
    specs = builder().specs
    assert [spec.name for spec in specs] == NAMES and all(spec.version == 1 and spec.unit for spec in specs)
    assert builder().keys == (
        "spread@v1", "mid_change_x2:1000000000@v1", "mid_change_x2:3000000000@v1", "quote_rate:2000000000@v1",
        "size_imbalance_ppm@v1", "range_x2:3000000000@v1", "since_change_ns@v1", "time_of_day_s@v1",
        "to_rollover_s@v1", "to_week_close_s@v1",
    )  # fmt: skip
    assert FeatureSpec("spread", 2, "price units").key == "spread@v2", "a change of meaning is a new key"
    assert len(set(builder().keys)) == len(specs)


@pytest.mark.parametrize(
    "change",
    [{"lookbacks_ns": ()}, {"lookbacks_ns": (2 * SECOND, SECOND)}, {"lookbacks_ns": (SECOND, SECOND)},
     {"lookbacks_ns": (0,)}, {"rate_window_ns": 0}, {"range_window_ns": -1}, {"stale_after_ns": 0},
     {"rate_window_ns": 1.5}],
)  # fmt: skip
def test_windows_that_are_not_whole_positive_and_in_order_are_refused(change):
    settings = {"lookbacks_ns": (SECOND,), "rate_window_ns": SECOND, "range_window_ns": SECOND, "stale_after_ns": 1}
    with pytest.raises((ValueError, TypeError)):
        FeatureConfig(**{**settings, **change})


# ---- a stream worked out by hand -------------------------------------------------------------------------------------


def test_a_row_is_what_the_definitions_say_on_a_stream_worked_out_by_hand():
    stream = [
        quote(0.0, 108_500, 108_510),
        quote(1.0, 108_520, 108_530),  # the mid rises by 20
        quote(2.5, 108_520, 108_530, sizes=(1_000_000, 1_000_000)),  # a change of size only
        quote(3.0, 108_510, 108_522),  # history now reaches back three seconds: the first row
        beat(3.5),
        quote(4.0, 108_510, 108_522, sizes=(1_000_000, 3_000_000)),
    ]
    rows = list(builder().rows(stream))
    assert [(row.n, row.known_ns - T0) for row in rows] == [(4, 3 * SECOND), (5, 3_500 * MS), (6, 4 * SECOND)]
    first = named(rows[0])
    assert first["spread@v1"] == 12
    assert first["mid_change_x2:1000000000@v1"] == (108_510 + 108_522) - (108_520 + 108_530), "against 2.0 s"
    assert first["mid_change_x2:3000000000@v1"] == (108_510 + 108_522) - (108_500 + 108_510), "against 0.0 s"
    assert first["quote_rate:2000000000@v1"] == 2, "the quotes at 2.5 s and 3.0 s; the one at 1.0 s is two seconds old"
    assert first["size_imbalance_ppm@v1"] == 500_000, "(3m - 1m) / 4m"
    assert first["range_x2:3000000000@v1"] == (108_520 + 108_530) - (108_500 + 108_510)
    assert first["since_change_ns@v1"] == 2 * SECOND, "the prices last moved at 1.0 s, until this quote"
    on_the_beat = named(rows[1])
    assert on_the_beat["since_change_ns@v1"] == 500 * MS and on_the_beat["quote_rate:2000000000@v1"] == 2
    assert on_the_beat["mid_change_x2:1000000000@v1"] == (108_510 + 108_522) - (108_520 + 108_530)
    last = named(rows[2])
    assert last["size_imbalance_ppm@v1"] == -500_000 and last["since_change_ns@v1"] == SECOND
    assert last["mid_change_x2:1000000000@v1"] == 0 and last["quote_rate:2000000000@v1"] == 3
    assert last["range_x2:3000000000@v1"] == (108_520 + 108_530) - (108_510 + 108_522), "the book of 1.0 s still counts"


def test_the_times_of_day_come_from_the_calendar_and_add_up_to_the_trading_day():
    rows = list(builder().rows([quote(s) for s in range(6)]))
    at = rows[0].known_ns
    values = named(rows[0])
    assert values["time_of_day_s@v1"] == (at - last_rollover(at)) // SECOND
    assert values["to_rollover_s@v1"] == (next_rollover(at) - at) // SECOND
    assert values["to_week_close_s@v1"] == (next_close(at) - at) // SECOND
    assert values["time_of_day_s@v1"] + values["to_rollover_s@v1"] == 24 * 3_600, "no clock change that day"


def test_a_row_carries_the_receive_time_and_the_number_of_the_last_event_it_used():
    stream = [quote(0), quote(1, pair="USD.JPY"), quote(2), quote(3), quote(4, pair="USD.JPY"), quote(5)]
    rows = list(builder().rows(stream))
    assert [(row.pair, row.n, row.known_ns) for row in rows] == [
        ("EUR.USD", 4, T0 + 3 * SECOND), ("EUR.USD", 6, T0 + 5 * SECOND),
    ], "events of another pair are counted and make no row"  # fmt: skip
    assert all(type(value) is int for row in rows for value in row.values)


# ---- no row rather than a made-up value ------------------------------------------------------------------------------


def test_a_crossed_book_and_a_book_without_size_make_no_row():
    stream = [quote(s) for s in range(4)]
    stream += [quote(4, 108_520, 108_510), beat(4.5), quote(5, sizes=(0, 0)), quote(6)]
    assert [row.n for row in builder().rows(stream)] == [4, 8], "not on the crossed quote, nor on its heartbeat"


def test_a_book_nobody_confirmed_is_forgotten_and_the_history_starts_again():
    stream = [quote(s) for s in range(5)] + [quote(4 + 5.001), quote(11), quote(12), quote(13), quote(14)]
    rows = list(builder().rows(stream))
    assert [row.n for row in rows] == [4, 5, 9, 10], "nothing until three seconds of new history are behind a row"
    kept_alive = [quote(s) for s in range(5)] + [beat(7), beat(9.5), quote(10)]
    assert [row.n for row in builder().rows(kept_alive)] == [4, 5, 6, 7, 8], "a heartbeat is a confirmation"


def test_a_connection_change_or_a_recorder_restart_starts_the_history_again():
    for interruption in (ConnectionEvent(ConnectionState.READY, T0 + 4_500 * MS, 9), Marker(True, T0 + 4_500 * MS, 2)):
        stream = [quote(s) for s in range(5)] + [interruption] + [quote(s) for s in range(5, 10)]
        assert [row.n for row in builder().rows(stream)] == [4, 5, 10, 11]


def test_a_shut_market_makes_no_row_and_leaves_no_history_behind():
    holiday = Calendar(closures=(Closure("holiday", T0 + 4 * SECOND, T0 + 6 * SECOND),))
    rows = list(builder(calendar=holiday).rows([quote(s / 2) for s in range(24)]))
    assert [row.known_ns - T0 for row in rows][:3] == [3 * SECOND, 3_500 * MS, 9 * SECOND]
    friday_close = next_close(T0)
    weekend = [Quote("EUR.USD", 108_500, 108_510, 1, 1, friday_close + s * SECOND, s + 1) for s in range(10)]
    assert list(builder().rows(weekend)) == []


# ---- the same offline and live, and blind to the future --------------------------------------------------------------

EVENTS = st.lists(
    st.tuples(
        st.integers(0, 1_500),  # milliseconds since the event before
        st.sampled_from(["quote", "quote", "quote", "beat", "other", "connection", "marker"]),
        st.integers(-3, 3), st.integers(0, 4), st.integers(0, 3), st.integers(0, 3),
    ),
    max_size=60,
)  # fmt: skip


def stream_of(steps) -> list:
    out, at, mid = [], T0, 108_500
    for number, (wait_ms, kind, move, spread, bid_size, ask_size) in enumerate(steps, 1):
        at += wait_ms * MS
        mid += 5 * move
        if kind in ("quote", "other"):
            pair = "EUR.USD" if kind == "quote" else "USD.JPY"
            out.append(Quote(pair, mid, mid + 5 * (spread - 1), bid_size * 10**6, ask_size * 10**6, at, number))
        elif kind == "beat":
            out.append(Heartbeat("EUR.USD", at, at, 0))
        elif kind == "connection":
            out.append(ConnectionEvent(ConnectionState.READY, at, number))
        else:
            out.append(Marker(True, at, 1))
    return out


@given(EVENTS)
def test_a_live_stream_taken_one_event_at_a_time_gives_the_rows_of_the_recording(steps):
    stream = stream_of(steps)
    offline = list(builder().rows(stream))
    live, state = [], builder().initial()
    for event in stream:  # a new builder object for every event: nothing is remembered outside the state
        at = event.ts_ns if isinstance(event, ConnectionEvent) else event.recv_ts_ns
        before = state
        state, row = builder().step(state, event, at)
        assert builder().step(before, event, at) == (state, row), "the state handed in is not changed by a step"
        live += [row] if row is not None else []
    assert live == offline
    assert [row.n for row in offline] == sorted({row.n for row in offline})


@given(EVENTS, EVENTS, st.integers(0, 60))
def test_a_row_does_not_change_when_the_future_does(steps, other, cut):
    stream, changed = stream_of(steps), stream_of(steps[:cut] + other)
    until = min(cut, len(steps))
    assert [row for row in builder().rows(stream) if row.n <= until] == [
        row for row in builder().rows(changed) if row.n <= until
    ]
    assert list(builder().rows(stream[:until])) == [row for row in builder().rows(stream) if row.n <= until]


@given(EVENTS)
def test_every_row_is_stamped_with_the_event_it_was_made_on_and_holds_whole_numbers(steps):
    stream = stream_of(steps)
    for row in builder().rows(stream):
        event = stream[row.n - 1]
        assert isinstance(event, Quote | Heartbeat) and event.pair == "EUR.USD" and event.recv_ts_ns == row.known_ns
        assert len(row.values) == len(NAMES) and all(type(value) is int for value in row.values)
        values = named(row)
        assert values["spread@v1"] >= 0 and values["quote_rate:2000000000@v1"] >= 0
        assert values["range_x2:3000000000@v1"] >= 0
        assert -1_000_000 <= values["size_imbalance_ppm@v1"] <= 1_000_000 and values["since_change_ns@v1"] >= 0


def test_time_does_not_run_backwards_when_the_recorded_clock_stepped_back():
    stream = [quote(0), quote(1), quote(2), quote(3), quote(2.5), quote(4)]
    rows = list(builder().rows(stream))
    assert [row.known_ns - T0 for row in rows] == [3 * SECOND, 3 * SECOND, 4 * SECOND], "at the time already reached"
