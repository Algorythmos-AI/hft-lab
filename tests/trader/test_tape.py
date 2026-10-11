"""The tape and the slippage arithmetic: exact, and the same after a trip through a file."""
from __future__ import annotations

import json

import pytest

from hftlab.core import Fill, Liquidity, OrderEvent, OrderState, Quote, Side
from hftlab.trader import TapeError
from hftlab.trader.slippage import Matched, difference, report
from hftlab.trader.tape import Request, decode, encode, read_requests, read_tape
from hftlab.venues.port import CommissionEvent, ConnectionEvent, ConnectionState, DepthSnapshot, VenueNotice
from tests.venues.kit import T0

BUY, SELL = Side.BUY, Side.SELL
EVENTS = [
    Quote("EUR.USD", 108_500, 108_510, 1_000_000, 2_000_000, T0, 7),
    ConnectionEvent(ConnectionState.RECONCILING, T0, 8, "a reason"),
    OrderEvent("hft-control-0", OrderState.PARTIALLY_FILLED, T0, 9, 10_000, "B1", None),
    Fill("x1", "hft-control-0", "EUR.USD", SELL, 10_000, 108_500, T0, Liquidity.TAKER, 2_000_000, "USD", T0 - 5, 10),
    Fill("x2", "hft-control-0", "EUR.USD", BUY, 10_000, 108_510, T0),
    CommissionEvent("x1", 2_000_000, "USD", T0, 11),
    VenueNotice("cancel-too-late", "the order was already done", T0, 12, "hft-control-0"),
]


@pytest.mark.parametrize("event", EVENTS, ids=lambda event: type(event).__name__)
def test_an_event_comes_back_from_the_tape_as_it_went_in_and_holds_no_float(event):
    row = encode(event)
    assert decode(json.loads(json.dumps(row))) == event
    assert not any(isinstance(value, float) for value in row)


def test_an_event_the_trader_has_no_use_for_is_not_recorded_and_an_unknown_row_is_refused():
    assert encode(DepthSnapshot("EUR.USD", ((1, 1),), ((2, 1),), T0, 1)) is None
    with pytest.raises(TapeError, match="nobody wrote"):
        decode(["z", 1])


def test_a_tape_is_read_without_the_line_a_dying_process_left_half_written(tmp_path):
    tape = tmp_path / "t.jsonl"
    tape.write_bytes(b'["open",1,"fake",[],"READY"]\n["marks",{}]\n["step",2,"READY",[],null]\n["ste')
    assert [row[0] for row in read_tape(tape)] == ["open", "marks", "step"]
    tape.write_bytes(b'["step",2,"READY",[],null]\n')
    with pytest.raises(TapeError, match="does not begin as a tape does"):
        read_tape(tape)
    tape.write_bytes(b'["open",1,"fake",[],"READY"]\nnot json\n["marks",{}]\n')
    with pytest.raises(TapeError, match="line 2 is not a tape line"):
        read_tape(tape)


def test_requests_are_taken_once_in_order_and_a_file_that_is_not_one_is_left_alone(tmp_path):
    (tmp_path / "0002-stage.json").write_text('{"what":"stage","subject":"control","value":"off","operator":"sam"}')
    (tmp_path / "0001-reset.json").write_text('{"what":"reset","subject":"ORDER_FAULT","value":"","operator":"sam"}')
    (tmp_path / "0003-odd.json").write_text('{"what":"reset"}')
    (tmp_path / "notes.txt").write_text("not a request")
    assert read_requests(tmp_path) == (
        Request("reset", "ORDER_FAULT", "", "sam"), Request("stage", "control", "off", "sam"),
    )  # fmt: skip
    assert sorted(path.name for path in tmp_path.iterdir()) == ["0003-odd.json", "notes.txt"]
    assert read_requests(tmp_path) == () and read_requests(tmp_path / "missing") == ()


# ---- slippage --------------------------------------------------------------------------------------------------------


def order(side: Side, broker, shadow, pair: str = "EUR.USD") -> Matched:
    return Matched("control", pair, side, 5, tuple(broker), tuple(shadow))


def test_the_difference_is_positive_when_the_shadow_venue_filled_worse_whichever_way_the_order_pointed():
    assert difference(order(BUY, [(20_000, 108_510)], [(20_000, 108_520)])) == (2, True), "paid more"
    assert difference(order(SELL, [(20_000, 108_500)], [(20_000, 108_490)])) == (2, True), "received less"
    assert difference(order(BUY, [(20_000, 108_510)], [(20_000, 108_505)])) == (-1, True), "the model was kinder"
    assert difference(order(BUY, [(20_000, 108_510)], [(20_000, 108_510)])) == (0, True)


def test_an_order_filled_in_pieces_is_compared_on_what_it_paid_and_rounded_towards_the_kinder_side():
    pieces = [(10_000, 108_510), (10_000, 108_515)]
    assert difference(order(BUY, [(20_000, 108_510)], pieces)) == (0, False), "half an increment worse, rounded down"
    assert difference(order(BUY, pieces, [(20_000, 108_510)])) == (-1, False), "half an increment better, rounded down"
    assert difference(order(BUY, [(20_000, 108_510)], [(10_000, 108_510)])) is None, "different quantities"
    assert difference(order(BUY, [], [])) is None


def test_the_report_has_one_line_a_sleeve_and_pair_and_counts_what_it_could_not_compare():
    lines = report([
        order(BUY, [(20_000, 108_510)], [(20_000, 108_520)]),
        order(SELL, [(20_000, 108_500)], [(20_000, 108_505)]),
        order(BUY, [(20_000, 108_510)], [(20_000, 108_525)]),
        order(BUY, [(20_000, 108_510)], []),
        order(SELL, [], [(20_000, 108_500)]),
        order(BUY, [(20_000, 108_510)], [(10_000, 108_510)]),
        order(BUY, [], []),
        order(BUY, [(20_000, 151_240)], [(20_000, 151_240)], pair="USD.JPY"),
    ])  # fmt: skip
    assert lines == [
        {"sleeve": "control", "pair": "EUR.USD", "count": 3, "sum_increments": 4, "lowest_increments": -1,
         "highest_increments": 3, "inexact": 0, "broker_only": 1, "shadow_only": 1, "unequal": 1,
         "mean_increments": 1},
        {"sleeve": "control", "pair": "USD.JPY", "count": 1, "sum_increments": 0, "lowest_increments": 0,
         "highest_increments": 0, "inexact": 0, "broker_only": 0, "shadow_only": 0, "unequal": 0, "mean_increments": 0},
    ]  # fmt: skip
    assert report([]) == []
