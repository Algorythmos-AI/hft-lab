"""What holds for any stream of orders on a cash account with a float (ADR 0031).

The stream is generated: sleeves buying and selling two pairs in any order and size, rates moving between
them, a venue that is slow to answer. Whatever comes:

  * no order the gate let through is refused by the broker for want of cash, and no currency goes under zero;
  * an order that adds to a position never leaves the currency it paid in under its reserve;
  * the venue's cash is the anchor plus what the book's fills moved, to the millionth, in every currency;
  * the baseline and the sleeves' deviations add up to the venue's cash.

An exit is not held to the reserve (it takes back what the entry paid out), and a fee is taken whatever the
gate says. So "never under the reserve" is said of entries, and of every moment only where rates stand still
and nothing is charged.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import example, given, settings
from hypothesis import strategies as st

from hftlab.book import FLOAT, effect, expected
from hftlab.core import OrderEvent, OrderState, Side
from hftlab.risk import parse_risk_config
from hftlab.trader import funding
from hftlab.venues.fake.market import REFUSED
from tests.inventory.kit import AUDUSD, QUOTES, RESERVES, UNIT, Till, account, inventory, limits
from tests.risk.kit import raw
from tests.venues.kit import EURUSD, MS

PAIR = st.sampled_from(["EUR.USD", "AUD.USD"])
SIZE = st.sampled_from([20_000, 25_000, 30_000, 50_000, 75_000])
ORDER = st.tuples(st.just("order"), st.sampled_from(Side), SIZE, PAIR, st.sampled_from(["control", "ml"]))
WAIT = st.tuples(st.just("wait"), st.integers(0, 300))
STEP = st.one_of(ORDER, st.tuples(st.just("move"), st.integers(-40, 40)), WAIT)
STILL = st.one_of(ORDER, WAIT)  # the steps with the rates left where they are, drawn and not filtered for


def play(steps, config, *, moves: bool) -> None:
    with tempfile.TemporaryDirectory() as folder:
        till = Till(Path(folder), config)
        till.fund()
        reserve = {code: units * UNIT for code, units in RESERVES.items()}
        moved = 0
        for step in steps:
            if step[0] == "move" and moves:
                moved += 5 * step[1]
                till.tick(moved)
                till.pump(MS)
            elif step[0] == "wait":
                till.pump(step[1] * MS)
            elif step[0] == "order":
                _, side, quantity, pair, sleeve = step
                order = till.order(side, quantity, pair=pair, sleeve=sleeve)
                held = till.oms.book.position(sleeve, pair).quantity
                adds = held * side.sign >= 0 or quantity > abs(held)
                pays = pair[4:] if side is Side.BUY else pair[:3]
                proposal = till.oms.propose(order)
                heard = till.pump(60 * MS)
                said = [e.reason for e in heard if isinstance(e, OrderEvent) and e.state is OrderState.REJECTED]
                refused = [reason for reason in said if reason and reason.startswith(REFUSED)]
                assert not refused, "the gate let through an order the account could not pay for"
                if proposal.sent and adds and not till.oms.in_flight():
                    assert till.held(pays) >= reserve[pays], (step, till.market.cash())
            cash = till.market.cash()
            assert min(cash.values()) >= 0
            if not moves and not till.market.config.commission:
                assert all(cash[code] >= kept for code, kept in reserve.items()), cash
        till.pump(2_000 * MS)
        book, cash = till.oms.book, till.market.cash()
        assert till.agrees() and not till.oms.in_flight()
        total = expected(till.anchor, book, FLOAT)
        for sleeve in book.sleeves():
            if sleeve != FLOAT:
                for currency, amount in effect(book, sleeve).items():
                    total[currency] = total.get(currency, 0) + amount
        assert {c: a for c, a in total.items() if a} == {c: a for c, a in cash.items() if a}
        assert not till.reconciler.check().cash


# Found by this test on 2026-10-13: one sleeve's short brought in dollars, another sleeve's entry spent them, and
# the first sleeve's cover was rejected by the broker for want of a dollar.
SPENT_COVER = [("order", Side.BUY, 20000, "EUR.USD", "control")] * 13 + [
    ("order", Side.SELL, 25000, "EUR.USD", "ml"),
    ("order", Side.BUY, 25000, "EUR.USD", "control"),
    ("order", Side.BUY, 25000, "EUR.USD", "ml"),
]


@settings(max_examples=40, deadline=None)
@given(st.lists(STEP, max_size=40), st.sampled_from([1, 2]), st.sampled_from([1, 150]))
@example(SPENT_COVER, 1, 1)
def test_no_stream_of_orders_overdraws_the_account_or_parts_the_book_from_the_venues_cash(steps, chunks, ack_ms):
    play(steps, account(fill_chunks=chunks, ack_delay_ns=ack_ms * MS), moves=True)


@settings(max_examples=25, deadline=None)
@given(st.lists(STILL, max_size=40))
def test_with_rates_still_and_nothing_charged_no_currency_is_ever_under_its_reserve(steps):
    # An exit still pays the spread. An entry is judged at the edge of its collar and filled at the touch, and that
    # margin on the last entry let through is many times the spread these few exits pay.
    play(steps, account(commission=0), moves=False)


# ---- funding is idempotent: the plan of an account that was funded by a plan is empty ----------------------------

HOLDING = st.fixed_dictionaries(
    {"AUD": st.integers(0, 2_000_000), "USD": st.integers(0, 400_000), "EUR": st.integers(0, 200_000)}
)


def _limits():
    loaded = raw({"per_pair/USD.JPY": ...})
    loaded["per_pair"]["AUD.USD"] = dict(loaded["per_pair"]["EUR.USD"])
    loaded["inventory"] = inventory()
    return parse_risk_config(loaded)


@settings(max_examples=200, deadline=None)
@given(HOLDING, st.integers(-2_000, 2_000), st.integers(-2_000, 2_000), st.integers(0, 100))
def test_a_plan_carried_out_at_any_price_its_collar_allows_leaves_nothing_to_plan(holding, eur, aud, slip):
    risk = _limits()
    instruments = {"EUR.USD": EURUSD, "AUD.USD": AUDUSD}
    quotes = {
        "EUR.USD": (QUOTES["EUR.USD"][0] + 5 * eur, QUOTES["EUR.USD"][1] + 5 * eur),
        "AUD.USD": (QUOTES["AUD.USD"][0] + 5 * aud, QUOTES["AUD.USD"][1] + 5 * aud),
    }
    cash = {code: units * UNIT for code, units in holding.items()}
    try:
        steps = funding.plan(cash, quotes, instruments, risk.inventory, risk.pairs)
    except funding.CannotFund:
        return
    for step in steps:
        instrument, limit = instruments[step.pair], risk.pairs[step.pair]
        bid, ask = quotes[step.pair]
        edge = funding.worst(step.side, bid, ask, instrument, limit)
        touch = ask if step.side is Side.BUY else bid
        price = touch + (edge - touch) * slip // 100  # anywhere between the touch and the collar's edge
        paid = step.quantity * price * 10
        cash[step.pair[:3]] += step.side.sign * step.quantity * UNIT
        cash["USD"] -= step.side.sign * paid
        assert min(cash.values()) >= 0, "sells come first, so no step is paid from cash that is not there yet"
    for code, wanted in risk.inventory.float_micro.items():
        assert cash[code] >= wanted
    assert funding.plan(cash, quotes, instruments, risk.inventory, risk.pairs) == ()


def test_the_test_limits_are_the_ones_the_stream_is_judged_by():
    assert limits().inventory.reserve_micro == {code: units * UNIT for code, units in RESERVES.items()}
