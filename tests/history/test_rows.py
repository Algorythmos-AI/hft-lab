"""Whatever is wrong with a row has one stated answer: the file is refused, or the row is dropped and counted."""
from __future__ import annotations

import pytest

from hftlab.history.importer import ImportRefused
from hftlab.history.rows import DROPPED, PLACEHOLDER_SIZE
from hftlab.replay import events
from tests.history.kit import SIZE_COLUMNS, a_folder, run

HEAD = "Time,Bid,Ask\n"
DAY = "2026-10-14"


def row(time: str, bid: str = "1.08500", ask: str = "1.08510", *more: str) -> str:
    return ",".join([f"{DAY} {time}", bid, ask, *more]) + "\n"


def imported(tmp_path, text: str, **changes):
    report = run(tmp_path / "store", a_folder(tmp_path / "in", {"ticks.csv": text}, **changes)).as_dict()
    day = tmp_path / "store" / DAY
    return report, list(events(day)) if day.is_dir() else []


def test_a_good_row_is_kept_to_the_unit_and_a_file_without_sizes_gets_the_placeholder_on_both_sides(tmp_path):
    report, quotes = imported(tmp_path, HEAD + row("03:00:00.000") + row("03:00:01.5", "1.08501", "1.08512"))
    assert [(q.bid, q.ask, q.bid_size, q.ask_size) for q in quotes] == [
        (108_500, 108_510, PLACEHOLDER_SIZE, PLACEHOLDER_SIZE), (108_501, 108_512, PLACEHOLDER_SIZE, PLACEHOLDER_SIZE),
    ]  # fmt: skip
    assert quotes[1].recv_ts_ns - quotes[0].recv_ts_ns == 1_500_000_000
    assert report["rows"] == {"read": 2, "kept": 2} and report["files"][0]["dropped"] == {}
    assert PLACEHOLDER_SIZE == 1


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (row("03:00:01.000", "1.085001"), "price_unfit"),  # a sixth decimal on a pair quoted to five
        (row("03:00:01.000", "0.00000"), "price_unfit"),
        (row("03:00:01.000", "1.08500", "-1.08510"), "price_unfit"),
        (row("03:00:01.000", "1.08520", "1.08510"), "crossed"),
        (row("02:59:59.000", "1.08400", "1.08410"), "time_goes_back"),
        (row("03:00:00.000"), "duplicate"),
    ],
)
def test_a_row_that_is_written_correctly_and_cannot_be_used_is_dropped_and_counted_by_its_reason(tmp_path, bad, reason):
    report, quotes = imported(tmp_path, HEAD + row("03:00:00.000") + bad + row("03:00:02.000", "1.08502", "1.08512"))
    assert reason in DROPPED and report["files"][0]["dropped"] == {reason: 1}
    assert report["rows"] == {"read": 3, "kept": 2} and [q.bid for q in quotes] == [108_500, 108_502]
    assert report["days"][0]["dropped"] == {reason: 1}


def test_a_dropped_row_is_never_repaired_or_moved_and_the_day_s_journal_says_what_went(tmp_path):
    text = HEAD + row("03:00:00.000") + row("03:00:01.000", "1.085004999") + row("02:00:00.000") + row("03:00:00.000")
    report, quotes = imported(tmp_path, text)
    assert [(q.bid, q.recv_ts_ns % 3_600_000_000_000) for q in quotes] == [(108_500, 0)]  # not rounded, not re-sorted
    journal = (tmp_path / "store" / DAY / "journal.jsonl").read_text()
    for reason, rows in (("price_unfit", 1), ("time_goes_back", 2)):
        assert f'"file": "ticks.csv", "reason": "{reason}", "rows": {rows}, "type": "dropped"' in journal
    assert report["files"][0]["dropped"] == {"price_unfit": 1, "time_goes_back": 2}


def test_a_row_while_the_market_is_shut_is_dropped_and_a_day_with_nothing_kept_gets_no_folder(tmp_path):
    saturday = "2026-10-17 12:00:00.000,1.08500,1.08510\n"
    report, quotes = imported(tmp_path, HEAD + row("03:00:00.000") + saturday)
    assert len(quotes) == 1 and report["files"][0]["dropped"] == {"market_closed": 1}
    assert [(day["day"], day["status"]) for day in report["days"]] == [(DAY, "written"), ("2026-10-17", "nothing_kept")]
    assert not (tmp_path / "store" / "2026-10-17").exists()


def test_a_wall_clock_time_in_a_repeated_hour_is_dropped_because_the_file_does_not_say_which_one_it_is(tmp_path):
    """New York repeats 01:00 to 02:00 on Sunday 2026-11-01; the Wednesday rows around it are kept."""
    text = HEAD + "2026-10-28 12:00:00.000,1.08500,1.08510\n2026-11-01 01:30:00.000,1.08500,1.08510\n"
    report, _ = imported(tmp_path, text + "2026-11-04 12:00:00.000,1.08500,1.08510\n", time_zone="America/New_York")
    assert report["files"][0]["dropped"] == {"time_not_one_instant": 1} and report["rows"]["kept"] == 2


def test_a_size_is_read_in_the_stated_unit_exactly_and_one_that_is_not_whole_units_is_dropped(tmp_path):
    head = "Time,Bid,Ask,BidSize,AskSize\n"
    prices = ("1.08500", "1.08510")
    text = head + row("03:00:00.000", *prices, "1.5", "2") + row("03:00:01.000", *prices, "0.0000001", "1")
    text += row("03:00:02.000", *prices, "0", "0.75") + row("03:00:03.000", *prices, "-1", "1")
    report, quotes = imported(tmp_path, text, columns=SIZE_COLUMNS, size_unit="1000000")
    assert [(q.bid_size, q.ask_size) for q in quotes] == [(1_500_000, 2_000_000), (0, 750_000)]
    assert report["files"][0]["dropped"] == {"size_unfit": 2} and report["days"][0]["sizes"] == "stated"


def test_a_locked_quote_and_a_price_off_this_venue_s_grid_are_kept_and_counted(tmp_path):
    text = HEAD + row("03:00:00.000", "1.08500", "1.08500") + row("03:00:01.000", "1.08501", "1.08512")
    report, quotes = imported(tmp_path, text)
    assert len(quotes) == 2 and report["files"][0]["counted"] == {"locked": 1, "off_increment": 1}


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        (row("03:00:01.000", "1.085e0"), "not a plain decimal number"),
        (row("03:00:01.000", "1,08500").replace("1,08500", "abc"), "not a plain decimal number"),
        (row("03:00:01"), "is not written as"),
        ("2026-10-14 03:00:01.000,1.08500\n", "2 fields"),
        (row("03:00:01.000", " 1.08500"), "not a plain decimal number"),
    ],
)
def test_a_row_that_is_not_written_as_described_refuses_the_whole_file_and_nothing_is_imported(tmp_path, bad, why):
    with pytest.raises(ImportRefused, match=f"ticks.csv: line 3: .*{why}"):
        imported(tmp_path, HEAD + row("03:00:00.000") + bad + row("03:00:02.000"))
    assert not list((tmp_path / "store").glob("20*")) and not (tmp_path / "store" / ".staging").exists()
