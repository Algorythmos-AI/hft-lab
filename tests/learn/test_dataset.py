"""Datasets: features known before the label starts, a label window that ends, and a builder that refuses a leak."""
from __future__ import annotations

import dataclasses
import itertools
from fractions import Fraction

import pytest

from hftlab.calendar import Calendar
from hftlab.core import Quote, Side
from hftlab.features import FeatureRow
from hftlab.journal import canonical
from hftlab.labels import AT_HORIZON, LATE_EXIT, NO_ENTRY, PENALISED, Labeller, LabelSpec
from hftlab.learn.dataset import DatasetError, LeakError, build, build_from_days, check_causal, cuts
from hftlab.learn.validate import three_way
from hftlab.record.store import days
from tests.learn.kit import CONFIG, COST, FEE, HORIZON, IMBALANCE, SIZE, WIN, dataset, features, labels, planted, record
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
    assert made.dropped == {"window_not_ended": 11}, "the decisions of the last second, and one latency"
    assert made.counted == {"no_entry": 0, "late_exit": 0, "penalised": 0}
    assert len(made.rows) + 11 == len(list(features().rows(stream)))


def test_a_decision_the_venue_would_not_have_filled_is_kept_and_says_how_it_was_priced():
    """One quote shows 1,000 at the ask. Nobody can buy on it: not to enter, and not to buy back a sale."""
    stream = planted(1, 4)
    stream[60] = dataclasses.replace(stream[60], ask_size=1_000)
    made, whole = dataset(stream), dataset(planted(1, 4))
    assert len(made.rows) == len(whole.rows), "no decision is left out for what happened after it"
    assert made.dropped == whole.dropped and made.counted == {"no_entry": 1, "late_exit": 0, "penalised": 1}
    entering = next(row for row in made.rows if row.buy_how == NO_ENTRY)
    assert entering.n == 61 and entering.buy_micro == 0, "it would have bought on that quote: no trade, nothing made"
    assert (entering.sell_how, entering.sell_micro) == (AT_HORIZON, -COST), "and it could have sold there"
    leaving = next(row for row in made.rows if row.sell_how == PENALISED)
    assert leaving.n == 51, "sold a second earlier, it had to buy back on that quote"
    assert leaving.sell_micro == -2 * FEE, "kit's labels set no wait and no penalty: closed where it was opened"
    assert leaving.buy_how == AT_HORIZON and leaving.end_ns == leaving.known_ns + HORIZON + 5 * MS


ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York
AROUND = {"no_fill_before_ns": 2 * SECOND, "no_fill_after_ns": 2 * SECOND, "widen_before_ns": 4 * SECOND,
          "widen_after_ns": 4 * SECOND, "spread_multiplier_x100": 300}  # fmt: skip


def over_the_rollover(**spec):
    """A still book from twelve seconds before the rollover to twelve after, and its dataset under a no-fill window."""
    stream = planted(7, 8, effect=False, start_ns=ROLLOVER - 12 * SECOND)
    labeller = Labeller(LabelSpec(HORIZON, **spec), labels().instrument, plain_costs(**AROUND), Calendar())
    return build(stream, features(), labeller)


def test_a_holding_time_with_a_rollover_inside_it_is_priced_by_hand():
    made = over_the_rollover(exit_wait_ns=10 * SECOND, stuck_increments=40)
    by_time = {row.known_ns - ROLLOVER: row for row in made.rows}
    held_in = by_time[-2_500 * MS]  # in at the widened ask 2.495 s before; its exit, a second later, cannot fill
    assert (held_in.buy_how, held_in.sell_how) == (LATE_EXIT, LATE_EXIT)
    assert held_in.end_ns == ROLLOVER + 2_005 * MS, "the first order to arrive after the no-fill window"
    assert held_in.buy_micro == held_in.sell_micro == -(10 + 20) * SIZE * 10 - 2 * FEE == -10_000_000, (
        "the spread, and each side moved out by 10 at both ends, where a quiet second costs 6,000,000"
    )
    shut_out = by_time[-1_000 * MS]
    assert (shut_out.buy_how, shut_out.buy_micro, shut_out.sell_micro) == (NO_ENTRY, 0, 0), "no entry in the window"
    assert by_time[-6 * SECOND].buy_micro == -COST and by_time[6 * SECOND].buy_how == AT_HORIZON
    assert made.counted["late_exit"] == 2 * 10, "the ten decisions of the second before the window, both sides"
    # an entry arrives in the window for the forty decisions from 2.0 s before the rollover to 1.9 s after
    assert made.counted["no_entry"] == 2 * 40 and made.counted["penalised"] == 0
    short = over_the_rollover(exit_wait_ns=2 * SECOND, stuck_increments=40)
    stuck = {row.known_ns - ROLLOVER: row for row in short.rows}[-2_500 * MS]
    assert stuck.buy_how == PENALISED and stuck.buy_micro == -40 * 5 * SIZE * 10 - 2 * FEE == -44_000_000
    assert stuck.end_ns == stuck.known_ns + HORIZON + 2 * SECOND + 5 * MS and short.counted["late_exit"] == 0


def test_the_old_rule_would_have_reported_a_better_result_on_a_stream_with_a_rollover_in_the_holding_time():
    """Leaving out every decision whose exit did not fill removes exactly the trades that were stuck."""
    made = over_the_rollover(exit_wait_ns=10 * SECOND, stuck_increments=40)
    old = [row for row in made.rows if (row.buy_how, row.sell_how) == (AT_HORIZON, AT_HORIZON)]  # the rule that was
    assert len(old) < len(made.rows)
    before = three_way(old, made.pair, lambda row: Side.BUY, seed=3)
    after = three_way(made.rows, made.pair, lambda row: Side.BUY, seed=3)
    for was, now in ((before.model, after.model), (before.random, after.random)):
        assert now.total_micro < was.total_micro, "the stuck trades are losses the old rule never counted"
        assert Fraction(now.total_micro, now.trades) < Fraction(was.total_micro, was.trades), "and per trade too"
        assert now.trades == was.trades + 10 and now.late == 10 and now.unfilled == 40 and was.unfilled == 0
    assert after.model.total_micro - before.model.total_micro == 10 * -10_000_000
    no_wait = over_the_rollover(exit_wait_ns=0, stuck_increments=40)
    worst = three_way(no_wait.rows, made.pair, lambda row: Side.BUY, seed=3)
    assert worst.model.penalised == 10 and worst.model.total_micro == before.model.total_micro + 10 * -44_000_000


def test_a_dataset_says_what_it_was_made_from():
    stream = planted(2, 5)
    made = build(stream, features(), labels(profit_increments=8, loss_increments=6), days=["2025-10-09"])
    assert made.pair == "EUR.USD" and made.features == features().keys and made.days == ("2025-10-09",)
    assert made.label == {"version": 2, "horizon_ns": HORIZON, "profit_increments": 8, "loss_increments": 6,
                          "exit_wait_ns": 0, "stuck_increments": 0}  # fmt: skip
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
