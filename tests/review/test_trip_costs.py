"""Round trips against the cost model: every figure worked out by hand from a book the test wrote itself.

On the test cost model a round trip of 20,000 EUR.USD costs one spread of 10 price units (2.00 USD) and two
commissions of 2.00 USD: 6.00 USD, which is 6,000,000 millionths and 12,000,000 in the doubled figures the
command prints. An order meets the quote it was decided on: the venue's delay is 5 ms and quotes are 100 ms
apart.
"""
from __future__ import annotations

import json

import pytest

from hftlab.calendar import rollover_near
from hftlab.core import Quote
from hftlab.journal import read
from hftlab.record.format import Heartbeat
from hftlab.review import trip_costs
from hftlab.review.trip_costs import Books, compare, verdict
from hftlab.review.trips import round_trips
from hftlab.shadow import Journal, Shadow, run
from hftlab.strategy import ControlSleeve, IdleSleeve
from tests.review.kit import BUY, FEE, SELL, SIZE, UNIT, Scripted, book, recording
from tests.shadow.kit import TRADED, constant, session
from tests.venues.kit import EURUSD, MS, SECOND, T0, plain_costs

COST_X2 = 2 * (10 * UNIT + 2 * FEE)  # 12,000,000: one spread and two commissions, doubled


def ran(tmp_path, plan, stream, *, costs=None, name: str = "control", **options):
    """A scripted sleeve over the stream in the real shadow runner. The journal's entries."""
    sleeves = [IdleSleeve(), Scripted(plan, name)]
    _, entries, _ = session(tmp_path / "journal.jsonl", sleeves, stream, costs=costs, **options)
    return entries


# ---- by hand ---------------------------------------------------------------------------------------------------------


def test_two_round_trips_on_a_moving_book_give_the_sums_the_mean_and_the_variance_found_by_hand(tmp_path):
    mids = [108_505] * 30
    mids[7:] = [108_515] * 23  # up 10 units between the first trip's two fills
    mids[15:] = [108_519] * 15  # and up 4 more between the second's
    stream = book(mids)
    entries = ran(tmp_path, {3: (BUY, SIZE), 8: (SELL, SIZE), 12: (SELL, SIZE), 20: (BUY, SIZE)}, stream)
    found = compare(entries, stream, "control")
    pair = found["pairs"]["EUR.USD"]
    # Trip 1 bought at 108,510 and sold at 108,510: nothing, less 4.00 of commissions. Its cost is 6.00, so its
    # excess is +2.00: the 10 units the mid rose while it was long. Trip 2 sold at 108,510 and bought back at
    # 108,524: -2.80 and -4.00. Its excess is -0.80: the 4 units the mid rose while it was short.
    assert pair["round_trips"] == 2 and pair["currency"] == "USD"
    assert pair["sum_result"] == 2 * (-2 * FEE) + 2 * (-14 * UNIT - 2 * FEE) == -21_600_000
    assert pair["sum_cost"] == 2 * COST_X2 == 24_000_000
    assert pair["sum_excess"] == 2 * (10 * UNIT) + 2 * (-4 * UNIT) == 2_400_000
    assert pair["sum_excess_squared"] == 4_000_000**2 + 1_600_000**2
    assert pair["mean_excess"] == [1_200_000, 1] and pair["mean_cost"] == [12_000_000, 1]
    # The two excesses are 4,000,000 and -1,600,000: each 2,800,000 from their mean. One degree of freedom.
    assert pair["variance_of_excess"] == [2 * 2_800_000**2, 1] == [15_680_000_000_000, 1]
    assert pair["not_exact"] == pair["unmatched"] == pair["irregular"] == pair["open_quantity"] == 0
    # Ten times the excess is exactly the cost, so it is close; two trips are far too few to tell.
    assert pair["verdict"] == found["verdict"] == "inconclusive"
    assert found["money"].endswith("TIMES TWO") and found["cost_model_is_placeholder"] is True
    assert found["cost_model"] == plain_costs().as_dict(), "the model the run was made under, from the journal"


def test_on_a_still_book_every_round_trip_costs_exactly_the_modelled_cost_and_the_pair_passes(tmp_path):
    stream = constant(3_000)
    control = ControlSleeve("control", seed=7, sizes={"EUR.USD": SIZE}, entry_per_million=50_000, hold_ns=SECOND,
                            retry_ns=SECOND)  # fmt: skip
    result, entries, _ = session(tmp_path / "j.jsonl", [IdleSleeve(), control], stream)
    pair = compare(entries, stream, "control")["pairs"]["EUR.USD"]
    fills = result["sleeves"]["control"]["fills"]
    assert pair["round_trips"] == fills // 2 > 20 and abs(pair["open_quantity"]) == (fills % 2) * SIZE
    assert pair["sum_result"] == -pair["round_trips"] * COST_X2 == -pair["sum_cost"], "minus the modelled cost, exactly"
    assert pair["sum_excess"] == pair["sum_excess_squared"] == pair["not_exact"] == 0
    assert pair["mean_excess"] == [0, 1] and pair["variance_of_excess"] == [0, 1] and pair["verdict"] == "pass"
    assert compare(entries, stream, "idle")["pairs"] == {}, "a sleeve that never traded has no round trip"


def test_round_trips_that_all_lose_the_same_move_of_the_mid_fail_and_are_exact_all_the_same(tmp_path):
    mids, plan = [], {}
    for trip in range(12):  # long over a fall of 10 units, every time
        mids += [108_505] * 5 + [108_495] * 5
        plan[trip * 10 + 3] = (BUY, SIZE)
        plan[trip * 10 + 8] = (SELL, SIZE)
    stream = book(mids)
    found = compare(ran(tmp_path, plan, stream), stream, "control")
    pair = found["pairs"]["EUR.USD"]
    assert pair["round_trips"] == 12 and pair["sum_excess"] == 12 * 2 * (-10 * UNIT) == -48_000_000
    assert pair["variance_of_excess"] == [0, 1] and pair["not_exact"] == 0, "each is the mid's move times its size"
    assert 10 * 48_000_000 > pair["sum_cost"] == 12 * COST_X2 and pair["verdict"] == found["verdict"] == "fail"


def test_near_the_rollover_the_cost_is_taken_on_the_quote_as_the_venue_widened_it(tmp_path):
    rollover = rollover_near(T0 + 12 * 3_600 * SECOND)
    costs = plain_costs(widen_before_ns=60 * SECOND, widen_after_ns=60 * SECOND, spread_multiplier_x100=300)
    stream = book([108_505] * 20, start_ns=rollover - 10 * SECOND)
    entries = ran(tmp_path, {3: (BUY, SIZE), 8: (SELL, SIZE)}, stream, costs=costs)
    fills = [(e["side"], e["price"]) for e in entries if e["type"] == "fill"]
    assert fills == [("BUY", 108_520), ("SELL", 108_490)], "three times the spread: each side 10 units further out"
    pair = compare(entries, stream, "control")["pairs"]["EUR.USD"]
    assert pair["sum_cost"] == 2 * (30 * UNIT + 2 * FEE) == 20_000_000, "a spread of 30 units, and two commissions"
    assert pair["sum_result"] == -20_000_000 and pair["sum_excess"] == 0
    assert pair["not_exact"] == pair["unmatched"] == 0


def test_two_pairs_are_measured_apart_and_never_added(tmp_path):
    euro, yen = book([108_505] * 20), book([151_235] * 20, pair="USD.JPY", start_ns=T0 + 50 * MS)
    stream = sorted([*euro, *yen], key=lambda quote: quote.recv_ts_ns)
    euros, yens = {3: (BUY, SIZE), 8: (SELL, SIZE)}, {4: (SELL, SIZE), 9: (BUY, SIZE)}
    sleeves = [Scripted(euros), Scripted(yens, pair="USD.JPY")]
    shadow = Shadow(TRADED, plain_costs(), [sleeves[0]], Journal(tmp_path / "a.jsonl"))
    run(shadow, stream)
    assert set(compare(read(tmp_path / "a.jsonl")[1], stream, "control")["pairs"]) == {"EUR.USD"}
    both = Scripted({}, "control")
    both.decide = lambda _, event, now, view: (None, [a for s in sleeves for a in s.decide(None, event, now, view)[1]])
    run(Shadow(TRADED, plain_costs(), [both], Journal(tmp_path / "b.jsonl")), stream)
    pairs = compare(read(tmp_path / "b.jsonl")[1], stream, "control")["pairs"]
    assert {name: (pair["currency"], pair["round_trips"]) for name, pair in pairs.items()} == {
        "EUR.USD": ("USD", 1), "USD.JPY": ("JPY", 1),
    }  # fmt: skip
    assert pairs["USD.JPY"]["sum_cost"] == 2 * (10 * SIZE * 1_000 + 2 * FEE), "a yen price unit: a thousandth of a yen"


# ---- what is not measured --------------------------------------------------------------------------------------------


def test_a_position_closed_in_more_than_two_fills_is_counted_as_irregular_and_one_left_open_is_reported(tmp_path):
    stream = book([108_505] * 40)
    plan = {3: (BUY, SIZE), 5: (BUY, SIZE), 9: (SELL, 2 * SIZE), 12: (SELL, SIZE), 15: (BUY, SIZE), 30: (BUY, SIZE)}
    entries = ran(tmp_path, plan, stream)
    found = round_trips(entries, "control", {"EUR.USD": EURUSD})["EUR.USD"]
    assert (len(found.trips), found.irregular, found.open_quantity) == (1, 1, SIZE)
    (trip,) = found.trips
    assert (trip.direction, trip.quantity, trip.fees_micro) == (-1, SIZE, 2 * FEE)
    assert trip.result_micro(EURUSD) == -10 * UNIT - 2 * FEE
    assert trip.exit.venue_ts_ns - trip.entry.venue_ts_ns == 300 * MS
    pair = compare(entries, stream, "control")["pairs"]["EUR.USD"]
    assert (pair["round_trips"], pair["irregular"], pair["open_quantity"]) == (1, 1, SIZE)
    assert pair["verdict"] == "inconclusive", "a journal with something in it that is not a round trip decides nothing"


def test_a_fill_that_was_not_made_at_the_recordings_quote_is_unmatched_and_its_trip_is_not_measured(tmp_path):
    stream = book([108_505] * 20)
    entries = ran(tmp_path, {3: (BUY, SIZE), 8: (SELL, SIZE), 12: (BUY, SIZE), 16: (SELL, SIZE)}, stream)
    other = list(stream)
    other[7] = Quote("EUR.USD", 108_495, 108_505, 10**9, 10**9, stream[7].recv_ts_ns, 8)  # the quote one exit met
    pair = compare(entries, other, "control")["pairs"]["EUR.USD"]
    assert (pair["round_trips"], pair["unmatched"], pair["verdict"]) == (1, 1, "inconclusive")
    assert pair["sum_cost"] == COST_X2, "the other trip is measured as before"
    before = compare(entries, stream[5:], "control")["pairs"]["EUR.USD"]
    assert before["unmatched"] == 1, "a fill before the recording's first quote met nothing"


def test_the_quote_in_force_is_the_last_one_shown_by_then_on_a_clock_that_never_goes_back():
    late = Quote("EUR.USD", 3, 4, 1, 1, 50, 2)  # stamped before the one that was shown before it
    books = Books([Quote("EUR.USD", 1, 2, 1, 1, 100, 1), late, Heartbeat("EUR.USD", 110, 110, 0),
                   Quote("USD.JPY", 7, 8, 1, 1, 105, 3), Quote("EUR.USD", 5, 6, 1, 1, 120, 4)])  # fmt: skip
    assert books.in_force("EUR.USD", 99) is None and books.in_force("EUR.USD", 100) == (3, 4), "shown at 100, both"
    assert books.in_force("EUR.USD", 119) == (3, 4) and books.in_force("EUR.USD", 120) == (5, 6)
    assert books.in_force("USD.JPY", 104) is None and books.in_force("USD.JPY", 10**18) == (7, 8)
    assert books.in_force("GBP.USD", 10**18) is None


def test_the_verdict_is_the_registered_rule_decided_on_squares_without_a_root():
    nothing = {"mean_excess": None, "mean_cost": None, "variance_of_excess": None}
    assert verdict(0, 0, 0, 0) == ("inconclusive", nothing)
    assert verdict(1, 5, 100, 25)[0] == "inconclusive", "one round trip has no spread to judge by"
    # 100 trips, each of cost 100. Excesses of +1 and -1 in turn: mean 0, variance 100/99.
    assert verdict(100, 0, 10_000, 100) == (
        "pass", {"mean_excess": [0, 1], "mean_cost": [100, 1], "variance_of_excess": [100, 99]},
    )  # fmt: skip
    # The same spread about a mean of -20: ten times the sum is 20,000 against a cost of 10,000, and the mean
    # is two hundred standard errors from zero.
    assert verdict(100, -2_000, 10_000, 100 * 400 + 100)[0] == "fail"
    # Close in the sum, and far too noisy to say: excesses of +50 and -50.
    assert verdict(100, 0, 10_000, 100 * 2_500)[0] == "inconclusive"
    # Not close, and not clearly apart from zero either: two trips of +30 and -10.
    assert verdict(2, 20, 100, 1_000)[0] == "inconclusive"
    # Exactly on the line: three standard errors equal to a tenth of the mean cost. The line is inside.
    on_it = {"mean_excess": [0, 1], "mean_cost": [30, 1], "variance_of_excess": [2, 1]}
    assert verdict(2, 0, 60, 2) == ("pass", on_it)


# ---- the command -----------------------------------------------------------------------------------------------------


def test_the_command_reads_a_journal_and_the_recorded_days_and_prints_the_comparison(tmp_path, capsys):
    stream = constant(600)
    days = recording(tmp_path / "rec", stream)
    control = ControlSleeve("control", seed=7, sizes={"EUR.USD": SIZE}, entry_per_million=50_000, hold_ns=SECOND,
                            retry_ns=SECOND)  # fmt: skip
    journal = tmp_path / "journal.jsonl"
    session(journal, [IdleSleeve(), control], stream)
    argv = ["--journal", str(journal), "--day", *map(str, days)]
    assert trip_costs.main(argv) == 0
    out, err = capsys.readouterr()
    said = json.loads(out)
    assert said["days"] == [day.name for day in days] and said["journal"] == str(journal) and "PLACEHOLDER" in err
    assert said["pairs"]["EUR.USD"]["verdict"] == said["verdict"] == "pass" and said["sleeve"] == "control"
    assert "." not in out.replace("EUR.USD", "").replace(str(journal), ""), "no float is printed"
    assert trip_costs.main(argv) == 0 and capsys.readouterr().out == out, "the same bytes every time"
    assert trip_costs.main([*argv, "--sleeve", "idle"]) == 0 and json.loads(capsys.readouterr().out)["pairs"] == {}


def test_the_command_refuses_what_it_cannot_compare_and_says_when_a_recording_is_not_the_journals(tmp_path, capsys):
    stream = book([108_505] * 20)
    days = recording(tmp_path / "rec", stream)
    journal = tmp_path / "journal.jsonl"
    session(journal, [Scripted({3: (BUY, SIZE), 8: (SELL, SIZE)})], stream)
    other = recording(tmp_path / "other", book([108_600] * 20))
    assert trip_costs.main(["--journal", str(journal), "--day", *map(str, other)]) == 1, "not the journal's recording"
    assert json.loads(capsys.readouterr().out)["pairs"]["EUR.USD"]["unmatched"] == 1
    assert trip_costs.main(["--journal", str(tmp_path / "none.jsonl"), "--day", *map(str, days)]) == 2
    assert trip_costs.main(["--journal", str(journal), "--day", str(tmp_path / "no-day")]) == 2
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert trip_costs.main(["--journal", str(empty), "--day", *map(str, days)]) == 2, "a journal with no session"
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(journal.read_bytes().replace(b'"price":108510', b'"price":108500', 1))
    assert trip_costs.main(["--journal", str(broken), "--day", *map(str, days)]) == 1
    assert "does not verify" in capsys.readouterr().err
    again = Shadow(TRADED, plain_costs(slippage_increments=1), [IdleSleeve()], Journal(journal))  # a second session
    again.close()
    assert trip_costs.main(["--journal", str(journal), "--day", *map(str, days)]) == 2
    assert "different cost models" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        trip_costs.main(["--journal", str(journal)])
