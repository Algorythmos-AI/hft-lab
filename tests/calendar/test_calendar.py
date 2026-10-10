"""The forex week follows New York's clock through both daylight-saving changes; config is dated and explicit."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab import calendar as cal
from hftlab.calendar import events as ev
from hftlab.calendar.week import MINUTE, SECOND, instant
from tests.support import ROOT

HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK_NS = 5 * DAY - 15 * MINUTE  # Sunday 17:15 to Friday 17:00 when New York's clock does not move
NY, SYDNEY = ZoneInfo("America/New_York"), ZoneInfo("Australia/Sydney")
times = st.integers(1_600_000_000, 2_000_000_000).map(lambda s: s * SECOND)  # 2020 to 2033


def utc(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()) * SECOND


# ---- the instants, in UTC, on both sides of both changes -----------------------------------------------------------


@pytest.mark.parametrize(
    ("opens", "closes"),
    [
        ("2026-03-01T22:15", "2026-03-06T22:00"),  # the last week of standard time
        ("2026-03-08T21:15", "2026-03-13T21:00"),  # New York moved its clock that morning: an hour earlier in UTC
        ("2026-10-25T21:15", "2026-10-30T21:00"),  # the last week of summer time
        ("2026-11-01T22:15", "2026-11-06T22:00"),  # the clock moved back that morning
        ("2027-03-14T21:15", "2027-03-19T21:00"),
    ],
)
def test_the_week_opens_and_closes_at_the_new_york_times_whatever_utc_says(opens, closes):
    opened, closed = utc(opens), utc(closes)
    assert not cal.is_week_open(opened - 1) and cal.is_week_open(opened)
    assert cal.is_week_open(closed - 1) and not cal.is_week_open(closed)
    assert cal.next_open(opened - DAY) == opened and cal.next_close(opened) == closed
    assert cal.new_york(opened).strftime("%a %H:%M") == "Sun 17:15"
    assert cal.new_york(closed).strftime("%a %H:%M") == "Fri 17:00"


def test_a_week_is_an_hour_shorter_or_longer_in_utc_only_when_the_clock_moves_inside_it():
    lengths = {}
    for opened in ("2026-03-01T22:15", "2026-03-08T21:15", "2026-10-25T21:15", "2026-11-01T22:15"):
        lengths[opened] = cal.next_close(utc(opened)) - utc(opened)
    assert set(lengths.values()) == {WEEK_NS}, "the clock moves on Sunday at 02:00, while the market is shut"
    weekend = {day: cal.next_open(utc(day)) - utc(day) for day in ("2026-03-06T22:00", "2026-10-30T21:00")}
    assert weekend["2026-03-06T22:00"] == 2 * DAY + 15 * MINUTE - HOUR, "the spring weekend is an hour short"
    assert weekend["2026-10-30T21:00"] == 2 * DAY + 15 * MINUTE + HOUR, "the autumn weekend is an hour long"


def test_a_rule_written_in_sydney_time_would_be_wrong_and_this_one_does_not_depend_on_it():
    """Sydney changes its own clock on other dates; the open moves through three Sydney times in a year."""
    sundays = ("2026-03-01T22:15", "2026-03-08T21:15", "2026-04-05T21:15", "2026-10-04T21:15", "2026-11-01T22:15")
    opens = [utc(t) for t in sundays]
    sydney = {datetime.fromtimestamp(t // SECOND, tz=SYDNEY).strftime("%a %H:%M") for t in opens}
    assert sydney == {"Mon 09:15", "Mon 08:15", "Mon 07:15"}
    assert all(cal.is_week_open(t) and not cal.is_week_open(t - 1) for t in opens)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [("2026-10-10T12:00", False), ("2026-10-11T21:14:59", False), ("2026-10-11T21:15", True),
     ("2026-10-14T03:00", True), ("2026-10-16T20:59:59", True), ("2026-10-16T21:00", False),
     ("2026-10-17T00:00", False)],
)  # fmt: skip
def test_open_and_shut_through_one_ordinary_week(moment, expected):
    assert cal.is_week_open(utc(moment)) is expected


# ---- properties, for any instant ------------------------------------------------------------------------------------


@given(times)
def test_openness_agrees_with_the_new_york_wall_clock(ts):
    local = datetime.fromtimestamp(ts // SECOND, tz=NY)
    minutes = local.hour * 60 + local.minute
    expected = {
        5: False,
        6: minutes >= 17 * 60 + 15,
        4: minutes < 17 * 60,
    }.get(local.weekday(), True)
    assert cal.is_week_open(ts) is expected


@given(times)
def test_the_next_open_and_close_bracket_exactly_one_week(ts):
    opened = cal.next_open(ts)
    closed = cal.next_close(opened)
    assert opened >= ts and cal.is_week_open(opened) and (opened == ts or not cal.is_week_open(opened - 1))
    assert cal.is_week_open(closed - 1) and not cal.is_week_open(closed)
    if cal.is_week_open(ts):
        assert 0 < closed - ts <= WEEK_NS
    else:
        assert closed - opened == WEEK_NS, "the clock never moves while the market is open"
    reopened = cal.next_open(closed)
    assert reopened - closed in (2 * DAY + 15 * MINUTE + delta for delta in (-HOUR, 0, HOUR))
    assert cal.is_week_open(ts) == (cal.next_open(ts) == ts)


@given(times)
def test_the_rollover_is_at_five_in_the_afternoon_in_new_york_every_day(ts):
    rollover = cal.rollover_near(ts)
    assert cal.new_york(rollover).strftime("%H:%M:%S") == "17:00:00" and abs(rollover - ts) <= 12 * HOUR + HOUR
    assert rollover % DAY in (21 * HOUR, 22 * HOUR), "21:00 or 22:00 UTC, depending on the season"


@given(times, st.integers(0, 120), st.integers(0, 120))
def test_the_rollover_window_is_closed_at_both_ends_and_nowhere_else(ts, before, after):
    rollover = cal.rollover_near(ts)
    before_ns, after_ns = before * MINUTE, after * MINUTE
    assert cal.in_rollover_window(rollover - before_ns, before_ns, after_ns)
    assert cal.in_rollover_window(rollover + after_ns, before_ns, after_ns)
    assert not cal.in_rollover_window(rollover - before_ns - 1, before_ns, after_ns)
    assert not cal.in_rollover_window(rollover + after_ns + 1, before_ns, after_ns)
    assert cal.in_rollover_window(ts, before_ns, after_ns) == (-before_ns <= ts - rollover <= after_ns)


def test_the_helpers_refuse_what_is_not_a_time():
    for wrong in (-1, 1.5, True, "0"):
        with pytest.raises((TypeError, ValueError)):
            cal.is_week_open(wrong)
    with pytest.raises(ValueError):
        cal.in_rollover_window(0, -1, 0)
    assert instant(datetime(2026, 10, 12).date(), datetime(2026, 10, 12, 17, 0).time()) == utc("2026-10-12T21:00")


# ---- dated configuration --------------------------------------------------------------------------------------------


def write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body)
    return path


def test_the_shipped_calendar_is_empty_and_documents_its_format():
    shipped = cal.load_calendar(ROOT / "config" / "calendar")
    assert shipped == cal.Calendar() and shipped.closures == () and shipped.events == ()
    for name, key in (("holidays.yaml", "closures: []"), ("macro_events.yaml", "events: []")):
        text = (ROOT / "config" / "calendar" / name).read_text()
        assert key in text and "EMPTY" in text and "offset" in text
    assert cal.load_calendar(ROOT / "docs") == cal.Calendar(), "a directory without the files is an empty calendar"


def test_a_holiday_shuts_the_market_inside_an_open_week(tmp_path):
    body = (
        "closures:\n"
        '  - {name: Example closure, from: "2026-12-24T17:00:00-05:00", to: "2026-12-25T12:00:00Z"}\n'
    )
    calendar = cal.Calendar(closures=ev.load_closures(write(tmp_path, "holidays.yaml", body)))
    inside, before, after = utc("2026-12-25T10:00"), utc("2026-12-24T21:59:59"), utc("2026-12-25T12:00")
    assert cal.is_week_open(inside) and not calendar.is_open(inside)
    assert calendar.is_open(before) and calendar.is_open(after)
    assert calendar.closed_reason(inside) == "Example closure" and calendar.closed_reason(before) is None
    assert calendar.closed_reason(utc("2026-12-26T12:00")) == "weekend"


def test_open_time_in_a_span_is_counted_for_explaining_gaps():
    calendar = cal.Calendar()
    friday_close, sunday_open = utc("2026-10-16T21:00"), utc("2026-10-18T21:15")
    assert calendar.open_ns_between(friday_close, sunday_open) == 0
    assert calendar.open_ns_between(friday_close - 10 * MINUTE, sunday_open + 5 * MINUTE) == 15 * MINUTE
    assert calendar.open_ns_between(utc("2026-10-14T03:00"), utc("2026-10-14T03:00") + 90 * SECOND) == 90 * SECOND


def test_a_macro_event_blacks_out_only_pairs_with_its_currency(tmp_path):
    body = (
        "defaults: {before_minutes: 2, after_minutes: 10}\n"
        "events:\n"
        '  - {name: Example release, at: "2026-11-06T13:30:00Z", currencies: [USD]}\n'
        '  - {name: Another, at: "2026-11-06T10:00:00+01:00", currencies: [EUR, GBP],\n'
        "     before_minutes: 0, after_minutes: 1}\n"
    )
    calendar = cal.Calendar(events=ev.load_events(write(tmp_path, "macro_events.yaml", body)))
    release = utc("2026-11-06T13:30")
    assert calendar.in_blackout(release, "EUR.USD") and calendar.in_blackout(release - 2 * MINUTE, "USD.JPY")
    assert not calendar.in_blackout(release - 2 * MINUTE - 1, "USD.JPY")
    assert calendar.in_blackout(release + 10 * MINUTE, "USD.JPY")
    assert not calendar.in_blackout(release + 10 * MINUTE + 1)
    assert not calendar.in_blackout(release, "EUR.GBP")
    assert calendar.in_blackout(utc("2026-11-06T09:00:30"), "EUR.GBP")
    assert calendar.in_blackout(release) and [e.name for e in calendar.events] == ["Another", "Example release"]


@pytest.mark.parametrize(
    "body",
    [
        "events:\n  - {name: x, at: '2026-11-06T13:30:00', currencies: [USD], before_minutes: 1, after_minutes: 1}\n",
        "events:\n  - {name: x, at: '2026-11-06T13:30:00Z', currencies: [USD]}\n",
        "events:\n  - {name: x, at: 'tomorrow', currencies: [USD], before_minutes: 1, after_minutes: 1}\n",
        "events:\n  - {name: x, at: '2026-11-06T13:30:00Z', currencies: [usd], before_minutes: 1, after_minutes: 1}\n",
        "events:\n  - {name: x, at: '2026-11-06T13:30:00Z', currencies: [], before_minutes: 1, after_minutes: 1}\n",
        "events:\n  - {name: x, at: '2026-11-06T13:30:00Z', currencies: [USD], before_minutes: -1, after_minutes: 1}\n",
        "events:\n  - {name: x, at: '2026-11-06T13:30Z', currencies: [USD], before_minutes: 1.5, after_minutes: 0}\n",
        "events:\n  - {name: x, at: '2026-11-06T13:30:00Z', currencies: [USD], before_minutes: 1, note: y}\n",
        "events:\n  - {name: '', at: '2026-11-06T13:30:00Z', currencies: [USD], before_minutes: 1, after_minutes: 1}\n",
        "events: {}\n",
        "nothing: here\n",
        "events: [\n",
    ],
)
def test_a_macro_event_file_that_is_ambiguous_or_malformed_is_refused(tmp_path, body):
    with pytest.raises(cal.CalendarError):
        ev.load_events(write(tmp_path, "macro_events.yaml", body))


@pytest.mark.parametrize(
    "body",
    [
        "closures:\n  - {name: x, from: '2026-12-24T17:00:00', to: '2026-12-25T17:00:00Z'}\n",
        "closures:\n  - {name: x, from: '2026-12-25T17:00:00Z', to: '2026-12-24T17:00:00Z'}\n",
        "closures:\n  - {name: x, from: '2026-12-24T17:00:00Z'}\n",
        "closures:\n  - {name: '', from: '2026-12-24T17:00:00Z', to: '2026-12-25T17:00:00Z'}\n",
        "closures:\n  - just text\n",
        "closures: 5\n",
    ],
)
def test_a_holiday_file_that_is_ambiguous_or_malformed_is_refused(tmp_path, body):
    with pytest.raises(cal.CalendarError):
        ev.load_closures(write(tmp_path, "holidays.yaml", body))


def test_the_same_instant_written_with_two_offsets_is_one_instant():
    assert ev.parse_instant("2026-11-06T13:30:00Z", "at") == ev.parse_instant("2026-11-07T00:30:00+11:00", "at")
    assert ev.parse_instant("2026-11-06T13:30:00.250Z", "at") % SECOND == 250_000_000
    assert timedelta(0) == timedelta(0)


@given(times, st.integers(0, 14 * 24 * 3600), st.integers(-3 * 24 * 3600, 3 * 24 * 3600), st.integers(0, 24 * 3600))
def test_open_time_is_exact_to_the_nanosecond_and_adds_up(start, length, closure_offset, closure_length):
    """Counted second by second it would take for ever; instead check it against its own parts and its bounds."""
    end = start + length * SECOND
    shut = start + closure_offset * SECOND
    calendar = cal.Calendar(closures=(cal.Closure("x", shut, shut + closure_length * SECOND),))
    whole, plain = calendar.open_ns_between(start, end), cal.Calendar().open_ns_between(start, end)
    middle = start + (length // 3) * SECOND
    assert whole == calendar.open_ns_between(start, middle) + calendar.open_ns_between(middle, end)
    assert 0 <= plain - whole <= closure_length * SECOND and 0 <= whole <= end - start
    if calendar.open_ns_between(start, start + 1) == 1:
        assert calendar.is_open(start)
    assert calendar.open_ns_between(start, start + 1) == (1 if calendar.is_open(start) else 0)
