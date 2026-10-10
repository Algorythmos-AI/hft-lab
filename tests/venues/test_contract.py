"""The venue contract: one set of scenarios, run against the fake broker and against the broker adapter.

The adapter runs over a stub of the library's client that is driven by the same fake market, so any
difference between the two columns is a difference between the fake broker and the adapter, which is exactly
what must not exist. Later phases test against the fake; this is why that is allowed.
"""
from __future__ import annotations

import dataclasses

import pytest

from hftlab.core import Fill, InstrumentKind, OrderEvent, OrderState, Quote, Side, TimeInForce
from hftlab.safety import PaperOnlyError
from hftlab.venues.fake import FakeConfig
from hftlab.venues.port import (
    CommissionEvent,
    ConnectionEvent,
    ConnectionState,
    DepthSnapshot,
    NotReady,
    OrderRefused,
    UnknownInstrument,
    UnknownOrder,
    VenueUnavailable,
)
from hftlab.venues.supervisor import Backoff
from tests.venues.kit import ASK, BID, EURUSD, EURUSD_CFD, KINDS, MS, SECOND, T0, USDJPY, Harness, an_order, brief, of

IOC, DAY = TimeInForce.IOC, TimeInForce.DAY
both = pytest.mark.parametrize("kind", KINDS)


def states(events) -> list[str]:
    return [e.state.name for e in of(events, ConnectionEvent)]


# ---- connection and the lock ----------------------------------------------------------------------------------------


@both
def test_a_connection_starts_reconciling_and_takes_orders_only_once_reconciled(kind):
    h = Harness(kind)
    h.venue.connect()
    assert h.venue.state is ConnectionState.RECONCILING and states(h.venue.poll()) == ["CONNECTING", "RECONCILING"]
    instrument = h.venue.instrument("EUR.USD", InstrumentKind.CASH)
    with pytest.raises(NotReady):
        h.venue.place(an_order(), instrument)
    h.venue.mark_reconciled()
    h.venue.place(an_order(), instrument)
    assert brief(h.venue.poll()) == [("connection", "READY"), ("order", "SUBMITTED", 0)]


@both
@pytest.mark.parametrize("accounts", [("U7654321",), ("DU1234567", "U7654321"), ()])
def test_an_account_list_that_is_not_all_paper_is_refused_after_the_handshake(kind, accounts):
    h = Harness(kind, FakeConfig(accounts=accounts))
    with pytest.raises(PaperOnlyError):
        h.venue.connect()
    assert h.venue.state is ConnectionState.REFUSED and states(h.venue.poll()) == ["CONNECTING", "REFUSED"]
    with pytest.raises(PaperOnlyError):
        h.venue.place(an_order(), EURUSD)
    assert h.after(600 * SECOND) == []
    if h.stub is not None:
        assert h.stub.calls == ["handshake 127.0.0.1:4002 as 77", "close"], "nothing was asked before the lock"


@both
@pytest.mark.parametrize(("environ", "port"), [({"MODE": "live"}, 4002), ({"MODE": "paper"}, 4001), ({}, 7497)])
def test_a_wrong_environment_or_port_is_refused_before_anything_is_dialled(kind, environ, port):
    h = Harness(kind, FakeConfig(port=port), environ=environ)
    with pytest.raises(PaperOnlyError):
        h.venue.connect()
    assert states(h.venue.poll()) == ["REFUSED"]
    if h.stub is not None:
        assert h.stub.calls == []


@both
def test_the_port_dialled_must_be_the_port_the_lock_judged(kind):
    h = Harness(kind, FakeConfig(port=4001), port=4002)  # the gate was told 4002, the venue would dial 4001
    with pytest.raises(PaperOnlyError, match="not the port the paper-only lock was given"):
        h.venue.connect()
    assert h.venue.state is ConnectionState.REFUSED
    if h.stub is not None:
        assert h.stub.calls == []


@both
def test_the_lock_is_checked_again_before_every_order(kind):
    h = Harness(kind)
    instrument = h.ready()
    h.venue.place(an_order(1), instrument)
    h.environ["MODE"] = "live"
    with pytest.raises(PaperOnlyError):
        h.venue.place(an_order(2), instrument)
    assert h.venue.state is ConnectionState.REFUSED
    assert h.market.holds(an_order(1).client_order_id) and not h.market.holds(an_order(2).client_order_id)


# ---- quotes and instruments -----------------------------------------------------------------------------------------


@both
def test_instruments_are_what_the_venue_says_they_are(kind):
    h = Harness(kind)
    h.venue.connect()
    assert h.venue.instrument("EUR.USD", InstrumentKind.CASH) == EURUSD
    assert h.venue.instrument("EUR.USD", InstrumentKind.CFD) == EURUSD_CFD
    assert h.venue.instrument("USD.JPY", InstrumentKind.CASH) == USDJPY
    for pair, form in (("USD.JPY", InstrumentKind.CFD), ("GBP.USD", InstrumentKind.CASH)):
        with pytest.raises(UnknownInstrument):
            h.venue.instrument(pair, form)


@both
def test_quotes_are_exact_integers_in_order_and_only_for_subscribed_pairs(kind):
    h = Harness(kind)
    h.ready()
    h.market.script_quote(T0 + 1 * MS, "EUR.USD", 108_505, 108_515, 750_000, 1_250_000)
    h.market.script_quote(T0 + 2 * MS, "USD.JPY", 151_230, 151_240)
    h.market.script_quote(T0 + 3 * MS, "EUR.USD", 108_500, 108_515, 750_000, 1_250_000)
    quotes = of(h.after(10 * MS), Quote)
    assert [(q.pair, q.bid, q.ask, q.bid_size, q.ask_size) for q in quotes] == [
        ("EUR.USD", 108_505, 108_515, 750_000, 1_250_000),
        ("EUR.USD", 108_500, 108_515, 750_000, 1_250_000),
    ]
    assert all(q.recv_ts_ns == T0 + 10 * MS for q in quotes) and quotes[0].seq < quotes[1].seq


@both
def test_the_server_clock_and_the_account_list(kind):
    h = Harness(kind, FakeConfig(server_clock_offset_ns=-2 * SECOND))
    h.ready()
    assert h.venue.server_time_ns() == T0 - 2 * SECOND
    assert h.venue.managed_accounts() == ("DU1234567",)


@both
def test_depth_snapshots(kind):
    h = Harness(kind, FakeConfig(depth_levels=5))
    instrument = h.ready()
    h.venue.subscribe_depth(instrument, 2)
    h.market.script_quote(T0 + MS, "EUR.USD", BID, ASK, 700_000, 900_000)
    (snapshot,) = of(h.after(5 * MS), DepthSnapshot)
    assert snapshot.bids == ((BID, 700_000), (BID - 5, 700_000))
    assert snapshot.asks == ((ASK, 900_000), (ASK + 5, 900_000))


@both
def test_historical_quotes_come_back_oldest_first_at_the_venues_times(kind):
    h = Harness(kind)
    instrument = h.ready()
    for i in range(1, 6):
        h.market.script_quote(T0 + i * SECOND, "EUR.USD", BID + 5 * i, ASK + 5 * i)
    h.after(10 * SECOND)
    history = h.venue.historical_quotes(instrument, T0 + 4 * SECOND, 3)
    assert [(q.bid, q.recv_ts_ns - T0) for q in history] == [
        (BID + 10, 2 * SECOND), (BID + 15, 3 * SECOND), (BID + 20, 4 * SECOND),
    ]  # fmt: skip


# ---- orders ---------------------------------------------------------------------------------------------------------


@both
def test_a_market_order_is_acknowledged_filled_and_charged(kind):
    h = Harness(kind)
    instrument = h.ready()
    h.venue.place(an_order(side=Side.SELL), instrument)
    events = h.venue.poll() + h.after(50 * MS)
    assert brief(events) == [
        ("order", "SUBMITTED", 0),
        ("order", "WORKING", 0),
        ("fill", "SELL", 20_000, BID, "TAKER"),
        ("order", "FILLED", 20_000),
        ("commission", 2_000_000, "USD"),
    ]
    fill, commission = of(events, Fill)[0], of(events, CommissionEvent)[0]
    assert fill.client_order_id == an_order(side=Side.SELL).client_order_id and commission.fill_id == fill.fill_id
    assert fill.ts_ns == T0 + 50 * MS and fill.venue_ts_ns == T0 + 5 * MS
    assert [p.quantity for p in h.venue.positions()] == [-20_000] and h.venue.open_orders() == ()


@both
def test_a_limit_order_rests_is_visible_and_fills_as_a_maker(kind):
    h = Harness(kind)
    instrument = h.ready()
    resting = an_order(limit=108_400)
    h.venue.place(resting, instrument)
    assert brief(h.venue.poll() + h.after(30 * MS)) == [("order", "SUBMITTED", 0), ("order", "WORKING", 0)]
    (seen,) = h.venue.open_orders()
    assert (seen.client_order_id, seen.quantity, seen.filled_quantity) == (resting.client_order_id, 20_000, 0)
    assert (seen.limit_price, seen.side, seen.tif, seen.state) == (108_400, Side.BUY, DAY, OrderState.WORKING)
    h.market.script_quote(T0 + 40 * MS, "EUR.USD", 108_390, 108_400)
    later = brief(h.after(20 * MS))
    assert ("fill", "BUY", 20_000, 108_400, "MAKER") in later and ("order", "FILLED", 20_000) in later


@both
def test_partial_fills_add_up(kind):
    h = Harness(kind, FakeConfig(fill_chunks=3))
    instrument = h.ready()
    h.venue.place(an_order(quantity=50_000), instrument)
    events = h.venue.poll() + h.after(50 * MS)
    assert [f.quantity for f in of(events, Fill)] == [16_666, 16_666, 16_668]
    assert [(e.state.name, e.filled_quantity) for e in of(events, OrderEvent)] == [
        ("SUBMITTED", 0), ("WORKING", 0), ("PARTIALLY_FILLED", 16_666), ("PARTIALLY_FILLED", 33_332),
        ("FILLED", 50_000),
    ]  # fmt: skip


@both
def test_an_immediate_order_that_cannot_trade_is_cancelled_not_rejected(kind):
    h = Harness(kind)
    instrument = h.ready()
    h.venue.place(an_order(limit=108_400, tif=IOC), instrument)
    events = h.venue.poll() + h.after(50 * MS)
    assert brief(events) == [("order", "SUBMITTED", 0), ("order", "WORKING", 0), ("order", "CANCELLED", 0)]
    assert "not immediately executable" in of(events, OrderEvent)[-1].reason


@both
def test_a_rejected_order_carries_the_brokers_reason(kind):
    h = Harness(kind, reject=lambda order: "insufficient margin")
    instrument = h.ready()
    h.venue.place(an_order(), instrument)
    events = h.venue.poll() + h.after(50 * MS)
    assert brief(events) == [("order", "SUBMITTED", 0), ("order", "REJECTED", 0)]
    assert "insufficient margin" in of(events, OrderEvent)[-1].reason and h.venue.open_orders() == ()


@both
def test_cancel_a_working_order_and_cancel_too_late(kind):
    h = Harness(kind)
    instrument = h.ready()
    resting, taken = an_order(1, limit=108_400), an_order(2)
    h.venue.place(resting, instrument)
    h.venue.place(taken, instrument)
    h.venue.poll()
    h.after(30 * MS)
    h.venue.cancel(resting.client_order_id)
    h.venue.cancel(taken.client_order_id)
    assert brief(h.after(10 * MS)) == [("order", "CANCELLED", 0), ("notice", "cancel-too-late")]
    with pytest.raises(UnknownOrder):
        h.venue.cancel(an_order(99).client_order_id)


@both
def test_orders_that_do_not_fit_the_instrument_never_leave(kind):
    h = Harness(kind)
    instrument = h.ready()
    for bad in (an_order(quantity=19_000), an_order(quantity=20_500), an_order(limit=108_512)):
        with pytest.raises(OrderRefused):
            h.venue.place(bad, instrument)
    h.venue.place(an_order(7), instrument)
    with pytest.raises(OrderRefused):
        h.venue.place(an_order(7), instrument)
    assert len(h.market.working()) == 1


@both
def test_duplicated_and_reordered_execution_reports_are_passed_on_as_they_came(kind):
    h = Harness(kind, FakeConfig(fill_chunks=2, chunk_gap_ns=0, duplicate_every=2, reorder_every=1))
    instrument = h.ready()
    h.venue.place(an_order(quantity=40_000), instrument)
    events = h.venue.poll() + h.after(50 * MS)
    fills = of(events, Fill)
    assert [f.fill_id[-1] for f in fills] == ["2", "1", "1"]
    assert sum(f.quantity for f in {f.fill_id: f for f in fills}.values()) == 40_000
    assert [(e.state.name, e.filled_quantity) for e in of(events, OrderEvent)][2:] == [
        ("FILLED", 40_000), ("PARTIALLY_FILLED", 20_000),
    ]  # fmt: skip


# ---- outages and reconciliation -------------------------------------------------------------------------------------


@both
def test_a_disconnect_mid_order_then_reconnect_reconcile_and_resume(kind):
    h = Harness(kind, FakeConfig(outages=((T0 + 1 * MS, T0 + 400 * MS),)), backoff=Backoff())
    instrument = h.ready()
    filled_blind, left_working = an_order(1, quantity=30_000), an_order(2, limit=108_400)
    h.venue.place(filled_blind, instrument)
    h.venue.place(left_working, instrument)
    assert brief(h.venue.poll()) == [("order", "SUBMITTED", 0), ("order", "SUBMITTED", 0)]
    assert brief(h.after(100 * MS)) == [("connection", "DISCONNECTED")]
    with pytest.raises(NotReady):
        h.venue.place(an_order(3), instrument)
    with pytest.raises(VenueUnavailable):
        h.venue.open_orders()
    assert brief(h.after(2 * SECOND)) == [("connection", "CONNECTING"), ("connection", "RECONCILING")]

    with pytest.raises(NotReady):
        h.venue.place(an_order(3), instrument)  # a new connection is not trusted until it is reconciled
    (still_there,) = h.venue.open_orders()
    assert still_there.client_order_id == left_working.client_order_id and still_there.state is OrderState.WORKING
    (missed,) = h.venue.executions_since(T0)
    assert (missed.client_order_id, missed.quantity, missed.price) == (filled_blind.client_order_id, 30_000, ASK)
    assert (missed.commission, missed.commission_currency, missed.venue_ts_ns) == (2_000_000, "USD", T0 + 5 * MS)
    assert [p.quantity for p in h.venue.positions()] == [30_000]

    h.venue.cancel(left_working.client_order_id)  # cancelling is allowed before the connection is reconciled
    assert brief(h.after(10 * MS)) == [("order", "CANCELLED", 0)]
    h.venue.mark_reconciled()
    h.venue.place(an_order(3), instrument)
    assert ("order", "FILLED", 20_000) in brief(h.venue.poll() + h.after(50 * MS))


@both
def test_a_reconnect_to_a_live_account_is_refused_for_good(kind):
    h = Harness(kind, FakeConfig(outages=((T0 + 1 * MS, T0 + 400 * MS),)), backoff=Backoff())
    instrument = h.ready()
    h.after(100 * MS)
    h.market.config = dataclasses.replace(h.market.config, accounts=("U7654321",))
    assert states(h.after(2 * SECOND)) == ["CONNECTING", "REFUSED"]
    with pytest.raises(PaperOnlyError):
        h.venue.place(an_order(), instrument)
    assert h.after(600 * SECOND) == []


@both
def test_quote_subscriptions_are_restored_after_a_reconnect(kind):
    h = Harness(kind, FakeConfig(outages=((T0 + 1 * MS, T0 + 400 * MS),)), backoff=Backoff())
    h.ready()
    h.market.script_quote(T0 + 200 * MS, "EUR.USD", 108_550, 108_560)
    h.market.script_quote(T0 + 5 * SECOND, "EUR.USD", 108_600, 108_610)
    h.after(100 * MS)
    h.after(2 * SECOND)
    assert brief(h.after(4 * SECOND)) == [("quote", "EUR.USD", 108_600, 108_610)]


@both
def test_executions_since_a_time(kind):
    h = Harness(kind)
    instrument = h.ready()
    h.venue.place(an_order(1), instrument)
    h.after(1 * SECOND)
    h.venue.place(an_order(2, side=Side.SELL), instrument)
    h.after(1 * SECOND)
    assert len(h.venue.executions_since(T0)) == 2
    (second,) = h.venue.executions_since(T0 + 1 * SECOND)
    assert second.side is Side.SELL and h.venue.executions_since(T0 + 10 * SECOND) == ()
    assert h.venue.positions() == ()


# ---- the two columns, side by side ----------------------------------------------------------------------------------


def a_busy_session(kind: str) -> list[tuple]:
    """Seeded quotes, a dozen orders of every sort, faults in the reports, and an outage in the middle."""
    config = FakeConfig(
        fill_chunks=2, respect_displayed_size=True, duplicate_every=5, reorder_every=4,
        outages=((T0 + 700 * MS, T0 + 1_500 * MS),),
    )  # fmt: skip
    reject = lambda order: "too large" if order.quantity > 400_000 else None  # noqa: E731
    h = Harness(kind, config, backoff=Backoff(), seed=11, reject=reject)
    instrument = h.ready()
    walk = {"start_ns": T0 + MS, "count": 600, "interval_ns": 5 * MS, "mid": 108_505, "spread": 10, "step": 5}
    h.market.generate_quotes("EUR.USD", **walk)
    out: list[tuple] = []
    for number in range(1, 13):
        side = Side.BUY if number % 3 else Side.SELL
        limit = None if number % 2 else (108_480 if side is Side.BUY else 108_530)
        size, tif = (20 + 40 * number) * 1_000, IOC if number % 4 else DAY
        order = an_order(number, side=side, quantity=size, limit=limit, tif=tif)
        try:
            h.venue.place(order, instrument)
        except NotReady:
            out.append(("refused while", h.venue.state.name))
        if number == 6:
            try:
                h.venue.cancel(an_order(4).client_order_id)
            except (NotReady, UnknownOrder) as exc:
                out.append(("cancel refused", type(exc).__name__))
        out += brief(h.venue.poll() + h.after(250 * MS))
        if h.venue.state is ConnectionState.RECONCILING:
            out.append(("open", len(h.venue.open_orders()), "executions", len(h.venue.executions_since(T0))))
            h.venue.mark_reconciled()
    out.append(("positions", tuple(p.quantity for p in h.venue.positions())))
    return out


def test_the_fake_broker_and_the_adapter_tell_the_same_story():
    fake, adapter = a_busy_session("fake"), a_busy_session("ibkr")
    assert fake == adapter
    told = {entry[0] for entry in fake}
    assert {"quote", "order", "fill", "commission", "connection", "refused while", "open", "positions"} <= told
    assert len(fake) > 400
