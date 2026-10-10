"""Faults injected into the whole order path: the gate, the order manager and the reconciler on the fake broker."""
from __future__ import annotations

from hftlab.calendar import next_close, next_open, rollover_near
from hftlab.core import Fill, Side
from hftlab.venues.fake import FakeConfig
from hftlab.venues.port import ConnectionState
from tests.exec.kit import ASK, BID, MS, SECOND, T0, TRADED, Floor
from tests.faults.kit import consistent
from tests.risk.kit import FLATTEN

BUY, SELL = Side.BUY, Side.SELL
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
READY, RECONCILING, DISCONNECTED = ConnectionState.READY, ConnectionState.RECONCILING, ConnectionState.DISCONNECTED


def outage(start_ms: int, end_ms: int, **more) -> FakeConfig:
    return FakeConfig(outages=((T0 + start_ms * MS, T0 + end_ms * MS),), **more)


def wait_for_the_venue(floor: Floor, limit: int = 600) -> None:
    for _ in range(limit):
        if floor.broker.state in (RECONCILING, READY):
            return
        floor.pump(SECOND)
    raise AssertionError("the venue never came back")


# ---- the connection -------------------------------------------------------------------------------------------------


def test_a_disconnect_in_the_middle_of_a_partly_filled_order_loses_reports_and_no_fill(tmp_path):
    floor = Floor(tmp_path, outage(60, 900, fill_chunks=3, chunk_gap_ns=40 * MS))
    order = floor.order(1, quantity=60_000)
    floor.oms.propose(order)  # at 10 ms: executions at 15, 55 and, with the link down, 95 ms
    floor.pump(30 * MS)
    assert floor.stage(order) == "PARTIALLY_FILLED" and floor.held() == (20_000, 20_000, 20_000)
    floor.pump(40 * MS)
    assert floor.broker.state is DISCONNECTED and floor.stage(order) == "UNKNOWN"
    assert floor.oms.tracked(order.client_order_id).booked == 40_000, "what was booked stays booked"
    wait_for_the_venue(floor)
    assert floor.held() == (40_000, 40_000, 60_000) and floor.oms.propose(floor.order(2)).code == "NOT_RECONCILED"
    assert floor.settle()
    assert floor.stage(order) == "FILLED" and floor.held() == (60_000, 60_000, 60_000)
    assert [f.quantity for f in floor.events if isinstance(f, Fill)] == [20_000, 20_000], "the third was never reported"
    assert floor.oms.faults == ()
    consistent(floor)


def test_a_gateway_that_restarts_for_minutes_with_an_order_working_and_a_position_held(tmp_path):
    floor = Floor(tmp_path, outage(200, 5 * 60 * 1_000))
    floor.trade(1, quantity=40_000)
    resting = floor.trade(2, SELL, limit=108_600)
    floor.pump(100 * MS)
    assert floor.broker.state is DISCONNECTED
    tried = 0
    while floor.broker.state is DISCONNECTED:  # the venue retries on its own schedule; the trader only polls
        tried += 1
        assert floor.oms.propose(floor.order(100 + tried)).code == "NOT_RECONCILED"
        assert not floor.reconciler.check().asked and not floor.oms.reconcile()
        assert tried < 60
        floor.pump(10 * SECOND)
    assert floor.clock.now_ns() >= T0 + 5 * MINUTE and floor.broker.state is RECONCILING
    assert floor.oms.propose(floor.order(99)).code == "NOT_RECONCILED", "connected is not yet safe"
    assert [o.client_order_id for o in floor.broker.open_orders()] == [resting.client_order_id], "it worked all along"
    assert floor.settle() and floor.stage(resting) == "CANCELLED" and floor.market.working() == []
    floor.tick()
    floor.pump(10 * MS)
    assert floor.oms.propose(floor.order(3, SELL, 40_000)).sent
    floor.pump(50 * MS)
    assert floor.held() == (0, 0, 0) and floor.oms.faults == () and not floor.reconciler.check().broken
    assert floor.kinds().count("refused") == tried + 1
    consistent(floor)


def test_a_second_drop_in_the_middle_of_reconciling_starts_the_reconciliation_again(tmp_path):
    twice = ((T0 + 100 * MS, T0 + 800 * MS), (T0 + 1_130 * MS, T0 + 3_000 * MS))
    config = FakeConfig(outages=twice, cancel_delay_ns=80 * MS)
    floor = Floor(tmp_path, config)
    resting = floor.trade(1, limit=108_400)
    floor.pump(60 * MS)
    floor.pump(1_000 * MS)  # back at 1.1 s (one second of backoff after the drop was seen)
    assert floor.broker.state is RECONCILING and not floor.oms.reconcile(), "the cancel is on its way"
    floor.pump(50 * MS)  # and the link drops again before it lands
    assert floor.broker.state is DISCONNECTED and floor.oms.blind and floor.stage(resting) == "UNKNOWN"
    wait_for_the_venue(floor)
    assert floor.settle() and floor.stage(resting) == "CANCELLED" and floor.market.working() == []
    assert floor.kinds().count("reconciled") == 2, "the first connection, and the one that finally held"
    consistent(floor)


# ---- reports --------------------------------------------------------------------------------------------------------


def test_duplicated_reordered_late_and_repeated_reports_with_partial_fills_and_rejects_all_at_once(tmp_path):
    config = FakeConfig(
        fill_chunks=3, chunk_gap_ns=0, duplicate_every=2, reorder_every=3, late_ack_every=2, echo_every=4,
        respect_displayed_size=True,
    )  # fmt: skip
    floor = Floor(tmp_path, config, reject=lambda order: "not today" if order.quantity == 40_000 else None)
    for number in range(1, 21):
        side = BUY if number % 3 else SELL
        quantity = (20_000, 40_000, 60_000)[number % 3]
        floor.oms.propose(floor.order(number, side, quantity))
        floor.pump(7 * MS)
        if number % 5 == 0:
            floor.tick(BID + 5 * (number % 4), ASK + 5 * (number % 4), pair="EUR.USD")
    floor.pump(SECOND)
    floor.pump(SECOND)
    assert floor.oms.faults == () and floor.gate.latches() == ()
    rejected = [t for t in floor.oms._orders.values() if t.stage.value == "REJECTED"]
    assert len(rejected) == 7 and all(t.order.quantity == 40_000 and t.booked == 0 for t in rejected)
    delivered = [e for e in floor.events if isinstance(e, Fill)]
    assert len(delivered) > len({f.fill_id for f in delivered}) > 20, "fills did arrive more than once"
    assert not floor.reconciler.check().differences
    consistent(floor)


def test_venue_rejects_in_a_row_trip_the_breaker_and_entries_stop(tmp_path):
    floor = Floor(tmp_path, changes={"desk/reject_streak_breaker": 3}, reject=lambda order: "no")
    for number in (1, 2, 3):
        assert floor.oms.propose(floor.order(number)).sent
        floor.pump(20 * MS)
    assert floor.gate.latches() == ("REJECT_STREAK",) and floor.oms.propose(floor.order(4)).code == "REJECT_STREAK"
    assert len(floor.at_broker()) == 3
    floor.restart()
    assert floor.oms.propose(floor.order(5)).code == "REJECT_STREAK"


# ---- the clock and the market ---------------------------------------------------------------------------------------


def test_a_clock_that_jumps_an_hour_makes_every_quote_stale_and_loses_nothing(tmp_path):
    floor = Floor(tmp_path)
    floor.beat = False
    resting = floor.trade(1, limit=108_400)
    first = floor.trade(2)
    floor.clock.advance(HOUR)  # the wall clock is corrected forward, or the host slept
    assert floor.oms.propose(floor.order(3)).code == "STALE_QUOTE"
    assert floor.oms.propose(floor.order(4, SELL)).code == "STALE_QUOTE", "not even an exit, on a quote an hour old"
    floor.pump()
    assert floor.oms.propose(floor.order(5)).code == "STALE_QUOTE", "polling brings no quote if the book has not moved"
    floor.tick(BID + 5, ASK + 5, pair="EUR.USD")
    floor.pump(MS)
    assert floor.oms.propose(floor.order(6)).sent
    floor.pump(50 * MS)
    assert floor.stage(resting) == "WORKING" and floor.stage(first) == "FILLED" and floor.held()[0] == 40_000
    assert not floor.reconciler.check().broken and floor.oms.faults == ()


def test_quotes_that_stop_arriving_stop_every_order_until_one_arrives(tmp_path):
    floor = Floor(tmp_path)
    floor.beat = False  # the venue is connected and says nothing: no quote, no confirmation
    floor.trade(1)
    floor.pump(2 * SECOND)
    assert floor.oms.propose(floor.order(2)).code == "STALE_QUOTE"
    assert floor.oms.propose(floor.order(3, SELL)).code == "STALE_QUOTE"
    assert len(floor.at_broker()) == 1 and floor.broker.state is READY
    floor.beat = True  # it answers again: the book is the same, and known to be
    floor.pump(MS)
    assert floor.oms.propose(floor.order(4, SELL)).sent


def test_across_the_weekend_gap_entries_stop_before_the_close_and_nothing_is_sent_until_the_week_reopens(tmp_path):
    close = next_close(T0)
    floor = Floor(tmp_path, at_ns=close - 40 * MINUTE)
    floor.trade(1)
    floor.pump(10 * MINUTE)
    assert floor.oms.propose(floor.order(2)).code == "WEEKLY_CLOSE", "the last half hour: exits only"
    assert floor.oms.propose(floor.order(3, SELL)).sent
    floor.pump(50 * MS)
    assert floor.held() == (0, 0, 0)
    floor.pump(30 * MINUTE)
    assert floor.oms.propose(floor.order(4)).code == "MARKET_SHUT"
    sent = len(floor.at_broker())
    for hour in range(48):  # Saturday and Sunday, an hour at a time, with the broker's quotes still ticking
        floor.pump(HOUR)
        floor.tick(BID + 5 * (hour % 2), ASK + 5 * (hour % 2), pair="EUR.USD")
        if floor.clock.now_ns() < next_open(close):
            assert floor.oms.propose(floor.order(100 + hour)).code == "MARKET_SHUT"
    assert len(floor.at_broker()) == sent, "nothing was sent while the market was shut"
    floor.clock.advance_to(next_open(close) + 25 * MINUTE)  # past the rollover window the week opens inside
    floor.tick(BID + 100, ASK + 100, pair="EUR.USD")  # and the first quote of the week is somewhere else
    floor.pump(MS)
    assert floor.oms.propose(floor.order(5)).sent
    floor.pump(50 * MS)
    assert floor.held() == (20_000, 20_000, 20_000) and floor.oms.faults == ()


def test_a_spread_that_blows_out_at_the_rollover_takes_no_order_and_exits_pass_when_it_comes_back(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    floor = Floor(tmp_path, changes={"per_pair/EUR.USD/price_collar_ticks": 10}, at_ns=rollover - 15 * MINUTE)
    floor.trade(1)
    floor.pump(5 * MINUTE)
    assert floor.oms.propose(floor.order(2)).code == "ROLLOVER_WINDOW", "ten minutes before: exits only"
    floor.pump(10 * MINUTE)
    floor.tick(BID - 300, ASK + 300, pair="EUR.USD")  # the rollover: sixty times the usual spread
    floor.pump(MS)
    entry, out = floor.gate.check(floor.order(3)), floor.gate.check(floor.order(4, SELL))
    assert [r.value for r in entry.reasons] == ["PRICE_COLLAR", "SPREAD_TOO_WIDE", "ROLLOVER_WINDOW"]
    assert out.code == "PRICE_COLLAR" and not out.allowed, "nothing is sold into a blown-out book, not even an exit"
    floor.switched.add(FLATTEN)
    for close in floor.gate.flatten_orders():
        assert floor.oms.propose(close).code == "PRICE_COLLAR"
    assert len(floor.at_broker()) == 1 and floor.held()[0] == 20_000
    floor.pump(2 * MINUTE)
    floor.tick(BID, ASK, pair="EUR.USD")  # the spread comes back, still inside the window
    floor.pump(MS)
    (close,) = floor.gate.flatten_orders()
    assert floor.oms.propose(close).sent
    floor.pump(50 * MS)
    assert floor.held() == (0, 0, 0) and floor.gate.flatten_orders() == ()
    floor.switched.clear()
    assert floor.oms.propose(floor.order(5)).code == "ROLLOVER_WINDOW"
    floor.pump(18 * MINUTE)
    floor.tick(BID + 5, ASK + 5, pair="EUR.USD")
    floor.pump(MS)
    assert floor.oms.propose(floor.order(6)).sent


# ---- the books ------------------------------------------------------------------------------------------------------


def test_a_trading_session_checked_every_step_has_no_reconciliation_break(tmp_path):
    floor = Floor(tmp_path, FakeConfig(fill_chunks=2, chunk_gap_ns=20 * MS))
    for number in range(1, 13):
        floor.oms.propose(floor.order(number, BUY if number % 2 else SELL, 40_000))
        for _ in range(4):
            floor.pump(10 * MS)
            found = floor.reconciler.check()
            assert found.asked and not found.broken and found.recovered == ()
    assert (tmp_path / "reconcile.jsonl").read_bytes().count(b"break_opened") == 0
    consistent(floor)


def test_a_position_that_appears_at_the_broker_is_a_break_entries_stop_and_resume_when_it_is_explained(tmp_path):
    floor = Floor(tmp_path)
    floor.trade(1)
    floor.market.carry(TRADED[0], 30_000)
    assert not floor.reconciler.check().broken, "inside the grace time"
    assert floor.oms.propose(floor.order(2)).sent
    floor.pump(5 * SECOND)
    assert floor.reconciler.check().broken
    assert floor.oms.propose(floor.order(3)).code == "RECONCILIATION_BREAK"
    assert floor.oms.propose(floor.order(4, SELL)).sent, "exits are still allowed"
    floor.pump(50 * MS)
    floor.restart(up=False)
    floor.connect()
    assert not floor.settle(tries=3), "after a restart the connection is not even marked while the books differ"
    assert floor.reconciler.broken
    floor.market.carry(TRADED[0], -30_000)
    assert floor.settle() and not floor.reconciler.check().broken
    floor.pump(MS)
    assert floor.oms.propose(floor.order(5)).sent
