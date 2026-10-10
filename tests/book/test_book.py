"""The book's invariants, for any sequence of fills: cash conserved, positions are sums, nothing booked twice."""
from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.book import DESK, UNATTRIBUTED, Book, BookError, PnL
from hftlab.core import Fill, Position, Side, client_order_id
from hftlab.venues.port import CommissionEvent
from tests.venues.kit import EURUSD, USDJPY

SLEEVES = ("control", "ml")


def a_fill(number: int, side: Side, quantity: int, price: int, *, sleeve="control", pair="EUR.USD", **more) -> Fill:
    order = client_order_id(sleeve=sleeve, pair=pair, side=side, decision_seq=number)
    return Fill(f"x{number}", order, pair, side, quantity, price, number, **more)


fills = st.lists(
    st.builds(
        lambda side, quantity, price, sleeve: (side, quantity, price, sleeve),
        st.sampled_from(Side), st.integers(1, 10**6), st.integers(1, 10**6), st.sampled_from(SLEEVES),
    ),
    max_size=30,
).map(lambda rows: [a_fill(n, s, q, p, sleeve=sl) for n, (s, q, p, sl) in enumerate(rows)])  # fmt: skip


def booked(sequence, fees=()) -> Book:
    book = Book([EURUSD, USDJPY])
    for fill in sequence:
        book.apply(fill)
    for fee in fees:
        book.charge(fee)
    return book


# ---- the invariants -------------------------------------------------------------------------------------------------


@given(fills)
def test_a_position_is_the_sum_of_its_fills_per_sleeve_and_for_the_desk(sequence):
    book = booked(sequence)
    for sleeve in SLEEVES:
        mine = [f for f in sequence if f.client_order_id.startswith(f"hft-{sleeve}-")]
        position = book.position(sleeve, "EUR.USD")
        assert position.quantity == sum(f.signed_quantity for f in mine)
        assert position.quote_cash == -sum(f.signed_quantity * f.price for f in mine)
    desk = book.position(DESK, "EUR.USD")
    assert desk.quantity == sum(book.position(s, "EUR.USD").quantity for s in SLEEVES)
    assert desk.quote_cash == sum(book.position(s, "EUR.USD").quote_cash for s in SLEEVES), "cash is conserved"


@given(fills.flatmap(lambda fs: st.tuples(st.just(fs), st.permutations(fs))))
def test_positions_and_cash_do_not_depend_on_the_order_of_booking(both):
    sequence, shuffled = both
    one, other = booked(sequence), booked(shuffled)
    for sleeve in (*SLEEVES, DESK):
        assert one.position(sleeve, "EUR.USD") == other.position(sleeve, "EUR.USD")


@given(fills, st.integers(1, 10**6))
def test_realised_plus_unrealised_is_the_cash_plus_the_position_at_the_mark(sequence, mark):
    book = booked(sequence)
    for sleeve in (*SLEEVES, DESK):
        pnl = book.pnl(sleeve, "EUR.USD", mark, mark)
        if sleeve != DESK:
            assert pnl.realised + pnl.unrealised == book.position(sleeve, "EUR.USD").marked(mark)
        assert pnl.net_micro == (pnl.realised + pnl.unrealised) * 10 - pnl.fees_micro
    desk = book.pnl(DESK, "EUR.USD", mark, mark)
    parts = [book.pnl(sleeve, "EUR.USD", mark, mark) for sleeve in SLEEVES]
    assert (desk.realised, desk.unrealised) == (sum(p.realised for p in parts), sum(p.unrealised for p in parts))


@given(fills, st.data())
def test_booking_a_fill_twice_changes_nothing(sequence, data):
    book = booked(sequence)
    before = book.snapshot()
    for fill in data.draw(st.lists(st.sampled_from(sequence), max_size=10)) if sequence else []:
        assert book.apply(fill) is False
    assert book.snapshot() == before


@given(fills, st.data())
def test_fees_are_counted_once_each_and_never_lost(sequence, data):
    amounts = [data.draw(st.integers(0, 10**7)) for _ in sequence]
    events = [CommissionEvent(f.fill_id, amount, "USD", 0, 0) for f, amount in zip(sequence, amounts, strict=True)]
    arrivals = data.draw(st.permutations(events + events[: len(events) // 2]))  # some arrive twice, in any order
    split = data.draw(st.integers(0, len(arrivals)))
    book = Book([EURUSD])
    for event in arrivals[:split]:  # some commissions arrive before their fills
        book.charge(event)
    for fill in sequence:
        book.apply(fill)
    for event in arrivals[split:]:
        book.charge(event)
    assert book.fees().get("USD", 0) == sum(amounts)
    assert sum(book.fees(sleeve).get("USD", 0) for sleeve in SLEEVES) == sum(amounts)
    assert book.pnl(DESK, "EUR.USD", 1, 1).fees_micro == sum(amounts)


@given(fills)
def test_a_flat_sleeve_has_no_unrealised_profit_and_its_cash_is_its_realised_profit(sequence):
    book = booked(sequence)
    for number, sleeve in enumerate(SLEEVES, 10_000):
        position = book.position(sleeve, "EUR.USD")
        side = Side.SELL if position.quantity > 0 else Side.BUY
        if position.quantity:
            book.apply(a_fill(number, side, abs(position.quantity), 500, sleeve=sleeve))
        flat = book.position(sleeve, "EUR.USD")
        if flat.is_flat:
            pnl = book.pnl(sleeve, "EUR.USD", 1, 10**6)
            assert pnl.unrealised == 0 and pnl.realised == flat.quote_cash


# ---- worked examples ------------------------------------------------------------------------------------------------


def test_a_round_trip_by_hand():
    book = booked([a_fill(1, Side.BUY, 20_000, 108_510), a_fill(2, Side.SELL, 20_000, 108_530)])
    book.charge(CommissionEvent("x1", 2_000_000, "USD", 0, 0))
    book.charge(CommissionEvent("x2", 2_000_000, "USD", 0, 0))
    pnl = book.pnl("control", "EUR.USD", 108_500, 108_510)
    assert (pnl.realised, pnl.unrealised) == (20_000 * 20, 0), "20 price units on 20,000 units: 4.00 USD at scale 5"
    assert pnl.fees_micro == 4_000_000 and pnl.net_micro == 4_000_000 - 4_000_000


def test_a_fee_in_another_currency_is_not_netted_not_converted_and_not_hidden():
    yen = a_fill(3, Side.BUY, 20_000, 151_230, pair="USD.JPY")
    book = booked([a_fill(1, Side.BUY, 20_000, 108_510), a_fill(2, Side.SELL, 20_000, 108_530), yen])
    book.charge(CommissionEvent("x1", 3_000_000, "AUD", 0, 0))  # as a broker charges it: in the account's currency
    book.charge(CommissionEvent("x2", 2_000_000, "USD", 0, 0))
    book.charge(CommissionEvent("x3", 0, "AUD", 0, 0))
    pnl = book.pnl("control", "EUR.USD", 108_500, 108_510)
    assert pnl.fees_micro == 2_000_000 and pnl.fees_not_netted_micro == {"AUD": 3_000_000}
    assert pnl.net_micro == 4_000_000 - 2_000_000, "4.00 USD made, less the 2.00 USD fee: the 3.00 AUD is not in it"
    assert book.pnl(DESK, "EUR.USD", 108_500, 108_510).fees_not_netted_micro == {"AUD": 3_000_000}
    assert book.pnl("control", "USD.JPY", 151_230, 151_240).fees_not_netted_micro == {}, "a fee of zero is no fee"
    assert book.pnl("ml", "EUR.USD", 108_500, 108_510).fees_not_netted_micro == {}, "and it is the payer's alone"
    with pytest.raises(TypeError):
        pnl.fees_not_netted_micro["AUD"] = 0
    with pytest.raises(TypeError, match="fees_not_netted_micro"):
        PnL(0, 0, 0, 0)  # a result cannot be made without saying what it left out
    said = " ".join((PnL.__doc__ or "").split())
    assert "`net_micro` is net of quote-currency fees only" in said and "NOT in `net_micro`" in said


def test_first_in_first_out_and_marking_at_the_side_that_would_close():
    book = booked([
        a_fill(1, Side.BUY, 100, 10), a_fill(2, Side.BUY, 100, 14), a_fill(3, Side.SELL, 150, 13),
    ])  # fmt: skip
    pnl = book.pnl("control", "EUR.USD", 12, 16)
    assert pnl.realised == 100 * (13 - 10) + 50 * (13 - 14)
    assert pnl.unrealised == 50 * (12 - 14), "the long 50 is valued at the bid"
    book.apply(a_fill(4, Side.SELL, 80, 11))  # through flat: closes 50, opens a short of 30
    short = book.pnl("control", "EUR.USD", 12, 16)
    assert book.position("control", "EUR.USD").quantity == -30
    assert short.realised == 250 + 50 * (11 - 14) and short.unrealised == -30 * (16 - 11), "the short at the ask"


def test_the_view_a_sleeve_gets_is_its_own_and_read_only():
    yen = a_fill(3, Side.BUY, 5, 150_000, pair="USD.JPY")
    book = booked([a_fill(1, Side.BUY, 100, 10), a_fill(2, Side.SELL, 40, 12, sleeve="ml"), yen])
    book.charge(CommissionEvent("x1", 7, "USD", 0, 0))
    view = book.view("control")
    assert view.quantity("EUR.USD") == 100 and view.quantity("USD.JPY") == 5 and view.quantity("GBP.USD") == 0
    assert view.fees == {"USD": 7} and book.view("ml").quantity("EUR.USD") == -40
    assert book.view("nobody").positions == {}
    with pytest.raises(TypeError):
        view.positions["EUR.USD"] = Position("EUR.USD", 1, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.sleeve = "ml"
    assert book.sleeves() == ["control", "ml"] and book.position(DESK, "EUR.USD").quantity == 60


def test_what_cannot_be_booked_exactly_is_an_error():
    book = booked([a_fill(1, Side.BUY, 100, 10)])
    with pytest.raises(BookError, match="two different fills"):
        book.apply(a_fill(1, Side.BUY, 100, 11))
    with pytest.raises(BookError, match="not given an instrument"):
        book.apply(a_fill(2, Side.BUY, 1, 1, pair="GBP.USD"))
    stray = Fill("x9", "manual-order", "EUR.USD", Side.SELL, 10, 10, 0)
    assert book.apply(stray) and book.position(UNATTRIBUTED, "EUR.USD").quantity == -10
    assert book.position(DESK, "EUR.USD").quantity == 90


def test_a_commission_on_the_fill_itself_is_counted_and_a_later_report_replaces_it():
    book = booked([a_fill(1, Side.BUY, 100, 10, commission=5, commission_currency="USD")])
    assert book.fees() == {"USD": 5}
    book.charge(CommissionEvent("x1", 8, "USD", 0, 0))
    book.take(CommissionEvent("x1", 8, "USD", 0, 0))
    book.take("not an event")
    assert book.fees() == {"USD": 8} and book.fees("control", "EUR.USD") == {"USD": 8} and book.fees("ml") == {}
