"""A time as a file writes it becomes exact UTC nanoseconds, in a stated format and a stated zone, or is refused."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.history.times import SECOND, TimeError, reader, zone_of

PLAIN = "%Y-%m-%d %H:%M:%S.%f"
OCT_14 = 1_791_946_800  # 2026-10-14 03:00:00 UTC, in seconds


def test_a_time_is_read_to_the_nanosecond_and_a_short_fraction_is_not_a_small_one():
    read = reader(PLAIN, "UTC")
    assert read("2026-10-14 03:00:00.000") == OCT_14 * SECOND
    assert read("2026-10-14 03:00:00.5") == OCT_14 * SECOND + 500_000_000  # .5 is half a second, not 5 ns
    assert read("2026-10-14 03:00:00.123456789") == OCT_14 * SECOND + 123_456_789
    assert type(read("2026-10-14 03:00:00.000")) is int


@given(st.datetimes(min_value=datetime(1971, 1, 1), max_value=datetime(2100, 1, 1)), st.integers(0, 999_999))
def test_in_utc_it_agrees_with_the_calendar_for_any_instant(moment, micros):
    text = moment.strftime("%Y-%m-%d %H:%M:%S") + f".{micros:06d}"
    expected = int(moment.replace(tzinfo=UTC, microsecond=0).timestamp()) * SECOND + micros * 1_000
    assert reader(PLAIN, "UTC")(text) == expected


def test_a_zone_is_stated_and_never_guessed():
    for nothing in (None, "", 0):
        with pytest.raises(TimeError, match="never guessed"):
            reader(PLAIN, nothing)
    for unknown in ("Mars/Olympus", "utc", "GMT+2", "+2", "+25:00", " UTC"):
        with pytest.raises(TimeError):
            zone_of(unknown)


def test_a_fixed_offset_and_a_named_zone_move_the_same_wall_clock_to_different_instants():
    text = "2026-01-14 12:00:00.000"
    utc = reader(PLAIN, "UTC")(text)
    assert reader(PLAIN, "-05:00")(text) == utc + 5 * 3_600 * SECOND
    assert reader(PLAIN, "+02:00")(text) == utc - 2 * 3_600 * SECOND
    assert reader(PLAIN, "America/New_York")(text) == utc + 5 * 3_600 * SECOND  # winter
    summer = "2026-07-14 12:00:00.000"
    assert reader(PLAIN, "America/New_York")(summer) == reader(PLAIN, "UTC")(summer) + 4 * 3_600 * SECOND


def test_a_wall_clock_time_that_names_two_instants_or_none_is_not_given_one():
    """New York repeats 01:00 to 02:00 on 2026-11-01 and skips 02:00 to 03:00 on 2026-03-08."""
    read = reader(PLAIN, "America/New_York")
    assert read("2026-11-01 01:30:00.000") is None and read("2026-11-01 01:00:00.000") is None
    assert read("2026-03-08 02:30:00.000") is None
    before, after = read("2026-11-01 00:59:59.000"), read("2026-11-01 02:00:00.000")
    assert before is not None and after is not None
    assert after - before == 2 * 3_600 * SECOND + SECOND  # the repeated hour passed in between
    assert read("2026-03-08 03:00:00.000") - read("2026-03-08 01:59:59.000") == SECOND


def test_a_time_that_is_not_written_as_the_format_says_is_an_error_not_another_reading():
    read = reader(PLAIN, "UTC")
    for wrong in ("2026-10-14 03:00:00", "2026-10-14T03:00:00.000", "14.10.2026 03:00:00.000", "2026-13-01 00:00:00.0",
                  "2026-10-14 03:00:60.0", " 2026-10-14 03:00:00.0", "2026-1-14 03:00:00.0", ""):  # fmt: skip
        with pytest.raises(TimeError):
            read(wrong)


def test_a_count_since_1970_is_utc_and_whole():
    assert reader("epoch_ms", "UTC")("1791946800250") == OCT_14 * SECOND + 250_000_000
    assert reader("epoch_s", "UTC")(str(OCT_14)) == reader("epoch_ns", "UTC")(str(OCT_14 * SECOND)) == OCT_14 * SECOND
    with pytest.raises(TimeError, match="must be UTC"):
        reader("epoch_ms", "America/New_York")
    for wrong in ("1791946800.25", "-5", "1e9", ""):
        with pytest.raises(TimeError):
            reader("epoch_s", "UTC")(wrong)


@pytest.mark.parametrize("wrong", ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S %z", "%c", "", None, "%"])
def test_a_format_names_each_field_once_and_nothing_it_cannot_read_exactly(wrong):
    with pytest.raises(TimeError):
        reader(wrong, "UTC")
