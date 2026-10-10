"""The shadow venue's cost model, case by case with the arithmetic done by hand, and its place outside the lock."""
from __future__ import annotations

import ast
import dataclasses
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.calendar import next_close, next_open
from hftlab.core import (
    Fill,
    Instrument,
    InstrumentKind,
    Liquidity,
    OrderEvent,
    OrderState,
    Quote,
    Side,
    SimClock,
    TimeInForce,
)
from hftlab.safety import VENUES
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.port import CommissionEvent, ConnectionState, NotReady, OrderRefused, Unsupported, VenueError
from hftlab.venues.sim import CostModelError, SimVenue, load_cost_model
from hftlab.venues.sim.cost import NO_FILL, NORMAL, WIDENED
from tests.support import ROOT, imports, sources
from tests.venues.kit import ASK, BID, EURUSD, INSTRUMENTS, MS, SECOND, T0, USDJPY, an_order, brief, of, plain_costs

IOC, FOK, DAY, GTC = TimeInForce.IOC, TimeInForce.FOK, TimeInForce.DAY, TimeInForce.GTC
MINUTE = 60 * SECOND
ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York, later the same day as T0
SATURDAY = 1_760_184_000 * SECOND  # 2025-10-11 12:00 UTC


class Fixed:
    def __init__(self, ns: int) -> None:
        self.ns = ns

    def sample_ns(self, rng: random.Random) -> int:
        return self.ns


class Rig:
    def __init__(self, start: int = T0, **costs) -> None:
        self.clock = SimClock(start)
        self.venue = SimVenue(self.clock, INSTRUMENTS, plain_costs(**costs), seed=1)
        self.venue.connect()
        self.venue.mark_reconciled()
        self.venue.poll()
        self.fed = 0

    def quote(
        self, bid: int = BID, ask: int = ASK, bid_size: int = 10**6, ask_size: int = 10**6, *, at: int | None = None
    ) -> None:
        self.fed += 1
        at = self.clock.now_ns() if at is None else at
        self.venue.feed(Quote("EUR.USD", bid, ask, bid_size, ask_size, at, self.fed))

    def after(self, ns: int) -> list:
        self.clock.advance(ns)
        return self.venue.poll()

    def run(self, order, wait_ns: int = 50 * MS) -> list:
        self.venue.place(order, EURUSD)
        return self.venue.poll() + self.after(wait_ns)


def quoted(**costs) -> Rig:
    rig = Rig(**costs)
    rig.quote()
    return rig


# ---- latency: the order meets the quote that exists when it arrives -------------------------------------------------


def test_an_order_is_filled_at_the_quote_that_exists_after_the_delay_not_the_one_it_was_decided_on():
    rig = quoted()
    rig.venue.place(an_order(side=Side.BUY, tif=IOC), EURUSD)  # decided at an ask of 108,510
    rig.quote(108_530, 108_540, at=T0 + 3 * MS)  # the market moves away during the 5 ms it takes to arrive
    fill = of(rig.after(10 * MS), Fill)[0]
    assert (fill.price, fill.venue_ts_ns, fill.liquidity) == (108_540, T0 + 5 * MS, Liquidity.TAKER)


def test_a_quote_that_comes_after_the_order_arrived_does_not_change_its_fill():
    rig = quoted()
    rig.venue.place(an_order(side=Side.BUY, tif=IOC), EURUSD)
    rig.quote(108_530, 108_540, at=T0 + 6 * MS)
    assert of(rig.after(10 * MS), Fill)[0].price == ASK


def test_the_delay_is_drawn_from_the_configured_range_and_repeats_with_the_seed():
    def delays(seed: int) -> list[int]:
        clock = SimClock(T0)
        venue = SimVenue(clock, INSTRUMENTS, plain_costs(latency_min_ns=20 * MS, latency_max_ns=80 * MS), seed=seed)
        venue.connect()
        venue.mark_reconciled()
        venue.feed(Quote("EUR.USD", BID, ASK, 10**9, 10**9, T0, 1))
        for number in range(30):
            venue.place(an_order(number, tif=IOC), EURUSD)
        clock.advance(SECOND)
        return [f.venue_ts_ns - T0 for f in of(venue.poll(), Fill)]

    assert delays(4) == delays(4) and delays(4) != delays(5)
    assert all(20 * MS <= d <= 80 * MS for d in delays(4)) and len(set(delays(4))) > 10


def test_a_latency_model_can_be_injected():
    clock = SimClock(T0)
    venue = SimVenue(clock, INSTRUMENTS, plain_costs(), latency=Fixed(123 * MS))
    venue.connect()
    venue.mark_reconciled()
    venue.feed(Quote("EUR.USD", BID, ASK, 10**9, 10**9, T0, 1))
    venue.place(an_order(tif=IOC), EURUSD)
    clock.advance(SECOND)
    assert of(venue.poll(), Fill)[0].venue_ts_ns == T0 + 123 * MS


# ---- crossing the spread, displayed size, slippage ------------------------------------------------------------------


def test_a_buy_pays_the_ask_and_a_sell_gets_the_bid_plus_slippage_against_it():
    rig = quoted(slippage_increments=2)  # two increments of 5 price units
    bought = of(rig.run(an_order(1, side=Side.BUY, tif=IOC)), Fill)[0]
    sold = of(rig.run(an_order(2, side=Side.SELL, tif=IOC)), Fill)[0]
    assert (bought.price, sold.price) == (ASK + 10, BID - 10)


def test_slippage_that_would_break_the_limit_means_no_fill():
    rig = quoted(slippage_increments=2)
    events = rig.run(an_order(side=Side.BUY, limit=ASK + 5, tif=IOC))
    assert of(events, Fill) == [] and of(events, OrderEvent)[-1].state is OrderState.CANCELLED


def test_an_immediate_order_gets_no_more_than_the_size_on_display():
    rig = Rig()
    rig.quote(ask_size=30_000)
    events = rig.run(an_order(quantity=50_000, tif=IOC))
    assert brief(events) == [
        ("order", "SUBMITTED", 0), ("order", "WORKING", 0), ("fill", "BUY", 30_000, ASK, "TAKER"),
        ("order", "PARTIALLY_FILLED", 30_000), ("commission", 2_000_000, "USD"), ("order", "CANCELLED", 30_000),
    ]  # fmt: skip
    assert "unfilled remainder" in of(events, OrderEvent)[-1].reason


def test_fill_or_kill_fills_whole_or_not_at_all():
    rig = Rig()
    rig.quote(ask_size=30_000)
    assert of(rig.run(an_order(1, quantity=50_000, tif=FOK)), Fill) == []
    assert [f.quantity for f in of(rig.run(an_order(2, quantity=30_000, tif=FOK)), Fill)] == [30_000]


def test_the_rest_of_a_resting_marketable_order_takes_the_displayed_size_of_each_later_quote():
    rig = Rig()
    rig.quote(ask_size=30_000)
    first = rig.run(an_order(quantity=70_000, tif=DAY), wait_ns=10 * MS)
    rig.quote(BID + 5, ASK + 5, ask_size=25_000)
    second = rig.after(MS)
    rig.quote(BID, ASK, ask_size=500_000)
    third = rig.after(MS)
    fills = of(first + second + third, Fill)
    assert [(f.quantity, f.price) for f in fills] == [(30_000, ASK), (25_000, ASK + 5), (15_000, ASK)]
    assert of(third, OrderEvent)[-1].state is OrderState.FILLED and rig.venue.open_orders() == ()


# ---- resting limit orders: only when traded through -----------------------------------------------------------------


def test_a_resting_buy_is_not_filled_when_the_ask_only_touches_its_price():
    rig = quoted()
    resting = an_order(side=Side.BUY, limit=108_400)
    assert of(rig.run(resting), Fill) == []
    rig.quote(108_390, 108_400)  # the ask is AT the limit: a paper account would fill here
    assert of(rig.after(MS), Fill) == [] and len(rig.venue.open_orders()) == 1
    rig.quote(108_385, 108_395)  # the ask is THROUGH the limit
    fill = of(rig.after(MS), Fill)[0]
    assert (fill.price, fill.quantity, fill.liquidity) == (108_400, 20_000, Liquidity.MAKER), "at its own price"


def test_a_resting_sell_needs_the_bid_to_trade_through_too():
    rig = quoted()
    assert of(rig.run(an_order(side=Side.SELL, limit=108_600)), Fill) == []
    rig.quote(108_600, 108_610)
    assert of(rig.after(MS), Fill) == []
    rig.quote(108_605, 108_615)
    assert of(rig.after(MS), Fill)[0].price == 108_600


def test_a_resting_order_that_is_traded_through_gets_no_more_than_the_size_that_came_through():
    rig = quoted()
    rig.run(an_order(1, side=Side.BUY, quantity=70_000, limit=108_400))
    seen = []
    for ask, size in ((108_395, 30_000), (108_390, 0), (108_400, 10**6), (108_395, 25_000), (108_390, 10**6)):
        rig.quote(ask - 10, ask, ask_size=size)  # through; through with nothing shown; at the touch; through twice
        seen.append(rig.after(MS))
    filled = [[(f.quantity, f.price, f.liquidity) for f in of(events, Fill)] for events in seen]
    maker = Liquidity.MAKER
    assert filled == [[(30_000, 108_400, maker)], [], [], [(25_000, 108_400, maker)], [(15_000, 108_400, maker)]]
    assert [e.state for e in of(seen[0], OrderEvent)] == [OrderState.PARTIALLY_FILLED], "the rest goes on resting"
    assert of(seen[-1], OrderEvent)[-1].state is OrderState.FILLED and rig.venue.open_orders() == ()
    fees = [c.commission for events in seen for c in of(events, CommissionEvent)]
    assert fees == [2_000_000, 0, 0], "one order, one minimum, however many pieces it filled in"

    rig.run(an_order(2, side=Side.SELL, quantity=50_000, limit=108_600))
    rig.quote(108_605, 108_615, bid_size=20_000, ask_size=10**6)
    assert [f.quantity for f in of(rig.after(MS), Fill)] == [20_000], "a sell is held to the bid's size, not the ask's"


def test_a_resting_order_can_be_cancelled_and_a_late_cancel_is_a_notice():
    rig = quoted()
    resting = an_order(limit=108_400)
    rig.run(resting)
    rig.venue.cancel(resting.client_order_id)
    assert brief(rig.after(10 * MS)) == [("order", "CANCELLED", 0)]
    rig.venue.cancel(resting.client_order_id)
    assert brief(rig.after(10 * MS)) == [("notice", "cancel-too-late")]


# ---- commission -----------------------------------------------------------------------------------------------------


def test_commission_is_the_rate_on_the_trade_value_rounded_up_or_the_minimum():
    costs = plain_costs(commission_rate_bps_x100=20)  # 0.20 basis points
    small = costs.notional_micro(108_510, 20_000, EURUSD)
    assert small == 21_702_000_000, "21,702 USD"
    assert costs.commission_micro(small, "USD") == 2_000_000, "the rate gives 0.43 USD, so the minimum applies"
    large = costs.notional_micro(108_510, 2_000_000, EURUSD)
    assert costs.commission_micro(large, "USD") == 43_404_000, "43.404 USD: 0.2 bp of 2.17 million"
    assert costs.commission_micro(1, "USD") == 2_000_000
    assert plain_costs(commission_rate_bps_x100=20, commission_minimum={"USD": 0}).commission_micro(1, "USD") == 1, "up"
    yen = costs.notional_micro(151_234, 20_000, USDJPY)
    assert yen == 3_024_680_000_000, "3,024,680 yen"
    with pytest.raises(CostModelError, match="no commission minimum in GBP"):
        costs.commission_micro(1, "GBP")


def test_the_minimum_is_per_order_across_its_partial_fills_not_per_fill():
    rig = Rig(commission_rate_bps_x100=20)
    rig.quote(ask_size=30_000)
    first = rig.run(an_order(quantity=2_000_000, tif=DAY), wait_ns=10 * MS)
    rig.quote(BID, ASK, ask_size=10**9)
    second = rig.after(MS)
    charged = [c.commission for c in of(first + second, CommissionEvent)]
    assert charged[0] == 2_000_000, "the first small fill already owes the order's minimum"
    whole = plain_costs(commission_rate_bps_x100=20).commission_micro(108_510 * 2_000_000 * 10, "USD")
    assert sum(charged) == whole == 43_404_000 and charged[1] == whole - 2_000_000, "the order pays its rate once"


def test_an_order_below_the_cost_models_minimum_size_is_rejected_on_arrival():
    rig = quoted(min_order_size=50_000)
    events = rig.run(an_order(quantity=20_000, tif=IOC))
    assert brief(events) == [("order", "SUBMITTED", 0), ("order", "REJECTED", 0)]
    assert "minimum order size of 50000" in of(events, OrderEvent)[-1].reason and rig.venue.open_orders() == ()


# ---- the rollover and stale quotes ----------------------------------------------------------------------------------

AROUND = {"no_fill_before_ns": 5 * MINUTE, "no_fill_after_ns": 15 * MINUTE, "widen_before_ns": 30 * MINUTE,
          "widen_after_ns": 60 * MINUTE, "spread_multiplier_x100": 300}  # fmt: skip


@pytest.mark.parametrize(
    ("offset_min", "phase"),
    [(-31, NORMAL), (-30, WIDENED), (-6, WIDENED), (-5, NO_FILL), (0, NO_FILL), (15, NO_FILL), (16, WIDENED),
     (60, WIDENED), (61, NORMAL)],
)  # fmt: skip
def test_the_day_has_three_phases_around_the_rollover(offset_min, phase):
    assert plain_costs(**AROUND).phase(ROLLOVER + offset_min * MINUTE) == phase


def test_nothing_fills_inside_the_rollover_window():
    rig = Rig(ROLLOVER - 10 * MINUTE, **AROUND)
    rig.quote()
    rig.run(an_order(2, limit=108_400))  # it joins the book before the window, below the ask: it rests
    rig.clock.advance_to(ROLLOVER - 2 * MINUTE)
    events = rig.run(an_order(1, tif=IOC))
    assert of(events, Fill) == [] and "rollover" in of(events, OrderEvent)[-1].reason
    rig.quote(108_300, 108_310)  # traded through, but inside the window
    assert of(rig.after(MS), Fill) == []
    rig.clock.advance_to(ROLLOVER + 16 * MINUTE)
    rig.quote(108_290, 108_300)
    fill = of(rig.after(MS), Fill)[0]
    assert (fill.price, fill.liquidity) == (108_400, Liquidity.MAKER)


def test_a_limit_order_sent_inside_the_window_is_judged_by_the_first_quote_after_it():
    rig = Rig(ROLLOVER - 2 * MINUTE, **AROUND)
    rig.quote()
    rig.run(an_order(limit=108_400))  # below the ask as it stands, but nothing is judged inside the window
    rig.clock.advance_to(ROLLOVER + 16 * MINUTE)
    rig.quote(108_290, 108_300)  # the market fell through its price meanwhile
    fill = of(rig.after(MS), Fill)[0]
    assert (fill.price, fill.liquidity) == (108_310, Liquidity.TAKER), "as if it arrived now: across a widened spread"


def test_near_the_rollover_the_spread_is_taken_as_wider_than_quoted():
    costs = plain_costs(**AROUND)
    assert costs.extra_half_spread(10, EURUSD) == 10, "a spread of 10 taken as 30: 10 more on each side"
    assert costs.extra_half_spread(15, EURUSD) == 15 and costs.extra_half_spread(12, EURUSD) == 15, "up to the grid"
    assert plain_costs().extra_half_spread(10, EURUSD) == 0
    rig = Rig(ROLLOVER - 20 * MINUTE, **AROUND)
    rig.quote()
    assert of(rig.run(an_order(1, side=Side.BUY, tif=IOC)), Fill)[0].price == ASK + 10
    assert of(rig.run(an_order(2, side=Side.SELL, tif=IOC)), Fill)[0].price == BID - 10


def test_no_fill_on_a_quote_the_venue_has_not_confirmed_lately():
    rig = quoted(stale_after_ns=2 * SECOND)
    rig.clock.advance(3 * SECOND)
    events = rig.run(an_order(1, tif=IOC))
    assert of(events, Fill) == [] and "stale" in of(events, OrderEvent)[-1].reason


def test_a_heartbeat_keeps_a_quiet_book_fresh():
    rig = quoted(stale_after_ns=2 * SECOND)
    rig.clock.advance(3 * SECOND)
    rig.venue.quiet("EUR.USD", rig.clock.now_ns())  # the source says: no change, and I am alive
    rig.venue.quiet("USD.JPY", rig.clock.now_ns())  # a pair with no quote yet: nothing to refresh
    assert of(rig.run(an_order(1, tif=IOC)), Fill)[0].price == ASK


def test_the_market_being_shut_means_no_fill():
    saturday = 1_760_184_000 * SECOND  # 2025-10-11 12:00 UTC
    rig = Rig(saturday)
    rig.quote()
    assert of(rig.run(an_order(1, tif=IOC)), Fill) == []


# ---- an order that arrives to a book it cannot be judged against ----------------------------------------------------

SHOWN, WANTED = 30_000, 50_000
LIMITS = {"market": None, "marketable limit": ASK + 10, "passive limit": 108_400}
SAID = {
    "no quote": "no quote to trade against",
    "stale": "the quote is stale",
    "no fill": "inside the rollover window or the market is shut",
    "shut": "inside the rollover window or the market is shut",
}


def unjudged(reason: str) -> tuple[Rig, int]:
    """A venue on which an order sent now arrives to a book it cannot be judged against, and when that ends."""
    if reason == "no quote":
        return Rig(), T0 + SECOND
    if reason == "stale":
        rig = quoted(stale_after_ns=2 * SECOND)
        rig.clock.advance(3 * SECOND)
        return rig, rig.clock.now_ns() + SECOND
    if reason == "no fill":
        rig = Rig(ROLLOVER - 2 * MINUTE, no_fill_before_ns=5 * MINUTE, no_fill_after_ns=15 * MINUTE)
        rig.quote()
        return rig, ROLLOVER + 16 * MINUTE
    rig = Rig(SATURDAY)
    rig.quote()
    return rig, next_open(SATURDAY) + MINUTE


@pytest.mark.parametrize("tif", [DAY, GTC, IOC, FOK])
@pytest.mark.parametrize("kind", sorted(LIMITS))
@pytest.mark.parametrize("reason", sorted(SAID))
def test_an_order_that_could_not_be_judged_on_arrival_is_judged_by_the_first_usable_quote(reason, kind, tif):
    rig, usable_from = unjudged(reason)
    rig.venue.place(an_order(quantity=WANTED, limit=LIMITS[kind], tif=tif), EURUSD)
    arrival = rig.venue.poll() + rig.after(500 * MS)
    assert of(arrival, Fill) == []
    rig.clock.advance_to(usable_from)
    rig.quote(ask_size=SHOWN)  # a fresh quote, in an open market, outside the window
    later = rig.after(MS)
    if tif in (IOC, FOK):
        last = of(arrival, OrderEvent)[-1]
        assert (last.state, last.reason) == (OrderState.CANCELLED, SAID[reason]), "an immediate order does not wait"
        assert later == [] and rig.venue.open_orders() == ()
        return
    assert of(arrival, OrderEvent)[-1].state is OrderState.WORKING and len(rig.venue.open_orders()) == 1
    expected = (SHOWN, ASK, Liquidity.TAKER)  # across the spread, at the price on the screen, for the size on it
    if kind == "passive limit":
        assert of(later, Fill) == [], "the ask is above its price: it rests"
        rig.quote(108_385, 108_395, ask_size=SHOWN)
        later = rig.after(MS)
        expected = (SHOWN, 108_400, Liquidity.MAKER)
    (fill,) = of(later, Fill)
    assert (fill.quantity, fill.price, fill.liquidity) == expected
    assert rig.venue.open_orders()[0].filled_quantity == SHOWN, "the rest waits for the next quote"


def test_a_resting_market_order_sent_before_the_first_quote_does_not_stop_the_venue():
    clock = SimClock(T0)
    venue = SimVenue(clock, INSTRUMENTS, plain_costs(), seed=1)
    venue.connect()
    venue.mark_reconciled()
    venue.place(an_order(tif=DAY), EURUSD)  # a market order that may rest: the core types allow it
    clock.advance(500 * MS)
    venue.poll()
    venue.feed(Quote("EUR.USD", BID, ASK, 10**6, 10**6, clock.now_ns(), 1))  # this used to fail an assertion
    assert brief(venue.poll())[-3:] == [
        ("fill", "BUY", 20_000, ASK, "TAKER"), ("order", "FILLED", 20_000), ("commission", 2_000_000, "USD"),
    ]  # fmt: skip


def test_an_order_the_cost_model_cannot_price_is_refused_before_it_can_fill():
    rig = Rig(commission_minimum={"USD": 2_000_000})
    rig.venue.feed(Quote("USD.JPY", 151_230, 151_240, 10**6, 10**6, T0, 1))
    with pytest.raises(OrderRefused, match="no commission minimum in JPY"):
        rig.venue.place(an_order(pair="USD.JPY", tif=IOC), USDJPY)
    assert rig.after(SECOND) == [] and rig.venue.open_orders() == ()


STRANGER = Instrument.from_pair("GBP.USD", price_scale=5, min_order_size=1_000, kind=InstrumentKind.CASH)
WAITS = (MS, 5 * MS, SECOND, 3 * SECOND, MINUTE, 10 * MINUTE)
PAIRS = ("EUR.USD", "USD.JPY", "GBP.USD")
sizes = st.integers(0, 60).map(lambda n: n * 1_000)
STEP = st.one_of(
    st.tuples(
        st.just("place"), st.integers(0, 5), st.sampled_from(Side), sizes, st.sampled_from(TimeInForce),
        st.none() | st.integers(-6, 6).map(lambda n: ASK + n * 5), st.sampled_from(PAIRS),
        st.sampled_from([*INSTRUMENTS, STRANGER]),
    ),
    st.tuples(st.just("cancel"), st.integers(0, 9)),
    st.tuples(
        st.just("feed"), st.sampled_from(PAIRS), st.integers(-8, 8).map(lambda n: BID + n * 5),
        st.integers(-2, 6).map(lambda n: n * 5), sizes, sizes,
    ),
    st.tuples(st.just("quiet"), st.sampled_from(PAIRS)),
    st.tuples(st.just("wait"), st.sampled_from(WAITS)),
    st.just(("poll",)),
)  # fmt: skip
STARTS = (T0, ROLLOVER - 6 * MINUTE, next_close(T0) - 2 * MINUTE, SATURDAY)


@settings(max_examples=300)
@given(start=st.sampled_from(STARTS), steps=st.lists(STEP, max_size=40))
def test_no_sequence_of_calls_raises_anything_but_the_ports_own_errors(start, steps):
    """Orders of every kind, good and bad, in and out of the rollover window, on fresh, stale and missing quotes."""
    clock = SimClock(start)
    costs = plain_costs(stale_after_ns=2 * SECOND, min_order_size=5_000, commission_rate_bps_x100=20, **AROUND)
    venue = SimVenue(clock, INSTRUMENTS, costs, seed=3)
    venue.connect()
    venue.mark_reconciled()
    sent: dict[str, int] = {}
    events, fed = [], 0
    for step in steps:
        try:
            if step[0] == "place":
                _, number, side, quantity, tif, limit, pair, instrument = step
                order = an_order(number, side=side, quantity=quantity or 1, limit=limit, tif=tif, pair=pair)
                venue.place(order, instrument)
                sent[order.client_order_id] = order.quantity
            elif step[0] == "cancel":
                venue.cancel(sorted(sent)[step[1] % len(sent)] if sent and step[1] else "hft-control-nobody")
            elif step[0] == "feed":
                _, pair, bid, spread, bid_size, ask_size = step  # the spread may be zero or negative: locked, crossed
                fed += 1
                venue.feed(Quote(pair, bid, bid + spread, bid_size, ask_size, clock.now_ns(), fed))
            elif step[0] == "quiet":
                venue.quiet(step[1], clock.now_ns())
            elif step[0] == "wait":
                clock.advance(step[1])
            else:
                events += venue.poll()
        except VenueError:
            pass  # the port's own answers: refused, unknown instrument, unknown order
    events += venue.poll()
    filled: dict[str, int] = {}
    for fill in of(events, Fill):
        filled[fill.client_order_id] = filled.get(fill.client_order_id, 0) + fill.quantity
    assert all(0 < quantity <= sent[order_id] for order_id, quantity in filled.items())
    assert all(o.filled_quantity == filled.get(o.client_order_id, 0) < o.quantity for o in venue.open_orders())


# ---- the round-trip cost, by hand -----------------------------------------------------------------------------------


def test_the_round_trip_cost_of_a_minimum_size_trade_by_hand():
    costs = plain_costs(commission_rate_bps_x100=20, slippage_increments=1)
    cost_x2 = costs.round_trip_cost_micro(EURUSD, 20_000, (108_500, 108_510), (108_520, 108_535))
    spreads = (10 + 15 + 4 * 5) * 20_000 * 10  # both spreads and four half-slippages, in millionths, doubled
    assert cost_x2 == spreads + 2 * (2_000_000 + 2_000_000) == 17_000_000, "8.50 USD: 4.50 of spread, 4.00 of fees"


def test_the_cost_model_as_plain_values_leaves_no_parameter_out():
    costs = plain_costs(commission_minimum={"USD": 2_000_000, "JPY": 300_000_000})
    plain = costs.as_dict()
    assert list(plain) == [field.name for field in dataclasses.fields(costs)], "every field, in the order declared"
    assert plain["commission_minimum"] == {"JPY": 300_000_000, "USD": 2_000_000} and plain["placeholder"] is True
    assert type(costs)(**plain) == costs, "nothing was changed on the way"


def test_the_shipped_cost_model_loads_and_says_it_is_a_placeholder():
    costs = load_cost_model(ROOT / "config" / "cost_model.yaml")
    assert costs.placeholder is True and costs.effective_from == "2026-10-11"
    assert (costs.latency_min_ns, costs.latency_max_ns) == (20 * MS, 80 * MS)
    assert costs.commission_rate_bps_x100 == 20 and costs.commission_minimum == {"USD": 2_000_000, "JPY": 300_000_000}
    assert (costs.min_order_size, costs.slippage_increments, costs.stale_after_ns) == (20_000, 0, 2 * SECOND)
    assert costs.spread_multiplier_x100 == 300 and costs.no_fill_after_ns == 15 * MINUTE
    text = (ROOT / "config" / "cost_model.yaml").read_text()
    assert text.count("PLACEHOLDER") >= 11 and "TO BE REPLACED BY THE PROBE'S MEASUREMENTS" in text


@pytest.mark.parametrize(
    "change",
    [("version: 1", "version: 2"), ('rate_bps: "0.20"', "rate_bps: 0.20"), ('rate_bps: "0.20"', 'rate_bps: "0.205"'),
     ("min: 20000", "min: 90000"), ('spread_multiplier: "3.00"', 'spread_multiplier: "0.50"'),
     ("stale_after_ms: 2000", "stale_after_ms: -1"), ('effective_from: "2026-10-11"', "effective_from: 20261011"),
     ("  minimum:", "  minimums:"), ("rollover:", "rollovers:")],
)  # fmt: skip
def test_a_cost_model_that_is_incomplete_or_inexact_is_refused(tmp_path, change):
    text = (ROOT / "config" / "cost_model.yaml").read_text()
    assert text.count(change[0]) == 1
    path = tmp_path / "cost_model.yaml"
    path.write_text(text.replace(*change))
    with pytest.raises(CostModelError):
        load_cost_model(path)
    with pytest.raises(CostModelError):
        load_cost_model(tmp_path / "missing.yaml")


# ---- conservation ---------------------------------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    quantity=st.integers(20, 400).map(lambda n: n * 1_000),
    side=st.sampled_from(Side),
    tif=st.sampled_from([DAY, IOC, FOK]),
    limit_offset=st.none() | st.integers(-6, 6).map(lambda n: n * 5),
    book=st.lists(
        st.tuples(st.integers(-8, 8).map(lambda n: n * 5), st.integers(0, 150).map(lambda n: n * 1_000)),
        min_size=1,
        max_size=8,
    ),
)
def test_quantity_is_conserved_and_every_fill_respects_the_limit(quantity, side, tif, limit_offset, book):
    rig = Rig(commission_rate_bps_x100=20)
    rig.quote()
    limit = None if limit_offset is None else ASK + limit_offset
    order = an_order(side=side, quantity=quantity, limit=limit, tif=tif)
    events = rig.run(order, wait_ns=6 * MS)
    for move, size in book:
        rig.quote(BID + move, ASK + move, size, size)
        events += rig.after(MS)
    fills = of(events, Fill)
    total = sum(f.quantity for f in fills)
    states = {e.state for e in of(events, OrderEvent)}
    assert 0 <= total <= quantity and len({f.fill_id for f in fills}) == len(fills)
    assert (total == quantity) == (OrderState.FILLED in states)
    assert max((e.filled_quantity for e in of(events, OrderEvent)), default=0) == total
    assert sum(p.quantity for p in rig.venue.positions()) == side.sign * total
    if limit is not None:
        assert all(f.price <= limit if side is Side.BUY else f.price >= limit for f in fills)
    if tif is FOK:
        assert total in (0, quantity)
    if tif is not DAY:
        assert rig.venue.open_orders() == ()
    fees = sum(c.commission for c in of(events, CommissionEvent))
    assert len(of(events, CommissionEvent)) == len(fills) and (fees >= 2_000_000 if fills else fees == 0)
    assert [f.fill_id for f in rig.venue.executions_since(0)] == [f.fill_id for f in fills]


# ---- outside the lock, and why that is safe -------------------------------------------------------------------------


def test_the_shadow_venue_is_the_registered_venue_that_needs_no_lock_because_it_has_no_broker():
    venue = Rig().venue
    assert venue.name == "sim" and VENUES["sim"].needs_paper_lock is False
    assert not isinstance(venue, GuardedVenue) and venue.managed_accounts() == ()
    assert venue.capabilities.depth is False and venue.capabilities.historical_quotes is False
    for unsupported in (lambda: venue.subscribe_depth(EURUSD, 5), lambda: venue.historical_quotes(EURUSD, T0, 1)):
        with pytest.raises(Unsupported):
            unsupported()


OTHER_VENUES = ("hftlab.venues.ibkr", "hftlab.venues.fake", "hftlab.venues.guard")
WIRES = {"socket", "ssl", "asyncio", "http", "urllib", "ib_async", "subprocess"}


def test_nothing_under_the_shadow_venue_imports_a_broker_a_socket_or_another_venue():
    seen = 0
    for rel, tree in sources():
        if rel.startswith("src/hftlab/venues/sim/"):
            seen += 1
            for _, name in imports(tree, rel):
                assert not name.startswith(OTHER_VENUES), (rel, name)
                assert name.split(".")[0] not in WIRES, (rel, name)
            assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "open"], rel
    assert seen == 3


def test_the_port_states_still_apply():
    clock = SimClock(T0)
    venue = SimVenue(clock, INSTRUMENTS, plain_costs())
    with pytest.raises(NotReady):
        venue.place(an_order(), EURUSD)
    with pytest.raises(NotReady):
        venue.mark_reconciled()
    venue.connect()
    with pytest.raises(NotReady, match="RECONCILING"):
        venue.place(an_order(), EURUSD)
    venue.mark_reconciled()
    venue.mark_reconciled()
    venue.disconnect()
    assert venue.state is ConnectionState.DISCONNECTED and venue.server_time_ns() == T0
    with pytest.raises(NotReady):
        venue.cancel(an_order().client_order_id)
