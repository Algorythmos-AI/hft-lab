"""The fake broker: deterministic, and able to misbehave in every way a later phase has to survive."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.core import (
    Fill,
    InstrumentKind,
    Liquidity,
    OrderEvent,
    OrderState,
    Position,
    Quote,
    Side,
    TimeInForce,
    WallClock,
)
from hftlab.venues.fake import FakeConfig, FakeMarket, build
from hftlab.venues.port import CommissionEvent, DepthSnapshot, UnknownInstrument, UnknownOrder, Unsupported
from hftlab.venues.supervisor import Backoff
from tests.venues.kit import ASK, BID, EURUSD, INSTRUMENTS, MS, SECOND, T0, a_broker, after, an_order, brief, of

IOC, FOK, DAY = TimeInForce.IOC, TimeInForce.FOK, TimeInForce.DAY


def run(config=None, *orders, quotes=(), wait_ms=100, **options):
    """Place `orders` at T0 against the default quote (plus `quotes`), wait, and return (events, broker)."""
    clock, broker = a_broker(config, **options)
    for at_ms, bid, ask, *sizes in quotes:
        broker.market.script_quote(T0 + at_ms * MS, "EUR.USD", bid, ask, *sizes)
    for order in orders:
        broker.place(order, EURUSD)
    return broker.poll() + after(clock, broker, wait_ms * MS), broker


def filled(events) -> int:
    return sum(f.quantity for f in {f.fill_id: f for f in of(events, Fill)}.values())


# ---- determinism ----------------------------------------------------------------------------------------------------


def scenario(seed: int) -> list:
    clock, broker = a_broker(FakeConfig(fill_chunks=3), seed=seed)
    walk = {"start_ns": T0 + MS, "count": 200, "interval_ns": 5 * MS, "mid": 108_505, "spread": 10, "step": 5}
    broker.market.generate_quotes("EUR.USD", **walk)
    events = []
    for number in range(1, 6):
        broker.place(an_order(number, side=Side.BUY if number % 2 else Side.SELL, tif=IOC), EURUSD)
        events += after(clock, broker, 150 * MS)
    return events


def test_the_same_seed_and_script_give_the_same_events_to_the_nanosecond():
    assert scenario(7) == scenario(7)
    assert len(scenario(7)) > 100


def test_another_seed_gives_other_quotes():
    assert of(scenario(7), Quote) != of(scenario(8), Quote)


def test_generated_quotes_are_integers_on_the_price_grid_and_never_crossed():
    quotes = of(scenario(3), Quote)
    assert quotes and all(q.bid % 5 == 0 and q.ask - q.bid == 10 and q.bid_size > 0 for q in quotes)


def test_the_market_has_no_clock_of_its_own_and_cannot_go_back():
    market = FakeMarket(INSTRUMENTS)
    market.script_quote(100, "EUR.USD", BID, ASK)
    assert [h.at_ns for h in market.advance(100)] == [100] and market.advance(200) == []
    with pytest.raises(ValueError):
        market.advance(150)
    with pytest.raises(ValueError):
        market.script_quote(50, "EUR.USD", BID, ASK)


# ---- quotes ---------------------------------------------------------------------------------------------------------


def test_quotes_arrive_in_order_stamped_with_the_time_they_were_received():
    clock, broker = a_broker()
    for i in range(1, 4):
        broker.market.script_quote(T0 + i * MS, "EUR.USD", BID + 5 * i, ASK + 5 * i)
    quotes = of(after(clock, broker, 10 * MS), Quote)
    assert [q.bid for q in quotes] == [BID + 5, BID + 10, BID + 15]
    assert {q.recv_ts_ns for q in quotes} == {T0 + 10 * MS} and [q.seq for q in quotes] == sorted(q.seq for q in quotes)


def test_an_unchanged_top_of_book_is_silent_until_it_moves_or_the_connection_is_new():
    clock, broker = a_broker()
    for i in range(1, 4):
        broker.market.script_quote(T0 + i * MS, "EUR.USD", BID, ASK)
    broker.market.script_quote(T0 + 4 * MS, "EUR.USD", BID, ASK, 1_000_000, 900_000)  # only a size changed
    assert brief(after(clock, broker, 10 * MS)) == [("quote", "EUR.USD", BID, ASK), ("quote", "EUR.USD", BID, ASK)]
    broker.unsubscribe_quotes(EURUSD)
    broker.subscribe_quotes(EURUSD)
    broker.market.script_quote(T0 + 11 * MS, "EUR.USD", BID, ASK, 1_000_000, 900_000)
    assert len(of(after(clock, broker, 5 * MS), Quote)) == 1, "a fresh subscription reports the book it finds"


def test_only_subscribed_pairs_are_delivered():
    clock, broker = a_broker()
    broker.market.script_quote(T0 + MS, "USD.JPY", 151_230, 151_240)
    broker.market.script_quote(T0 + MS, "EUR.USD", BID, ASK)
    assert brief(after(clock, broker, 5 * MS)) == [("quote", "EUR.USD", BID, ASK)]
    broker.unsubscribe_quotes(EURUSD)
    broker.market.script_quote(T0 + 6 * MS, "EUR.USD", BID, ASK)
    assert after(clock, broker, 5 * MS) == []


# ---- fills from the top of the book ---------------------------------------------------------------------------------


def test_a_market_buy_is_acknowledged_then_filled_at_the_ask():
    events, broker = run(None, an_order(side=Side.BUY))
    assert brief(events) == [
        ("order", "SUBMITTED", 0),
        ("order", "WORKING", 0),
        ("fill", "BUY", 20_000, ASK, "TAKER"),
        ("order", "FILLED", 20_000),
        ("commission", 2_000_000, "USD"),
    ]
    assert broker.positions()[0].quantity == 20_000 and broker.open_orders() == ()


def test_a_market_sell_is_filled_at_the_bid():
    events, broker = run(None, an_order(side=Side.SELL))
    assert ("fill", "SELL", 20_000, BID, "TAKER") in brief(events) and broker.positions()[0].quantity == -20_000


def test_the_delays_are_the_configured_ones():
    clock, broker = a_broker(FakeConfig(ack_delay_ns=3 * MS, fill_delay_ns=9 * MS))
    broker.place(an_order(), EURUSD)
    broker.poll()
    assert after(clock, broker, 2 * MS) == []
    assert brief(after(clock, broker, 1 * MS)) == [("order", "WORKING", 0)]
    assert after(clock, broker, 5 * MS) == []
    fill = of(after(clock, broker, 1 * MS), Fill)[0]
    assert fill.venue_ts_ns == T0 + 9 * MS and fill.ts_ns == T0 + 9 * MS


def test_a_limit_order_away_from_the_market_rests_and_fills_when_the_quote_comes_to_it():
    resting = an_order(side=Side.BUY, limit=108_400)
    clock, broker = a_broker()
    broker.market.script_quote(T0 + 50 * MS, "EUR.USD", 108_390, 108_400)
    broker.place(resting, EURUSD)
    assert brief(broker.poll() + after(clock, broker, 30 * MS)) == [("order", "SUBMITTED", 0), ("order", "WORKING", 0)]
    assert [o.client_order_id for o in broker.open_orders()] == [resting.client_order_id]
    later = after(clock, broker, 40 * MS)
    assert ("fill", "BUY", 20_000, 108_400, "MAKER") in brief(later) and broker.open_orders() == ()


def test_a_marketable_limit_order_takes_the_quoted_price_not_its_limit():
    events, _ = run(None, an_order(side=Side.BUY, limit=108_600))
    assert ("fill", "BUY", 20_000, ASK, "TAKER") in brief(events)


@pytest.mark.parametrize("tif", [IOC, FOK])
def test_an_immediate_order_that_cannot_trade_is_cancelled(tif):
    events, broker = run(None, an_order(side=Side.BUY, limit=108_400, tif=tif))
    assert brief(events) == [("order", "SUBMITTED", 0), ("order", "WORKING", 0), ("order", "CANCELLED", 0)]
    assert broker.open_orders() == () and broker.positions() == ()


def test_an_immediate_order_with_no_quote_at_all_is_cancelled():
    clock, broker = a_broker(quote=False)
    broker.place(an_order(tif=IOC), EURUSD)
    events = after(clock, broker, 50 * MS)
    assert of(events, OrderEvent)[-1].state is OrderState.CANCELLED and "no quote" in of(events, OrderEvent)[-1].reason


def test_a_resting_market_order_waits_for_the_first_quote():
    clock, broker = a_broker(quote=False)
    broker.place(an_order(tif=DAY), EURUSD)
    assert of(after(clock, broker, 50 * MS), Fill) == []
    broker.market.script_quote(T0 + 60 * MS, "EUR.USD", BID, ASK)
    assert filled(after(clock, broker, 20 * MS)) == 20_000


# ---- partial fills --------------------------------------------------------------------------------------------------


def test_a_fill_can_be_reported_as_several_executions():
    events, _ = run(FakeConfig(fill_chunks=3, chunk_gap_ns=2 * MS), an_order(quantity=50_000))
    fills = of(events, Fill)
    assert [f.quantity for f in fills] == [16_666, 16_666, 16_668]
    assert [f.venue_ts_ns - T0 for f in fills] == [5 * MS, 7 * MS, 9 * MS] and len({f.fill_id for f in fills}) == 3
    assert [(e.state.name, e.filled_quantity) for e in of(events, OrderEvent)][2:] == [
        ("PARTIALLY_FILLED", 16_666), ("PARTIALLY_FILLED", 33_332), ("FILLED", 50_000),
    ]  # fmt: skip
    assert len(of(events, CommissionEvent)) == 3


def test_an_immediate_order_takes_what_is_shown_and_the_rest_is_cancelled():
    config = FakeConfig(respect_displayed_size=True)
    events, broker = run(config, an_order(quantity=50_000, tif=IOC), quotes=[(1, BID, ASK, 30_000, 30_000)])
    assert brief(events) == [
        ("order", "SUBMITTED", 0),
        ("quote", "EUR.USD", BID, ASK),
        ("order", "WORKING", 0),
        ("fill", "BUY", 30_000, ASK, "TAKER"),
        ("order", "PARTIALLY_FILLED", 30_000),
        ("commission", 2_000_000, "USD"),
        ("order", "CANCELLED", 30_000),
    ]
    assert broker.positions()[0].quantity == 30_000


def test_fill_or_kill_does_not_trade_at_all_when_the_size_is_not_there():
    config = FakeConfig(respect_displayed_size=True)
    events, _ = run(config, an_order(quantity=50_000, tif=FOK), quotes=[(1, BID, ASK, 30_000, 30_000)])
    assert of(events, Fill) == [] and of(events, OrderEvent)[-1].state is OrderState.CANCELLED


def test_a_resting_order_takes_the_shown_size_from_each_new_quote():
    config = FakeConfig(respect_displayed_size=True)
    ticks = [(1, BID, ASK, 30_000, 30_000), (20, BID, ASK, 15_000, 15_000), (40, BID, ASK, 40_000, 40_000)]
    events, _ = run(config, an_order(quantity=50_000, tif=DAY), quotes=ticks)
    assert [(f.quantity, f.liquidity) for f in of(events, Fill)] == [
        (30_000, Liquidity.TAKER), (15_000, Liquidity.MAKER), (5_000, Liquidity.MAKER),
    ]  # fmt: skip
    assert of(events, OrderEvent)[-1].state is OrderState.FILLED


@settings(max_examples=120)
@given(
    quantity=st.integers(20, 500).map(lambda n: n * 1_000),
    chunks=st.integers(1, 7),
    sizes=st.lists(st.integers(0, 120).map(lambda n: n * 1_000), min_size=1, max_size=6),
    tif=st.sampled_from([DAY, IOC, FOK]),
    side=st.sampled_from(Side),
    respect=st.booleans(),
    faults=st.tuples(st.integers(0, 3), st.integers(0, 3)),
)
def test_quantity_is_conserved_across_partial_fills(quantity, chunks, sizes, tif, side, respect, faults):
    duplicate, reorder = faults
    config = FakeConfig(
        fill_chunks=chunks, respect_displayed_size=respect, duplicate_every=duplicate, reorder_every=reorder
    )
    ticks = [(1 + 10 * i, BID, ASK, size, size) for i, size in enumerate(sizes)]
    events, broker = run(config, an_order(quantity=quantity, side=side, tif=tif), quotes=ticks, wait_ms=200)
    distinct = {f.fill_id: f for f in of(events, Fill)}
    total = sum(f.quantity for f in distinct.values())
    states = {e.state for e in of(events, OrderEvent)}
    assert 0 <= total <= quantity and all(f.quantity > 0 and f.side is side for f in distinct.values())
    assert max((e.filled_quantity for e in of(events, OrderEvent)), default=0) == total
    assert (total == quantity) == (OrderState.FILLED in states)
    position = Position("EUR.USD")
    for fill in distinct.values():
        position = position.apply(fill)
    assert position.quantity == side.sign * total == sum(p.quantity for p in broker.positions())
    assert len(of(events, CommissionEvent)) == len(distinct)
    if tif is not DAY:  # an immediate order always ends, though a reordered report may be the last one delivered
        assert states & {OrderState.FILLED, OrderState.CANCELLED} and broker.open_orders() == ()
    if tif is FOK:
        assert total in (0, quantity)
    if not respect:
        assert total == quantity


# ---- rejects and cancels --------------------------------------------------------------------------------------------


def test_a_rejected_order_is_reported_with_the_brokers_reason_and_never_works():
    reject = lambda order: "insufficient margin" if order.quantity > 100_000 else None  # noqa: E731
    events, broker = run(None, an_order(1, quantity=500_000), an_order(2), reject=reject)
    by_state = {e.state.name: e for e in of(events, OrderEvent) if e.client_order_id == an_order(1).client_order_id}
    assert set(by_state) == {"SUBMITTED", "REJECTED"} and by_state["REJECTED"].reason == "insufficient margin"
    assert filled(events) == 20_000 and broker.open_orders() == ()


def test_a_working_order_can_be_cancelled():
    clock, broker = a_broker()
    resting = an_order(limit=108_400)
    broker.place(resting, EURUSD)
    after(clock, broker, 20 * MS)
    broker.cancel(resting.client_order_id)
    assert broker.poll() == []
    assert brief(after(clock, broker, 5 * MS)) == [("order", "CANCELLED", 0)] and broker.open_orders() == ()


def test_a_cancel_that_arrives_after_the_fill_is_a_notice_not_an_order_event():
    clock, broker = a_broker()
    broker.place(an_order(), EURUSD)
    after(clock, broker, 50 * MS)
    broker.cancel(an_order().client_order_id)
    assert brief(after(clock, broker, 5 * MS)) == [("notice", "cancel-too-late")]


def test_cancelling_between_two_executions_stops_the_rest():
    clock, broker = a_broker(FakeConfig(fill_chunks=4, chunk_gap_ns=10 * MS))
    broker.place(an_order(quantity=40_000), EURUSD)
    assert filled(after(clock, broker, 12 * MS)) == 10_000  # the first execution, at 5 ms
    broker.cancel(an_order().client_order_id)  # takes effect at 14 ms, before the execution due at 15 ms
    events = after(clock, broker, 100 * MS)
    assert brief(events) == [("order", "CANCELLED", 10_000)]
    assert broker.positions()[0].quantity == 10_000 and broker.open_orders() == ()


def test_an_order_the_broker_never_saw_cannot_be_cancelled():
    _, broker = a_broker()
    with pytest.raises(UnknownOrder):
        broker.cancel(an_order(99).client_order_id)


# ---- duplicates and reordering --------------------------------------------------------------------------------------


def test_execution_reports_can_be_duplicated():
    events, broker = run(FakeConfig(fill_chunks=2, duplicate_every=1), an_order(quantity=40_000))
    fills = of(events, Fill)
    assert len(fills) == 4 and len({f.fill_id for f in fills}) == 2
    assert sum(f.quantity for f in fills) == 80_000 and filled(events) == 40_000
    assert broker.positions()[0].quantity == 40_000, "the broker itself filled the order once"


def test_execution_reports_can_arrive_out_of_order():
    events, _ = run(FakeConfig(fill_chunks=2, chunk_gap_ns=0, reorder_every=1), an_order(quantity=40_000))
    order_events = [(e.state.name, e.filled_quantity) for e in of(events, OrderEvent)]
    assert order_events[2:] == [("FILLED", 40_000), ("PARTIALLY_FILLED", 20_000)]
    assert [f.fill_id[-1] for f in of(events, Fill)] == ["2", "1"] and filled(events) == 40_000


# ---- outages --------------------------------------------------------------------------------------------------------


def test_a_disconnect_in_the_middle_of_an_order_loses_the_reports_but_not_the_order():
    config = FakeConfig(outages=((T0 + 1 * MS, T0 + 400 * MS),))
    clock, broker = a_broker(config, backoff=Backoff())
    order = an_order(quantity=30_000)
    broker.place(order, EURUSD)
    assert brief(broker.poll()) == [("order", "SUBMITTED", 0)]
    lost = after(clock, broker, 100 * MS)
    assert brief(lost) == [("connection", "DISCONNECTED")], "the acknowledgement and the fill were never delivered"
    reconnected = after(clock, broker, 2 * SECOND)
    assert brief(reconnected) == [("connection", "CONNECTING"), ("connection", "RECONCILING")]
    assert broker.open_orders() == ()
    (missed,) = broker.executions_since(T0)
    assert (missed.client_order_id, missed.quantity, missed.price) == (order.client_order_id, 30_000, ASK)
    assert missed.venue_ts_ns == T0 + 5 * MS and missed.ts_ns == clock.now_ns() and missed.commission == 2_000_000
    assert broker.positions()[0].quantity == 30_000


def test_an_order_still_working_after_an_outage_is_visible_and_can_be_cancelled_before_resuming():
    config = FakeConfig(outages=((T0 + 1 * MS, T0 + 400 * MS),))
    clock, broker = a_broker(config, backoff=Backoff())
    resting = an_order(limit=108_400)
    broker.place(resting, EURUSD)
    after(clock, broker, 100 * MS)
    after(clock, broker, 2 * SECOND)
    (seen,) = broker.open_orders()
    assert (seen.client_order_id, seen.limit_price) == (resting.client_order_id, 108_400)
    assert seen.state is OrderState.WORKING
    broker.cancel(resting.client_order_id)  # allowed while RECONCILING
    assert brief(after(clock, broker, 10 * MS)) == [("order", "CANCELLED", 0)]
    broker.mark_reconciled()


def test_executions_can_be_asked_for_since_a_time():
    clock, broker = a_broker()
    broker.place(an_order(1), EURUSD)
    after(clock, broker, 50 * MS)
    broker.place(an_order(2, side=Side.SELL), EURUSD)
    after(clock, broker, 50 * MS)
    assert len(broker.executions_since(T0)) == 2
    (second,) = broker.executions_since(T0 + 50 * MS)
    assert second.side is Side.SELL and second.commission_currency == "USD"
    assert broker.executions_since(T0 + 10 * SECOND) == () and broker.positions() == ()


# ---- capabilities ---------------------------------------------------------------------------------------------------


def test_depth_is_off_unless_configured_and_then_arrives_with_each_quote():
    _, plain = a_broker()
    assert plain.capabilities.depth is False
    with pytest.raises(Unsupported):
        plain.subscribe_depth(EURUSD, 5)
    clock, deep = a_broker(FakeConfig(depth_levels=5))
    deep.subscribe_depth(EURUSD, 3)
    deep.market.script_quote(T0 + MS, "EUR.USD", BID, ASK, 700_000, 900_000)
    (snapshot,) = of(after(clock, deep, 5 * MS), DepthSnapshot)
    assert snapshot.bids == ((BID, 700_000), (BID - 5, 700_000), (BID - 10, 700_000))
    assert snapshot.asks == ((ASK, 900_000), (ASK + 5, 900_000), (ASK + 10, 900_000))
    deep.unsubscribe_depth(EURUSD)
    deep.market.script_quote(T0 + 6 * MS, "EUR.USD", BID, ASK)
    assert of(after(clock, deep, 5 * MS), DepthSnapshot) == []
    with pytest.raises(ValueError):
        deep.subscribe_depth(EURUSD, 0)


def test_historical_quotes_are_the_scripted_past_or_unsupported():
    clock, broker = a_broker()
    for i in range(1, 6):
        broker.market.script_quote(T0 + i * SECOND, "EUR.USD", BID + 5 * i, ASK + 5 * i)
    after(clock, broker, 10 * SECOND)
    history = broker.historical_quotes(EURUSD, T0 + 4 * SECOND, 3)
    assert [q.bid for q in history] == [BID + 10, BID + 15, BID + 20]
    assert [q.recv_ts_ns - T0 for q in history] == [2 * SECOND, 3 * SECOND, 4 * SECOND]
    _, without = a_broker(FakeConfig(history=False))
    with pytest.raises(Unsupported):
        without.historical_quotes(EURUSD, T0, 3)


def test_instrument_lookup_and_the_server_clock():
    clock, broker = a_broker(FakeConfig(server_clock_offset_ns=-250 * MS))
    assert broker.instrument("EUR.USD", InstrumentKind.CFD).kind is InstrumentKind.CFD
    with pytest.raises(UnknownInstrument):
        broker.instrument("USD.JPY", InstrumentKind.CFD)
    with pytest.raises(UnknownInstrument):
        broker.instrument("GBP.USD", InstrumentKind.CASH)
    assert broker.server_time_ns() == clock.now_ns() - 250 * MS
    assert broker.managed_accounts() == ("DU1234567",) and broker.name == "fake"
    assert broker.capabilities.kinds == {InstrumentKind.CASH, InstrumentKind.CFD} and not broker.capabilities.modify


# ---- a clock the fake may drive -------------------------------------------------------------------------------------


def test_with_auto_advance_a_polling_loop_moves_time_to_the_next_happening():
    clock, broker = a_broker(auto_advance=True)
    broker.market.script_quote(T0 + 30 * MS, "EUR.USD", BID, ASK)
    broker.market.script_quote(T0 + 70 * MS, "EUR.USD", BID + 5, ASK + 5)
    seen = []
    while clock.now_ns() < T0 + 200 * MS:
        events = broker.poll(timeout_ns=50 * MS)
        seen.append(((clock.now_ns() - T0) // MS, len(events)))
    assert seen == [(30, 1), (70, 1), (120, 0), (170, 0), (220, 0)]


def test_with_auto_advance_an_outage_is_waited_out_and_the_reconnect_happens_on_schedule():
    config = FakeConfig(outages=((T0 + 10 * MS, T0 + 800 * MS),))
    clock, broker = a_broker(config, backoff=Backoff(), auto_advance=True)
    states = []
    while clock.now_ns() < T0 + 3 * SECOND:
        states += [e[1] for e in brief(broker.poll(timeout_ns=500 * MS)) if e[0] == "connection"]
    assert states == ["DISCONNECTED", "CONNECTING", "RECONCILING"]


def test_a_zero_timeout_never_moves_the_clock_and_only_a_sim_clock_can_be_driven():
    clock, broker = a_broker(auto_advance=True)
    broker.market.script_quote(T0 + 30 * MS, "EUR.USD", BID, ASK)
    assert broker.poll() == [] and clock.now_ns() == T0
    with pytest.raises(TypeError):
        build(WallClock(), INSTRUMENTS, auto_advance=True)
