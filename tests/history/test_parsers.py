"""Each layout has its own strict parser: it splits a line into the fields the layout names, or refuses the file."""
from __future__ import annotations

import pytest

from hftlab.history.describe import Description
from hftlab.history.parsers import PARSERS, Raw, Refused, generic, local_bid_ask, utc_ask_bid
from tests.history.kit import SIZE_COLUMNS
from tests.venues.kit import EURUSD


def description(layout: str = "generic", **more) -> Description:
    return Description(layout, "UTC", more.pop("header", True), {"EUR.USD": EURUSD}, (), **more)


def lines(text: str) -> list[bytes]:
    return text.encode("ascii").splitlines(keepends=True)


# ---- generic ---------------------------------------------------------------------------------------------------------


def test_generic_finds_its_columns_by_header_name_whatever_their_order():
    said = description(columns=SIZE_COLUMNS)
    text = "AskSize,Ask,Other,Bid,Time,BidSize\n2,1.08510,x,1.08500,t1,1.5\n3,1.08511,y,1.08501,t2,0.5\n"
    assert list(generic(lines(text), said)) == [
        Raw(2, "t1", "1.08500", "1.08510", "1.5", "2"),
        Raw(3, "t2", "1.08501", "1.08511", "0.5", "3"),
    ]


def test_generic_finds_its_columns_by_position_when_there_is_no_header_and_has_no_sizes_unless_they_are_named():
    said = description(header=False, delimiter=";", columns={"time": 1, "bid": 3, "ask": 2})
    assert list(generic(lines("t1;1.08510;1.08500\nt2;1.08511;1.08501"), said)) == [
        Raw(1, "t1", "1.08500", "1.08510", None, None),
        Raw(2, "t2", "1.08501", "1.08511", None, None),
    ]


@pytest.mark.parametrize(
    ("text", "line", "why"),
    [
        ("Time,Bid\nt,1\n", 1, "no column Ask"),
        ("Time,Bid,Ask,Bid\nt,1,2,1\n", 1, "twice"),
        ("Time,Bid,Ask\nt,1\n", 2, "2 fields"),
        ("Time,Bid,Ask\nt,1,2,3\n", 2, "4 fields"),
        ("Time,Bid,Ask\nt,1,2\n\nt,1,2\n", 3, "empty line"),
        ('Time,Bid,Ask\n"t",1,2\n', 2, "quote"),
        ("Time,Bid,Ask\n", 1, "no row"),
        ("", 1, "no row"),
    ],
)
def test_generic_refuses_a_file_that_is_not_shaped_as_described(text, line, why):
    with pytest.raises(Refused, match=why) as refused:
        list(generic(lines(text), description(columns={"time": "Time", "bid": "Bid", "ask": "Ask"})))
    assert refused.value.line == line


def test_a_byte_order_mark_is_stepped_over_and_any_other_byte_outside_ascii_is_refused():
    said = description(columns={"time": "Time", "bid": "Bid", "ask": "Ask"})
    marked = b"\xef\xbb\xbfTime,Bid,Ask\r\nt,1,2\r\n"
    assert list(generic(marked.splitlines(keepends=True), said)) == [Raw(2, "t", "1", "2")]
    with pytest.raises(Refused, match="not ASCII"):
        list(generic([b"Time,Bid,Ask\n", b"t,1,2\xc3\xa9\n"], said))


# ---- time in UTC, ask, bid, ask size, bid size -----------------------------------------------------------------------


def test_utc_ask_bid_reads_five_columns_and_puts_the_bid_and_the_ask_the_right_way_round():
    text = "a,b,c,d,e\n14.10.2026 03:00:00.000,1.08510,1.08500,2,1.5\n"
    assert list(utc_ask_bid(lines(text), description("utc_ask_bid"))) == [
        Raw(2, "14.10.2026 03:00:00.000", "1.08500", "1.08510", "1.5", "2")
    ]
    for wrong in ("a,b,c,d,e\nt,1,2,3\n", "a,b,c,d,e\nt,1,2,3,4,5\n", "a,b,c,d\nt,1,2,3,4\n"):
        with pytest.raises(Refused, match="five"):
            list(utc_ask_bid(lines(wrong), description("utc_ask_bid")))


# ---- wall-clock time, bid, ask ---------------------------------------------------------------------------------------


def test_local_bid_ask_reads_three_columns_and_never_takes_a_fourth_for_a_size():
    said = description("local_bid_ask", header=False)
    assert list(local_bid_ask(lines("20261014 030000000,1.08500,1.08510,0\n"), said)) == [
        Raw(1, "20261014 030000000", "1.08500", "1.08510", None, None)
    ]
    assert list(local_bid_ask(lines("t,1,2\nu,3,4\n"), said)) == [Raw(1, "t", "1", "2"), Raw(2, "u", "3", "4")]
    for wrong in ("t,1\n", "t,1,2,3,4\n", "t,1,2\nt,1,2,0\n"):
        with pytest.raises(Refused, match="three or four"):
            list(local_bid_ask(lines(wrong), said))


def test_every_parser_has_a_name_and_a_version_and_the_fixed_layouts_carry_their_own_time_format():
    assert {name: (parser.name, parser.version) for name, parser in PARSERS.items()} == {
        "generic": ("generic", 1), "utc_ask_bid": ("utc_ask_bid", 1), "local_bid_ask": ("local_bid_ask", 1),
    }  # fmt: skip
    assert PARSERS["generic"].time_format == ""
    assert all(PARSERS[name].time_format for name in PARSERS if name != "generic")
