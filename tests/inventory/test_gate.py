"""The risk gate, the order manager and the reconciler on a cash account with a float (ADR 0031).

The broker is the fake one, playing an account that lends nothing. Every balance is made up.
"""
from __future__ import annotations

from hftlab.book import FLOAT, effect, expected
from hftlab.core import InstrumentKind, OrderEvent, OrderState, Side
from hftlab.venues.fake.market import INSUFFICIENT_CASH
from tests.inventory.kit import RESERVES, UNIT, Till, account, limits
from tests.venues.kit import MS, SECOND

BUY, SELL = Side.BUY, Side.SELL


def proposed(till: Till, side: Side, quantity: int, **more) -> tuple[bool, str]:
    """(was it sent, the code it was let through or stopped with) for one order, with time for the venue to answer."""
    proposal = till.oms.propose(till.order(side, quantity, **more))
    till.pump(50 * MS)
    return proposal.sent, proposal.code


def test_until_a_float_stands_no_sleeve_enters_and_no_conversion_goes_unasked(tmp_path):
    till = Till(tmp_path)
    assert proposed(till, BUY, 20_000) == (False, "NOT_FUNDED"), "no anchor: nothing to deviate from"
    till.show(funded=False)
    assert proposed(till, SELL, 20_000) == (False, "NOT_FUNDED"), "an anchor alone is not a float"
    assert proposed(till, SELL, 25_000, pair="AUD.USD", sleeve=FLOAT) == (False, "FUNDING_NOT_OPEN")
    assert till.oms.book.open_lines() == {} and till.market.positions() == {}


def test_a_pair_the_account_cannot_hold_is_refused_funded_or_not(tmp_path):
    till = Till(tmp_path)
    assert proposed(till, BUY, 20_000, pair="USD.JPY") == (False, "PAIR_NOT_TRADABLE")
    till.fund()
    assert proposed(till, SELL, 20_000, pair="USD.JPY") == (False, "PAIR_NOT_TRADABLE")
    assert proposed(till, BUY, 25_000, pair="USD.JPY", sleeve=FLOAT) == (False, "PAIR_NOT_TRADABLE")


def test_the_float_is_a_line_of_its_own_and_the_venues_cash_is_the_anchor_plus_the_fills(tmp_path):
    till = Till(tmp_path)
    till.fund()
    assert till.oms.book.open_lines() == {(FLOAT, "AUD.USD"): -250_000, (FLOAT, "EUR.USD"): 60_000}
    assert till.agrees()
    assert till.held("EUR") == 60_000 * UNIT and till.held("USD") > 100_000 * UNIT
    assert till.gate.cash() == till.market.cash(), "the gate's own figure is the venue's, to the millionth"
    assert not till.reconciler.check().cash


def test_an_entry_under_the_pairs_minimum_is_an_odd_lot_and_an_exit_of_any_size_is_not(tmp_path):
    # The venue itself takes an order of 1,000: the minimum for good pricing is the limits' alone to keep.
    till = Till(tmp_path, risk=limits(minimum_entry={"EUR.USD": 30_000, "AUD.USD": 25_000}))
    till.fund()
    assert proposed(till, BUY, 20_000) == (False, "ODD_LOT")
    assert proposed(till, SELL, 29_000) == (False, "ODD_LOT")
    assert proposed(till, BUY, 50_000) == (True, "ALLOW")
    assert proposed(till, SELL, 20_000)[0], "a part of the position goes back, whatever its size"
    assert proposed(till, SELL, 20_000)[0]
    assert proposed(till, SELL, 20_000) == (False, "ODD_LOT"), "10,000 of it would close and 10,000 would open"
    assert till.oms.book.position("control", "EUR.USD").quantity == 10_000


def test_both_directions_from_flat_are_paid_from_the_float(tmp_path):
    till = Till(tmp_path)
    till.fund()
    euros, dollars = till.held("EUR"), till.held("USD")
    assert proposed(till, SELL, 20_000) == (True, "ALLOW"), "short from flat: the float's euros are sold"
    assert till.held("EUR") == euros - 20_000 * UNIT and till.held("USD") > dollars
    assert proposed(till, BUY, 25_000, pair="AUD.USD", sleeve="ml") == (True, "ALLOW")
    assert till.oms.book.position("control", "EUR.USD").quantity == -20_000
    assert till.oms.book.position("ml", "AUD.USD").quantity == 25_000
    assert till.agrees()


def test_an_entry_that_would_take_a_currency_under_its_reserve_is_refused_and_the_way_back_is_open(tmp_path):
    till = Till(tmp_path)
    till.fund()
    sent = [proposed(till, SELL, 20_000) for _ in range(4)]
    assert sent == [(True, "ALLOW"), (True, "ALLOW"), (False, "CASH_RESERVE"), (False, "CASH_RESERVE")]
    assert till.held("EUR") == 20_000 * UNIT >= RESERVES["EUR"] * UNIT, "60,000 less two entries; a third would leave 0"
    assert proposed(till, BUY, 20_000, sleeve="ml") == (True, "ALLOW"), "the other way is paid in dollars"
    assert proposed(till, BUY, 40_000)[0], "and the exit is never held to the reserve"
    assert till.agrees()


def test_what_orders_on_their_way_would_pay_is_kept_back_and_what_they_would_bring_is_not(tmp_path):
    till = Till(tmp_path, account(ack_delay_ns=5 * SECOND))
    till.show(funded=False, open_=True)
    first = till.oms.propose(till.order(SELL, 250_000, pair="AUD.USD", sleeve=FLOAT))
    assert first.sent and till.gate.affordable("EUR.USD", BUY) == 0, "the dollars are not there until it fills"
    assert till.convert(BUY, 60_000, "EUR.USD") == "CASH_RESERVE"
    till.pump(6 * SECOND)
    assert till.convert(BUY, 60_000, "EUR.USD") == "ALLOW"


def test_the_floats_drift_and_its_fees_are_not_the_desks_result_and_trip_no_latch(tmp_path):
    till = Till(tmp_path)
    till.fund()
    till.tick(-3_000)  # every rate falls three big figures: the float's line loses a great deal
    till.pump(10 * MS)
    assert proposed(till, BUY, 20_000) == (True, "ALLOW"), "no latch tripped, and entries go on"
    assert not till.gate.latches()


# ---- faults ----------------------------------------------------------------------------------------------------


def test_an_order_the_broker_refuses_for_cash_moves_nothing_and_the_missing_cash_becomes_a_break(tmp_path):
    till = Till(tmp_path)
    till.fund()
    till.market.post("USD", 1_000 * UNIT - till.held("USD"))  # cash left the account with no fill behind it
    before = till.market.cash()
    proposal = till.oms.propose(till.order(BUY, 20_000))
    heard = till.pump(50 * MS)
    assert proposal.sent, "the gate still believed its own figure"
    last = [event for event in heard if isinstance(event, OrderEvent)][-1]
    assert (last.state, last.reason) == (OrderState.REJECTED, INSUFFICIENT_CASH)
    assert till.market.cash() == before and till.oms.book.position("control", "EUR.USD").quantity == 0
    assert not till.oms.in_flight(), "a refused order holds nothing back"
    found = till.reconciler.check()
    assert [d.currency for d in found.cash] == ["USD"] and not found.broken, "inside the grace time"
    till.pump(2 * till.risk.reconciliation_grace_ns)
    found = till.reconciler.check()
    assert found.broken and found.event is not None and [d.currency for d in found.event.cash] == ["USD"]
    assert proposed(till, SELL, 20_000) == (False, "RECONCILIATION_BREAK")


def test_a_partial_fill_and_then_a_refusal_for_cash_leaves_the_book_and_the_venue_agreeing(tmp_path):
    config = account(fill_chunks=2, chunk_gap_ns=400 * MS)
    till = Till(tmp_path, config)
    till.fund()
    proposal = till.oms.propose(till.order(BUY, 40_000))
    assert proposal.sent
    till.pump(100 * MS)
    assert till.oms.book.position("control", "EUR.USD").quantity == 20_000, "the first half is filled"
    gone = till.held("USD") - 1_000 * UNIT
    till.market.post("USD", -gone)  # the dollars for the second half are no longer there
    heard = till.pump(SECOND)
    last = [event for event in heard if isinstance(event, OrderEvent)][-1]
    assert last.state is OrderState.CANCELLED and last.reason == INSUFFICIENT_CASH and last.filled_quantity == 20_000
    assert till.oms.book.position("control", "EUR.USD").quantity == 20_000 and not till.oms.in_flight()
    assert till.market.positions()[("EUR.USD", InstrumentKind.CASH)] == 80_000, "the float's 60,000 and the half"
    till.market.post("USD", gone)
    assert till.agrees(), "but for the cash that was taken away and given back, nothing is unexplained"
    assert proposed(till, SELL, 20_000)[0], "and the half that was filled can be closed"


def test_cash_the_broker_posts_by_itself_is_a_break_until_an_operator_takes_a_new_anchor(tmp_path):
    till = Till(tmp_path)
    till.fund()
    till.market.post("AUD", -40 * UNIT)  # a fee no fill explains
    till.reconciler.check()
    till.pump(2 * till.risk.reconciliation_grace_ns)
    assert till.reconciler.check().broken
    till.anchor = None
    till.show(funded=True)  # what `--reanchor` does: the venue's cash, less what the book's fills moved
    found = till.reconciler.check()
    assert not found.cash and not found.broken
    assert expected(till.anchor, till.oms.book) == till.market.cash()


def test_a_difference_within_the_tolerance_is_agreement(tmp_path):
    till = Till(tmp_path)
    till.fund()
    till.market.post("USD", till.risk.inventory.tolerance_micro)
    assert not till.reconciler.check().cash
    till.market.post("USD", 1)
    assert [d.currency for d in till.reconciler.check().cash] == ["USD"]


def test_a_sleeves_deviation_is_its_own_lines_and_sums_with_the_baseline_to_the_account(tmp_path):
    till = Till(tmp_path)
    till.fund()
    proposed(till, BUY, 20_000)
    proposed(till, SELL, 25_000, pair="AUD.USD", sleeve="ml")
    book = till.oms.book
    total = expected(till.anchor, book, FLOAT)
    for sleeve in ("control", "ml"):
        for currency, amount in effect(book, sleeve).items():
            total[currency] = total.get(currency, 0) + amount
    assert total == till.market.cash()
    assert effect(book, "control")["EUR"] == 20_000 * UNIT and effect(book, "ml")["AUD"] < -25_000 * UNIT
