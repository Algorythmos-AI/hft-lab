"""The reconnection schedule is a pure function of the attempt number."""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.venues.supervisor import SECOND_NS, Backoff


def test_the_default_schedule_doubles_from_one_second_to_a_minute():
    delays = [Backoff().delay_ns(n) // SECOND_NS for n in range(1, 10)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_a_schedule_that_does_not_grow():
    assert [Backoff(5, 1, 5).delay_ns(n) for n in (1, 2, 50)] == [5, 5, 5]


@given(st.integers(1, 10**9), st.integers(1, 10), st.integers(0, 10**12), st.integers(1, 200))
def test_delays_never_shrink_and_stay_between_the_first_delay_and_the_cap(first, factor, extra, attempt):
    backoff = Backoff(first, factor, first + extra)
    delay, following = backoff.delay_ns(attempt), backoff.delay_ns(attempt + 1)
    assert first <= delay <= following <= first + extra
    assert type(delay) is int


def test_a_very_late_attempt_costs_nothing_to_compute():
    assert Backoff().delay_ns(10**6) == 60 * SECOND_NS


@pytest.mark.parametrize("arguments", [(0, 2, 10), (-1, 2, 10), (10, 2, 5), (10, 0, 20), (1.0, 2, 10), (10, 2.0, 20)])
def test_a_schedule_that_makes_no_sense_is_refused(arguments):
    with pytest.raises((TypeError, ValueError)):
        Backoff(*arguments)


@pytest.mark.parametrize("attempt", [0, -1, 1.0, True])
def test_attempts_are_counted_from_one(attempt):
    with pytest.raises((TypeError, ValueError)):
        Backoff().delay_ns(attempt)
