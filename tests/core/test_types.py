"""The domain types: immutable, integer-only, and a position that is exactly the sum of its fills."""
from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import (
    CASH_SCALE,
    Fill,
    InexactError,
    Instrument,
    InstrumentKind,
    Liquidity,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Position,
    Quote,
    Side,
    TimeInForce,
    to_scaled,
)

EURUSD = Instrument.from_pair("EUR.USD", price_scale=5, min_order_size=20_000, kind=InstrumentKind.CASH)
USDJPY = Instrument.from_pair("USD.JPY", price_scale=3, min_order_size=25_000, kind=InstrumentKind.CFD)

prices = st.integers(1, 10**9)
quantities = st.integers(1, 10**9)
times = st.integers(0, 2**63 - 1)
sides = st.sampled_from(Side)
pair_names = st.sampled_from(["EUR.USD", "USD.JPY", "GBP.USD", "AUD.USD", "EUR.JPY"])


def a_fill(**changes) -> Fill:
    base = {"fill_id": "x1", "client_order_id": "hft-control-00", "pair": "EUR.USD", "side": Side.BUY,
            "quantity": 20_000, "price": 108_512, "ts_ns": 1}  # fmt: skip
    return Fill(**{**base, **changes})


def an_order(**changes) -> Order:
    base = {"client_order_id": "hft-control-00", "sleeve": "control", "pair": "EUR.USD", "side": Side.BUY,
            "quantity": 20_000, "order_type": OrderType.LIMIT, "tif": TimeInForce.IOC, "created_ts_ns": 1,
            "limit_price": 108_512}  # fmt: skip
    return Order(**{**base, **changes})


def a_quote(**changes) -> Quote:
    base = {"pair": "EUR.USD", "bid": 108_510, "ask": 108_512, "bid_size": 1_000_000, "ask_size": 2_000_000,
            "recv_ts_ns": 1_760_000_000_000_000_000, "seq": 7}  # fmt: skip
    return Quote(**{**base, **changes})


fills = st.builds(
    lambda n, side, quantity, price, ts: a_fill(fill_id=f"x{n}", side=side, quantity=quantity, price=price, ts_ns=ts),
    st.integers(0, 10**6), sides, quantities, prices, times,
)  # fmt: skip


def booked(sequence) -> Position:
    position = Position("EUR.USD")
    for fill in sequence:
        position = position.apply(fill)
    return position


# ---- every type is immutable and compares by value -----------------------------------------------------------------

EXAMPLES = [EURUSD, a_quote(), an_order(), OrderEvent("hft-control-00", OrderState.WORKING, 1, 1), a_fill(),
            Position("EUR.USD", 5, -3)]  # fmt: skip


@pytest.mark.parametrize("value", EXAMPLES, ids=lambda v: type(v).__name__)
def test_the_types_are_frozen_slotted_hashable_values(value):
    field = dataclasses.fields(value)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, getattr(value, field))
    with pytest.raises((AttributeError, TypeError)):
        value.anything_else = 1
    assert not hasattr(value, "__dict__")
    assert dataclasses.replace(value) == value and hash(dataclasses.replace(value)) == hash(value)


NUMERIC_FIELDS = [
    (EURUSD, "price_scale"), (EURUSD, "min_order_size"),
    (a_quote(), "bid"), (a_quote(), "ask"), (a_quote(), "bid_size"), (a_quote(), "ask_size"),
    (a_quote(), "recv_ts_ns"), (a_quote(), "seq"),
    (an_order(), "quantity"), (an_order(), "limit_price"), (an_order(), "created_ts_ns"),
    (OrderEvent("a", OrderState.WORKING, 1, 1), "ts_ns"), (OrderEvent("a", OrderState.WORKING, 1, 1), "seq"),
    (OrderEvent("a", OrderState.WORKING, 1, 1), "filled_quantity"),
    (a_fill(), "quantity"), (a_fill(), "price"), (a_fill(), "ts_ns"), (a_fill(), "commission"),
    (Position("EUR.USD"), "quantity"), (Position("EUR.USD"), "quote_cash"),
]  # fmt: skip


# None is refused everywhere except as a market order's limit price, which has its own test.
WRONG_NUMBERS = [
    (value, field, wrong)
    for value, field in NUMERIC_FIELDS
    for wrong in (1.0, 108512.0, float("nan"), True, "1", None)
    if not (wrong is None and field == "limit_price")
]


@pytest.mark.parametrize(
    ("value", "field", "wrong"), WRONG_NUMBERS, ids=[f"{type(v).__name__}.{f}={w!r}" for v, f, w in WRONG_NUMBERS]
)
def test_no_numeric_field_takes_a_float_a_bool_or_text(value, field, wrong):
    with pytest.raises((TypeError, ValueError)):
        dataclasses.replace(value, **{field: wrong})


# ---- instrument ----------------------------------------------------------------------------------------------------


def test_an_instrument_knows_its_pair_and_its_precision():
    assert (EURUSD.base, EURUSD.quote, EURUSD.pair, EURUSD.price_scale) == ("EUR", "USD", "EUR.USD", 5)
    assert EURUSD.price("1.08512") == 108_512 and EURUSD.format_price(108_512) == "1.08512"
    assert USDJPY.price("151.234") == 151_234 and USDJPY.format_price(151_234) == "151.234"
    assert USDJPY.kind is InstrumentKind.CFD


@given(pair_names, st.integers(0, 9), quantities, st.sampled_from(InstrumentKind))
def test_an_instrument_round_trips_its_pair(pair, scale, minimum, kind):
    instrument = Instrument.from_pair(pair, price_scale=scale, min_order_size=minimum, kind=kind)
    assert instrument.pair == pair
    assert instrument == Instrument(pair[:3], pair[4:], scale, minimum, kind)


@given(prices)
def test_a_price_round_trips_through_the_instruments_text_form(price):
    for instrument in (EURUSD, USDJPY):
        assert instrument.price(instrument.format_price(price)) == price


def test_an_instrument_carries_the_venues_increments():
    half_pip = Instrument.from_pair(
        "EUR.USD", price_scale=5, min_order_size=20_000, kind=InstrumentKind.CASH, size_increment=1_000,
        price_increment=5,
    )  # fmt: skip
    assert (half_pip.size_increment, half_pip.price_increment) == (1_000, 5)
    assert (EURUSD.size_increment, EURUSD.price_increment) == (1, 1)
    assert half_pip.refusals(20_000, 108_510) == [] and half_pip.refusals(25_000, None) == []
    assert half_pip.refusals(19_000, 108_510) == ["quantity 19000 is below the minimum order size 20000"]
    assert half_pip.refusals(20_500, 108_510) == ["quantity 20500 is not a multiple of the size increment 1000"]
    assert half_pip.refusals(20_000, 108_512) == ["limit price 108512 is not a multiple of the price increment 5"]
    assert len(half_pip.refusals(500, 108_512)) == 3


@given(quantities, prices, st.integers(1, 10**6), st.integers(1, 10**4), st.integers(1, 100))
def test_an_order_is_accepted_exactly_when_it_respects_the_minimum_and_both_increments(
    quantity, price, minimum, size_step, price_step
):
    instrument = Instrument.from_pair(
        "EUR.USD", price_scale=5, min_order_size=minimum, kind=InstrumentKind.CASH, size_increment=size_step,
        price_increment=price_step,
    )  # fmt: skip
    fits = quantity >= minimum and quantity % size_step == 0 and price % price_step == 0
    assert (instrument.refusals(quantity, price) == []) == fits


def test_a_price_finer_than_the_instrument_quotes_is_refused():
    with pytest.raises(InexactError):
        EURUSD.price("1.085125")
    with pytest.raises(InexactError):
        USDJPY.price("151.2345")


@pytest.mark.parametrize("value", [108_512, 1.08512, True])
def test_an_instrument_price_comes_from_decimal_text_only(value):
    with pytest.raises(TypeError):
        EURUSD.price(value)


@pytest.mark.parametrize(
    "pair", ["EURUSD", "eur.usd", "EUR/USD", "EUR.EUR", "EU.USD", "EUR.USDX", "EUR.USD ", "", None, 5]
)
def test_a_malformed_pair_is_refused(pair):
    with pytest.raises(ValueError):
        Instrument.from_pair(pair, price_scale=5, min_order_size=1, kind=InstrumentKind.CASH)


@pytest.mark.parametrize(
    "changes",
    [{"price_scale": -1}, {"price_scale": 10}, {"min_order_size": 0}, {"min_order_size": -5}, {"base": "eur"},
     {"quote": "EUR"}, {"kind": "CASH"}, {"kind": None}, {"size_increment": 0}, {"price_increment": 0},
     {"size_increment": 1.0}, {"price_increment": True}],
)  # fmt: skip
def test_an_invalid_instrument_is_refused(changes):
    with pytest.raises((TypeError, ValueError)):
        dataclasses.replace(EURUSD, **changes)


# ---- quote ---------------------------------------------------------------------------------------------------------


@given(prices, prices, st.integers(0, 10**12), st.integers(0, 10**12), times, st.integers(0, 2**62))
def test_a_quote_is_integers_and_its_arithmetic_is_exact(bid, ask, bid_size, ask_size, ts, seq):
    quote = Quote("EUR.USD", bid, ask, bid_size, ask_size, ts, seq)
    assert quote.spread == ask - bid and quote.mid_x2 == bid + ask
    assert quote.is_crossed == (bid > ask)
    assert all(type(getattr(quote, f.name)) is int for f in dataclasses.fields(quote) if f.name != "pair")


def test_a_crossed_quote_is_kept_as_received():
    quote = a_quote(bid=108_515, ask=108_512)
    assert quote.is_crossed and quote.spread == -3


@pytest.mark.parametrize(
    "changes",
    [{"bid": 0}, {"ask": 0}, {"bid": -1}, {"bid_size": -1}, {"ask_size": -1}, {"recv_ts_ns": -1}, {"seq": -1},
     {"pair": "EURUSD"}],
)  # fmt: skip
def test_an_invalid_quote_is_refused(changes):
    with pytest.raises(ValueError):
        a_quote(**changes)


# ---- order and its events ------------------------------------------------------------------------------------------


def test_a_limit_order_needs_a_price_and_a_market_order_has_none():
    assert an_order().limit_price == 108_512
    assert an_order(order_type=OrderType.MARKET, limit_price=None).limit_price is None
    with pytest.raises(ValueError):
        an_order(limit_price=None)
    with pytest.raises(ValueError):
        an_order(order_type=OrderType.MARKET)
    with pytest.raises(ValueError):
        an_order(limit_price=0)


@pytest.mark.parametrize(
    "changes",
    [{"quantity": 0}, {"quantity": -1}, {"side": "BUY"}, {"order_type": "LIMIT"}, {"tif": "IOC"}, {"pair": "EURUSD"},
     {"client_order_id": ""}, {"client_order_id": "has space"}, {"client_order_id": "line\nbreak"}, {"sleeve": ""},
     {"created_ts_ns": -1}],
)  # fmt: skip
def test_an_invalid_order_is_refused(changes):
    with pytest.raises((TypeError, ValueError)):
        an_order(**changes)


def test_the_vocabularies_are_exactly_these():
    assert {m.value for m in Side} == {"BUY", "SELL"}
    assert {m.value for m in OrderType} == {"MARKET", "LIMIT"}
    assert {m.value for m in TimeInForce} == {"DAY", "GTC", "IOC", "FOK"}
    assert {m.value for m in Liquidity} == {"MAKER", "TAKER", "UNKNOWN"}
    assert {m.value for m in InstrumentKind} == {"CASH", "CFD"}
    assert {m.value for m in OrderState} == {
        "NEW", "SUBMITTED", "WORKING", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED",
    }  # fmt: skip


def test_exactly_four_order_states_are_terminal():
    assert {s for s in OrderState if s.is_terminal} == {
        OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED,
    }  # fmt: skip


def test_a_side_has_a_sign_and_an_opposite():
    assert (Side.BUY.sign, Side.SELL.sign) == (1, -1)
    assert all(side.opposite.opposite is side and side.opposite.sign == -side.sign for side in Side)


def test_an_order_event_carries_cumulative_fill_and_a_filled_event_needs_one():
    event = OrderEvent("hft-control-00", OrderState.PARTIALLY_FILLED, 5, 2, filled_quantity=10_000, broker_order_id="7")
    assert event.filled_quantity == 10_000 and not event.state.is_terminal
    with pytest.raises(ValueError):
        OrderEvent("hft-control-00", OrderState.FILLED, 5, 2)
    with pytest.raises(TypeError):
        OrderEvent("hft-control-00", "FILLED", 5, 2, filled_quantity=1)
    with pytest.raises(TypeError):
        OrderEvent("hft-control-00", OrderState.REJECTED, 5, 2, reason=404)


# ---- fill ----------------------------------------------------------------------------------------------------------


def test_a_fill_is_signed_by_its_side():
    assert a_fill(side=Side.BUY).signed_quantity == 20_000
    assert a_fill(side=Side.SELL).signed_quantity == -20_000


def test_a_fill_may_carry_the_venues_own_time_and_a_sequence_number():
    fill = a_fill(ts_ns=1_760_000_000_500_000_000, venue_ts_ns=1_760_000_000_000_000_000, seq=41)
    assert fill.venue_ts_ns < fill.ts_ns and fill.seq == 41
    assert a_fill().venue_ts_ns is None and a_fill().seq == 0
    for wrong in ({"venue_ts_ns": -1}, {"seq": -1}, {"venue_ts_ns": 1.0}, {"seq": True}):
        with pytest.raises((TypeError, ValueError)):
            a_fill(**wrong)


def test_a_commission_is_integer_millionths_and_needs_its_currency():
    commission = to_scaled("2.00", CASH_SCALE)
    assert a_fill(commission=commission, commission_currency="USD").commission == 2_000_000
    assert a_fill(commission=-150, commission_currency="USD").commission == -150  # a rebate
    with pytest.raises(ValueError):
        a_fill(commission=commission)
    with pytest.raises(ValueError):
        a_fill(commission=commission, commission_currency="usd")


@pytest.mark.parametrize(
    "changes", [{"quantity": 0}, {"price": 0}, {"price": -5}, {"fill_id": ""}, {"side": 1}, {"liquidity": "MAKER"},
                {"ts_ns": -1}, {"pair": "EUR"}],
)  # fmt: skip
def test_an_invalid_fill_is_refused(changes):
    with pytest.raises((TypeError, ValueError)):
        a_fill(**changes)


# ---- position: the accounting invariants ---------------------------------------------------------------------------


@given(st.lists(fills, max_size=30))
def test_a_position_is_the_sum_of_its_fills(sequence):
    position = booked(sequence)
    assert position.quantity == sum(f.signed_quantity for f in sequence)
    assert position.quote_cash == -sum(f.signed_quantity * f.price for f in sequence)


@given(st.lists(fills, max_size=12).flatmap(lambda fs: st.tuples(st.just(fs), st.permutations(fs))))
def test_the_order_of_booking_does_not_matter(both):
    sequence, shuffled = both
    assert booked(sequence) == booked(shuffled)


@given(quantities, prices, prices, sides)
def test_a_round_trip_ends_flat_with_the_price_difference_as_profit(quantity, entry, exit_, side):
    position = booked([a_fill(side=side, quantity=quantity, price=entry),
                       a_fill(fill_id="x2", side=side.opposite, quantity=quantity, price=exit_)])  # fmt: skip
    assert position.is_flat
    assert position.quote_cash == side.sign * quantity * (exit_ - entry)
    assert position.marked(1) == position.marked(10**9) == position.quote_cash  # flat: the mark is irrelevant


@given(st.lists(fills, max_size=30), prices)
def test_marking_is_each_fills_profit_against_the_mark(sequence, mark):
    assert booked(sequence).marked(mark) == sum(f.signed_quantity * (mark - f.price) for f in sequence)


@given(quantities, quantities, prices, sides)
def test_one_fill_or_two_partial_fills_at_one_price_book_the_same(first, second, price, side):
    whole = booked([a_fill(side=side, quantity=first + second, price=price)])
    one = a_fill(side=side, quantity=first, price=price)
    two = a_fill(fill_id="x2", side=side, quantity=second, price=price)
    parts = booked([one, two])
    assert whole == parts


@given(st.lists(fills, max_size=10), fills)
def test_applying_a_fill_leaves_the_earlier_position_untouched(sequence, fill):
    before = booked(sequence)
    snapshot = dataclasses.replace(before)
    after = before.apply(fill)
    assert before == snapshot and after is not before
    assert after.quantity - before.quantity == fill.signed_quantity


def test_a_fill_in_another_pair_cannot_be_booked():
    with pytest.raises(ValueError):
        Position("USD.JPY").apply(a_fill())


def test_a_position_starts_flat_and_a_mark_must_be_a_price():
    assert Position("EUR.USD").is_flat and Position("EUR.USD").marked(108_512) == 0
    with pytest.raises(ValueError):
        Position("EUR.USD", 1, -1).marked(0)
    with pytest.raises(TypeError):
        Position("EUR.USD", 1, -1).marked(1.08512)
