"""Datasets of windows: the rule about time holds for a window's earliest row as for its latest (ADR 0029).

The builder trusts a source of windows no more than a source of single rows. It makes the single rows itself
and refuses a window that is not exactly them; the tests below plant each way a window can be wrong.
"""
from __future__ import annotations

import dataclasses

import pytest

from hftlab.calendar import next_rollover
from hftlab.features import FeatureRow
from hftlab.learn.dataset import LeakError, Row, build, check_causal, check_windows
from hftlab.learn.validate import walk_forward
from hftlab.record.format import Marker
from tests.learn.kit import CONFIG, HORIZON, dataset, features, labels, planted
from tests.venues.kit import MS, SECOND, T0

FOUR = dataclasses.replace(CONFIG, window_rows=4)
WIDTH = 10
WEEK = 7 * 24 * 3_600 * SECOND


def windowed(stream, **options):
    return build(stream, features(FOUR), labels(), **options)


class Tampered:
    """The real windows, each changed by `spoil` before the builder sees it. It shows its single rows honestly."""

    pair = "EUR.USD"

    def __init__(self, spoil, *, honest_about=lambda events: events) -> None:
        self._real, self._spoil, self._about = features(FOUR), spoil, honest_about
        self.keys, self.single = self._real.keys, self._real.single

    def rows(self, events):
        events = list(events)
        return [self._spoil(row, events) for row in self._real.rows(events)]

    def left_out(self, events):
        return self._real.left_out(events)


# ---- the join --------------------------------------------------------------------------------------------------------


def test_a_dataset_of_windows_keeps_when_each_windows_earliest_row_was_known_and_counts_the_rows_that_made_none():
    stream = planted(1, 6)
    made, single = windowed(stream), dataset(stream)
    by_n = {row.n: row for row in single.rows}
    assert made.rows and len(made.features) == 4 * WIDTH and made.features[-WIDTH:] == single.features
    for row in made.rows:
        assert row.features[-WIDTH:] == by_n[row.n].features, "the row in hand is the single row of the same event"
        one = by_n[row.n]
        assert (row.buy_micro, row.sell_micro, row.end_ns) == (one.buy_micro, one.sell_micro, one.end_ns)
        assert row.first_ns == row.known_ns - 300 * MS == row.earliest_ns, "three rows back, a tenth of a second each"
        assert row.plain()[-1] == row.first_ns and len(row.plain()) == len(by_n[row.n].plain()) + 1
    assert all(row.first_ns == 0 and row.earliest_ns == row.known_ns for row in single.rows)
    assert made.dropped == {"window_not_ended": 11, "window_gap": 3, "window_connection": 0, "window_session": 0}
    assert single.dropped == {"window_not_ended": 11}, "a dataset of single rows is written as it always was"
    assert len(single.rows) == len(made.rows) + 3 and made.content_hash != single.content_hash


def test_the_stream_cut_and_disturbed_gives_the_same_windows_so_the_real_ones_pass_every_check():
    stream = planted(2, 5)
    assert windowed(stream, checks=10_000) == windowed(stream), "cut after every event: no window reads past its own"
    source = features(FOUR)
    check_windows(source, stream, list(source.rows(stream)))
    check_windows(features(), stream, list(features().rows(stream)))  # single rows claim no window: nothing to check


# ---- a window that peeks, planted ------------------------------------------------------------------------------------


def test_a_window_that_holds_the_next_row_instead_of_ending_on_its_own_event_is_refused():
    """The planted peek: every window is slid one row forward, so its newest row is the one after the decision."""
    def slide(row: FeatureRow, events) -> FeatureRow:
        later = {one.n: one for one in features(FOUR).rows(events)}
        ahead = later.get(row.n + 1, row)
        return dataclasses.replace(row, values=ahead.values)

    stream = planted(4, 5)
    with pytest.raises(LeakError, match="a feature reads the future"):
        build(stream, Tampered(slide), labels())
    with pytest.raises(LeakError, match="is not the 4 single rows up to its own event"):
        check_windows(Tampered(slide), stream, Tampered(slide).rows(stream))
    for checks in (1, 2, 16):
        with pytest.raises(LeakError):
            build(stream, Tampered(slide), labels(), checks=checks)


def test_a_window_that_peeks_only_in_its_newest_place_is_caught_on_a_book_that_never_moves():
    quiet = planted(4, 5, effect=False)

    def peek(row: FeatureRow, events) -> FeatureRow:
        ahead = events[row.n] if row.n < len(events) else events[row.n - 1]
        return dataclasses.replace(row, values=(*row.values[:-1], ahead.bid_size))

    with pytest.raises(LeakError, match="a feature reads the future"):
        check_causal(Tampered(peek), quiet, Tampered(peek).rows(quiet), 16)
    with pytest.raises(LeakError):
        build(quiet, Tampered(peek), labels())


@pytest.mark.parametrize(
    ("spoil", "why"),
    [
        (lambda row: dataclasses.replace(row, first_ns=row.first_ns + 100 * MS), "it begins on event"),
        (lambda row: dataclasses.replace(row, first_n=row.first_n + 1, first_ns=row.first_ns + 100 * MS), "it begins"),
        (lambda row: dataclasses.replace(row, first_n=0, first_ns=0), "it begins on event"),
        (lambda row: dataclasses.replace(row, first_ns=row.known_ns + 1), "it begins on event"),
        (lambda row: dataclasses.replace(row, values=row.values[WIDTH:]), "not the 3 single rows|not 40 values wide"),
        (lambda row: dataclasses.replace(row, values=row.values[:-1]), "whole single rows|not 40 values wide"),
        (lambda row: dataclasses.replace(row, values=(*row.values[WIDTH:], *row.values[:WIDTH])), "oldest first"),
    ],
)  # fmt: skip
def test_a_window_that_says_it_begins_later_than_it_does_or_is_not_its_rows_in_order_is_refused(spoil, why):
    """Understating a window's reach is how a later row would slip past the purge. The builder knows the reach."""
    stream = planted(4, 5)
    source = Tampered(lambda row, events: spoil(row) if row.n == 60 else row)
    with pytest.raises(LeakError, match=why):
        build(stream, source, labels())


def test_rows_that_claim_a_window_from_a_source_that_cannot_show_its_single_rows_are_refused():
    class Claims:
        pair, keys = "EUR.USD", features().keys

        def rows(self, events):
            claim = {"first_n": 1, "first_ns": 1}
            return [dataclasses.replace(row, **claim) for row in features().rows(events)]

    with pytest.raises(LeakError, match="cannot show the rows it was made of"):
        build(planted(4, 3), Claims(), labels())


def test_a_window_that_spans_a_restart_a_gap_or_a_rollover_is_refused_whatever_its_source_says():
    """A source that keeps its rows across what should have emptied its hands. The builder looks at the events."""
    stream = planted(4, 4)
    restart = [*stream[:60], Marker(True, stream[59].recv_ts_ns, 2), *stream[60:]]

    class Forgetful:
        """Windows made by sliding over the single rows, with no regard for what lies between them."""

        pair = "EUR.USD"

        def __init__(self) -> None:
            self.keys, self.single = features(FOUR).keys, features(FOUR).single

        def rows(self, events):
            plain = list(self.single.rows(events))
            for last in range(3, len(plain)):
                held = plain[last - 3 : last + 1]
                values = tuple(value for one in held for value in one.values)
                yield FeatureRow(self.pair, held[-1].n, held[-1].known_ns, values, held[0].n, held[0].known_ns)

        def left_out(self, events):
            return {}

    assert build(stream, Forgetful(), labels()).rows, "on an unbroken stream, sliding is what a window is"
    with pytest.raises(LeakError, match="spans a gap, a closed market or a connection change"):
        build(restart, Forgetful(), labels())
    crossed = [*stream[:60], dataclasses.replace(stream[60], bid=stream[60].ask + 1), *stream[61:]]
    with pytest.raises(LeakError, match="spans a gap"):
        build(crossed, Forgetful(), labels())
    turn = next_rollover(T0)
    over = planted(4, 4, start_ns=turn - 6 * SECOND)
    with pytest.raises(LeakError, match="reaches across a trading day's rollover"):
        build(over, Forgetful(), labels())
    assert windowed(restart).rows and windowed(crossed).rows and windowed(over).rows, "the real builder lets go in time"
    assert windowed(over).dropped["window_session"] == 3


# ---- the purge goes by the earliest row ------------------------------------------------------------------------------


def a_row(known_ns: int, week: int, *, first_ns: int = 0) -> Row:
    return Row(known_ns, known_ns, known_ns + HORIZON, known_ns, week * WEEK, (1,), 1, -1, first_ns=first_ns)


def test_a_training_label_must_have_ended_an_embargo_before_the_earliest_row_of_any_test_window():
    """Two plans over the same decisions. In the second the test week's rows are windows that began 3 s earlier."""
    monday = 10 * WEEK + 5 * SECOND
    train = [a_row(monday - WEEK + step * SECOND, 10) for step in range(10)]
    single = [*train, a_row(monday, 11), a_row(monday + SECOND, 11)]
    reach = (monday - 3 * SECOND, monday - 2 * SECOND)
    windows = [*train, a_row(monday, 11, first_ns=reach[0]), a_row(monday + SECOND, 11, first_ns=reach[1])]
    how = {"embargo_ns": HORIZON, "horizons_ns": [HORIZON]}
    assert walk_forward(single, **how).folds[0].purged == 0 == walk_forward(windows, **how).folds[0].purged
    late = [*train, a_row(monday - 4 * SECOND, 10)]  # a label that ends 3 s before the test week's first decision
    plain = walk_forward([*late, *single[10:]], **how).folds[0]
    reaching = walk_forward([*late, *windows[10:]], **how).folds[0]
    assert plain.purged == 0 and len(plain.train) == 11, "against single rows it ended in time, embargo and all"
    assert reaching.purged == 1 and len(reaching.train) == 10, "against a window that began 3 s earlier it did not"
    assert all(late[p].end_ns + HORIZON < monday - 3 * SECOND for p in reaching.train)
    other = [a_row(monday, 11, first_ns=monday - SECOND), a_row(monday + SECOND, 11, first_ns=monday - 3 * SECOND)]
    assert walk_forward([*late, *other], **how).folds[0].purged == 1, "the earliest of them all, not the first row's"


def test_in_a_real_dataset_of_windows_every_fold_is_purged_against_where_its_test_windows_begin():
    from tests.learn.kit import weeks

    made = windowed(weeks(1, 12), checks=2)
    plan = walk_forward(made.rows, embargo_ns=made.window_ns, horizons_ns=[HORIZON])
    assert len(plan.folds) == 2
    for fold in plan.folds:
        begins = min(made.rows[p].first_ns for p in fold.test)
        assert begins < made.rows[fold.test[0]].known_ns
        assert all(made.rows[p].end_ns + plan.embargo_ns < begins for p in fold.train)
        rows = [made.rows[p] for p in (*fold.train, *fold.test)]
        assert all(row.first_ns <= row.known_ns < row.end_ns for row in rows)
