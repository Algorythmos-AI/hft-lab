"""Datasets: features known before the label starts, a label window that ends, and a builder that refuses a leak."""
from __future__ import annotations

import dataclasses
import itertools

import pytest

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.features import FeatureRow
from hftlab.journal import canonical
from hftlab.labels import Labeller, LabelSpec
from hftlab.learn.dataset import DatasetError, LeakError, build, build_from_days, check_causal, cuts
from hftlab.record.store import days
from tests.learn.kit import CONFIG, COST, HORIZON, IMBALANCE, WIN, dataset, features, labels, planted, record
from tests.venues.kit import MS, SECOND, T0, USDJPY, plain_costs

DAY = 24 * 3_600 * SECOND


# ---- the join --------------------------------------------------------------------------------------------------------


def test_a_row_is_the_features_of_a_decision_and_the_outcome_of_taking_each_side_there():
    stream = planted(1, 6)
    made = dataset(stream)
    by_n = {row.n: row for row in features().rows(stream)}
    assert made.rows
    assert all((row.features, row.known_ns) == (by_n[row.n].values, by_n[row.n].known_ns) for row in made.rows)
    for row in made.rows:
        leaning = row.features[IMBALANCE]
        assert row.end_ns == row.known_ns + HORIZON + 5 * MS, "the label's window: the horizon and one latency"
        if leaning:
            assert {row.buy_micro, row.sell_micro} == {WIN, -WIN - 2 * COST}
            assert (row.buy_micro == WIN) == (leaning > 0), "the planted effect: the mid jumps towards the heavier side"
        else:
            assert row.buy_micro == row.sell_micro == -COST
    assert {row.features[IMBALANCE] for row in made.rows} == {-500_000, 0, 500_000}


def test_a_decision_whose_label_window_has_not_ended_in_the_recording_has_no_row():
    stream = planted(1, 4)
    made = dataset(stream)
    last = stream[-1].recv_ts_ns
    assert all(row.end_ns < last for row in made.rows)
    assert made.dropped == {"window_not_ended": 11, "no_fill": 0}, "the decisions of the last second, and one latency"
    assert len(made.rows) + 11 == len(list(features().rows(stream)))


def test_a_decision_the_venue_would_not_have_filled_on_either_side_is_counted_and_left_out():
    stream = planted(1, 4)
    stream[60] = dataclasses.replace(stream[60], ask_size=1_000)
    made = dataset(stream)
    assert made.dropped["no_fill"] == 2, "the decision that would enter on it and the one that would leave on it"


def test_a_dataset_says_what_it_was_made_from():
    stream = planted(2, 5)
    made = build(stream, features(), labels(profit_increments=8, loss_increments=6), days=["2025-10-09"])
    assert made.pair == "EUR.USD" and made.features == features().keys and made.days == ("2025-10-09",)
    assert made.label == {"version": 1, "horizon_ns": HORIZON, "profit_increments": 8, "loss_increments": 6}
    assert made.cost_model == plain_costs().as_dict() and made.placeholder is True
    assert (made.size, made.window_ns, made.events, made.horizon_ns) == (20_000, HORIZON + 5 * MS, 150, HORIZON)
    assert all(row.buy_barrier in (-1, 0, 1) and row.sell_barrier in (-1, 0, 1) for row in made.rows)
    said = made.describe()
    assert said["rows"] == len(made.rows) and said["content_hash"] == made.content_hash and said["placeholder"] is True
    canonical(said)  # integers and text only: a float here would be refused
    measured = build(stream, features(), labels(costs=plain_costs(placeholder=False)))
    assert measured.placeholder is False


def test_the_hash_is_of_everything_and_the_same_stream_gives_the_same_dataset():
    stream = planted(3, 5)
    made = dataset(stream)
    assert dataset(list(stream)) == made and len(made.content_hash) == 64
    moved = [*stream[:70], dataclasses.replace(stream[70], bid=stream[70].bid - 5), *stream[71:]]
    others = [
        dataset(moved),
        dataset(stream, days=["2025-10-09"]),
        build(stream, features(), labels(costs=plain_costs(slippage_increments=1))),
        build(stream, features(), labels(2 * SECOND)),
        build(stream, features(dataclasses.replace(CONFIG, rate_window_ns=2 * SECOND)), labels()),
    ]
    assert len({made.content_hash, *(other.content_hash for other in others)}) == 6


def test_a_row_carries_its_trading_day_and_week_from_the_calendar():
    made = dataset(planted(1, 4))
    assert {row.day_ns for row in made.rows} == {1_759_957_200 * SECOND}, "the rollover of 2025-10-08 21:00 UTC"
    assert {row.week_ns for row in made.rows} == {1_760_130_000 * SECOND}, "the close of Friday 2025-10-10 21:00 UTC"


# ---- the leak test ---------------------------------------------------------------------------------------------------


class Shifted:
    """An alignment mistake: each decision is handed the features of the event after it, honestly stamped."""

    pair, keys = "EUR.USD", features().keys

    def rows(self, events):
        true = list(features().rows(events))
        return [dataclasses.replace(later, n=row.n) for row, later in itertools.pairwise(true)]


class Peeking:
    """A feature that reads the next event's bid and stamps the row as if it had not."""

    pair, keys = "EUR.USD", (*features().keys, "next_bid@v1")

    def rows(self, events):
        events = list(events)
        for row in features().rows(events):
            ahead = events[row.n] if row.n < len(events) else events[row.n - 1]
            yield FeatureRow(row.pair, row.n, row.known_ns, (*row.values, ahead.bid))


class Lagging:
    """A feature that reads the event before: late, and no leak."""

    pair, keys = "EUR.USD", (*features().keys, "last_bid@v1")

    def rows(self, events):
        events = list(events)
        for row in features().rows(events):
            yield FeatureRow(row.pair, row.n, row.known_ns, (*row.values, events[row.n - 2].bid))


def test_a_feature_that_peeks_one_event_into_the_future_is_refused_by_the_builder_itself():
    stream = planted(4, 5)
    with pytest.raises(LeakError, match="a feature reads the future"):
        build(stream, Peeking(), labels())
    with pytest.raises(LeakError, match="it was not made from the events up to its own"):
        build(stream, Shifted(), labels())
    for checks in (1, 2, 16, 10_000):
        with pytest.raises(LeakError):
            build(stream, Peeking(), labels(), checks=checks)
    assert build(stream, Lagging(), labels()).rows, "a feature of the past passes the same check"
    assert build(stream, features(), labels(), checks=10_000) == dataset(stream), "and so do the real ones"


def test_a_peek_is_caught_at_any_single_cut_even_on_a_book_that_never_moves():
    quiet = planted(4, 5, effect=False)  # the next bid is always this bid: taking the future away shows nothing
    rows = list(Peeking().rows(quiet))
    for checks in (1, 2, 16, 10_000):
        with pytest.raises(LeakError, match="are changed: a feature reads the future"):
            check_causal(Peeking(), quiet, rows, checks)
    check_causal(Lagging(), quiet, list(Lagging().rows(quiet)), 10_000)
    assert cuts(150, 10_000) == list(range(1, 150)) and cuts(150, 16)[0] == 1 and cuts(150, 16)[-1] == 149
    assert len(cuts(150, 16)) == 16 and cuts(1, 16) == [] and cuts(150, 0) == [] and cuts(150, 1) == [75]


def test_a_feature_that_reads_when_the_next_event_comes_is_caught_too():
    class Impatient:
        pair, keys = "EUR.USD", (*features().keys, "to_next_event_ns@v1")

        def rows(self, events):
            events = list(events)
            for row in features().rows(events):
                ahead = events[row.n].recv_ts_ns if row.n < len(events) else row.known_ns
                yield FeatureRow(row.pair, row.n, row.known_ns, (*row.values, ahead - row.known_ns))

    stream = planted(4, 5)
    with pytest.raises(LeakError, match="a feature reads the future"):
        build(stream, Impatient(), labels(), checks=1)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda row: dataclasses.replace(row, n=row.n + 1_000),
        lambda row: dataclasses.replace(row, pair="USD.JPY"),
        lambda row: dataclasses.replace(row, values=row.values[:-1]),
        lambda row: dataclasses.replace(row, known_ns=row.known_ns + 1),
        lambda row: dataclasses.replace(row, known_ns=row.known_ns - 1),
    ],
)
def test_rows_that_are_out_of_place_or_misdated_are_refused(spoil):
    stream = planted(4, 3)
    rows = list(features().rows(stream))
    with pytest.raises(LeakError):
        check_causal(features(), stream, [*rows[:5], spoil(rows[5]), *rows[6:]])
    with pytest.raises(LeakError, match="out of order"):
        check_causal(features(), stream, [rows[1], rows[0]])


def test_a_label_for_another_decision_is_refused():
    class Late(type(labels())):
        def label(self, tape, n):
            return dataclasses.replace(super().label(tape, n), n=n + 1)

    stream = planted(4, 3)
    late = Late(LabelSpec(HORIZON), labels().instrument, plain_costs(), Calendar())
    with pytest.raises(LeakError, match="another decision"):
        build(stream, features(), late)


# ---- from the store --------------------------------------------------------------------------------------------------


def test_a_dataset_from_day_folders_is_the_dataset_of_their_events_as_one_stream(tmp_path):
    start = T0 - T0 % DAY + DAY - 4 * SECOND  # four seconds before midnight UTC: the stream crosses into a new day
    stream = planted(5, 6, start_ns=start)
    folders = days(record(tmp_path / "rec", stream))
    assert [folder.name for folder in folders] == ["2025-10-09", "2025-10-10"]
    made = build_from_days(folders[::-1], "EUR.USD", CONFIG, LabelSpec(HORIZON), plain_costs(), Calendar())
    assert made == dataset(stream, days=["2025-10-09", "2025-10-10"]) and made.days == ("2025-10-09", "2025-10-10")
    assert any(row.known_ns < start + 4 * SECOND < row.end_ns for row in made.rows), "a label may end in the next day"
    one = build_from_days(folders[:1], "EUR.USD", CONFIG, LabelSpec(HORIZON), plain_costs(), Calendar())
    assert one.days == ("2025-10-09",) and len(one.rows) < len(made.rows)


def test_days_that_do_not_describe_the_pair_alike_cannot_be_made_into_a_dataset(tmp_path):
    folders = days(record(tmp_path / "rec", planted(5, 2)))
    arguments = (CONFIG, LabelSpec(HORIZON), plain_costs(), Calendar())
    with pytest.raises(DatasetError, match="does not describe GBP"):
        build_from_days(folders, "GBP.USD", *arguments)
    with pytest.raises(DatasetError, match="at least one recorded day"):
        build_from_days([], "EUR.USD", *arguments)
    other = days(record(tmp_path / "other", planted(5, 2, start_ns=T0 + DAY)))
    journal = other[0] / "journal.jsonl"
    journal.write_text(journal.read_text().replace('"price_increment": 5', '"price_increment": 1'))
    with pytest.raises(DatasetError, match="differently"):
        build_from_days([*folders, *other], "EUR.USD", *arguments)
    yen = Labeller(LabelSpec(HORIZON), USDJPY, plain_costs(), Calendar())
    with pytest.raises(DatasetError, match="cannot be joined"):
        build([Quote("USD.JPY", 151_000, 151_010, 1, 1, T0, 1)], features(), yen)
