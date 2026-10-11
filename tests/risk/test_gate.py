"""The risk gate, check by check: each has its own code, and each is pessimistic in the way its test says."""
from __future__ import annotations

import pytest

from hftlab.book import BreakEvent
from hftlab.calendar import Calendar, Closure, MacroEvent, next_close, next_open, rollover_near
from hftlab.core import Fill, OrderEvent, OrderState, OrderType, Side, SimClock, TimeInForce
from hftlab.journal import Journal, verify
from hftlab.risk import (
    HARD,
    ROOM,
    SOFT,
    LatchError,
    Outcome,
    Reason,
    RiskConfigError,
    RiskGate,
    TapeRoom,
    Verdict,
    file_switches,
)
from hftlab.venues.port import CommissionEvent, ConnectionEvent, ConnectionState
from tests.risk.kit import ASK, BID, FLATTEN, KILL, MINUTE, MS, SECOND, T0, Desk, an_order, limits
from tests.venues.kit import EURUSD

HOUR = 60 * MINUTE
BUY, SELL = Side.BUY, Side.SELL
ROOMY = {
    "per_pair/EUR.USD/max_position": 10**9, "per_sleeve/control/max_position": 10**9,
    "desk/max_order_to_trade_ratio": 10**6,
}  # fmt: skip


def codes(verdict: Verdict) -> list[str]:
    return [reason.value for reason in verdict.reasons]


# ---- the verdict itself ---------------------------------------------------------------------------------------------


def test_an_order_inside_every_limit_is_allowed_and_a_verdict_never_carries_free_text(tmp_path):
    desk = Desk(tmp_path)
    verdict = desk.check(an_order())
    assert verdict == Verdict(Outcome.ALLOW, True) and verdict.reason is None and verdict.code == "ALLOW"
    refused = desk.check(an_order(2, quantity=10**7))
    assert refused.outcome is Outcome.REFUSE and not refused.allowed and refused.code == "ORDER_SIZE"
    assert all(isinstance(reason, Reason) for reason in refused.reasons)
    assert {reason.value for reason in Reason} == {reason.name for reason in Reason}, "a code is its own name"


def test_a_verdict_is_one_of_three_shapes_and_no_other_can_be_built():
    assert Verdict(Outcome.EXITS_ONLY, True, (Reason.KILL_SWITCH,)).allowed
    assert not Verdict(Outcome.EXITS_ONLY, False, (Reason.KILL_SWITCH,)).allowed
    for wrong in (
        lambda: Verdict(Outcome.ALLOW, False),
        lambda: Verdict(Outcome.ALLOW, True, (Reason.KILL_SWITCH,)),
        lambda: Verdict(Outcome.REFUSE, False),
        lambda: Verdict(Outcome.REFUSE, True, (Reason.ORDER_SIZE,)),
        lambda: Verdict(Outcome.EXITS_ONLY, True),
    ):
        with pytest.raises(ValueError):
            wrong()


def test_several_reasons_are_all_listed_refusals_first(tmp_path):
    desk = Desk(tmp_path)
    desk.switched.add(KILL)
    verdict = desk.check(an_order(quantity=10**7, limit=200_000))
    assert verdict.outcome is Outcome.REFUSE
    assert codes(verdict) == ["PRICE_COLLAR", "ORDER_SIZE", "POSITION_LIMIT", "KILL_SWITCH"]


# ---- connection and the order itself --------------------------------------------------------------------------------


def test_no_order_until_the_gate_has_been_shown_a_ready_connection_and_none_after_it_is_lost(tmp_path):
    desk = Desk(tmp_path, ready=False)
    desk.quote()
    assert desk.code(an_order(1)) == "VENUE_NOT_READY"
    for state in (ConnectionState.CONNECTING, ConnectionState.RECONCILING):
        desk.gate.observe(ConnectionEvent(state, T0, 0))
        assert desk.code(an_order(1)) == "VENUE_NOT_READY", state
    desk.gate.observe(ConnectionEvent(ConnectionState.READY, T0, 0))
    assert desk.code(an_order(1)) == "STALE_QUOTE", "the quote is from before this connection was established"
    desk.quote()
    assert desk.check(an_order(1)).allowed
    desk.gate.observe(ConnectionEvent(ConnectionState.DISCONNECTED, T0, 0))
    assert desk.code(an_order(2, SELL)) == "VENUE_NOT_READY", "not even an exit: nothing is sent while blind"


def test_an_id_is_ruled_on_once_and_must_name_the_sleeve_the_order_claims(tmp_path):
    desk = Desk(tmp_path)
    order = an_order(1)
    assert desk.check(order).allowed and desk.code(order) == "DUPLICATE_ORDER"
    refused = an_order(2, quantity=10**7)
    assert desk.code(refused) == "ORDER_SIZE" and desk.code(refused) == "ORDER_SIZE", "a refusal reserves nothing"
    borrowed = an_order(3, sleeve="ml")
    posing = type(borrowed)(
        borrowed.client_order_id, "control", "EUR.USD", BUY, 20_000, OrderType.MARKET, TimeInForce.FOK, T0
    )
    assert desk.code(posing) == "ORDER_ID", "its fills would be booked to another sleeve than its limits were for"
    foreign = type(borrowed)("manual-1", "control", "EUR.USD", BUY, 20_000, OrderType.MARKET, TimeInForce.FOK, T0)
    assert desk.code(foreign) == "ORDER_ID"


def test_a_pair_without_limits_is_refused_outright_and_a_sleeve_without_limits_may_only_exit(tmp_path):
    desk = Desk(tmp_path, {"per_pair/USD.JPY": ..., "per_sleeve/ml": ...})
    assert desk.code(an_order(1, pair="USD.JPY")) == "PAIR_NOT_CONFIGURED"
    entry = desk.check(an_order(2, sleeve="ml"))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "SLEEVE_NOT_CONFIGURED")
    desk.fill(an_order(3, sleeve="ml"), ASK)  # a position the gate never allowed, as a recovered fill would be
    out = desk.check(an_order(4, SELL, sleeve="ml"))
    assert (out.outcome, out.allowed, out.code) == (Outcome.EXITS_ONLY, True, "SLEEVE_NOT_CONFIGURED")


def test_the_gate_cannot_be_built_on_limits_for_a_pair_it_has_no_instrument_for(tmp_path):
    with pytest.raises(RiskConfigError, match=r"no instrument for: USD\.JPY"):
        RiskGate(
            limits(), [EURUSD], calendar=Calendar(), clock=SimClock(T0), switches=set, journal=Journal(tmp_path / "j")
        )


# ---- switches -------------------------------------------------------------------------------------------------------


def test_kill_stops_entries_and_leaves_exits_and_lifts_when_the_file_is_gone(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.switched.add(KILL)
    entry = desk.check(an_order(2))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "KILL_SWITCH")
    out = desk.check(an_order(3, SELL))
    assert (out.outcome, out.allowed, out.code) == (Outcome.EXITS_ONLY, True, "KILL_SWITCH")
    desk.switched.clear()
    assert desk.check(an_order(4)) == Verdict(Outcome.ALLOW, True)


def test_an_exit_is_an_order_that_cannot_add_to_the_position_or_take_it_through_zero_whatever_fills(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1, quantity=40_000)
    desk.switched.add(KILL)
    assert not desk.check(an_order(2, SELL, 60_000)).allowed, "it would leave the sleeve short"
    assert not desk.check(an_order(3, BUY, 20_000)).allowed
    first = an_order(4, SELL, 20_000)
    assert desk.check(first).allowed and desk.check(an_order(5, SELL, 20_000)).allowed
    assert not desk.check(an_order(6, SELL, 20_000)).allowed, "two exits are in flight for all that is held"
    desk.end(first)  # cancelled without a fill: its size is free again
    assert desk.check(an_order(7, SELL, 20_000)).allowed


def test_flatten_stops_entries_and_proposes_one_close_for_each_line_the_book_holds(tmp_path):
    desk = Desk(tmp_path, {"per_pair/USD.JPY/max_order_size": 20_000})
    desk.hold(1, BUY, 40_000)
    for number in (2, 3):
        desk.hold(number, SELL, 20_000, pair="USD.JPY", sleeve="ml")
    assert desk.gate.flatten_orders() == (), "nothing is proposed while the switch is off"
    desk.switched.add(FLATTEN)
    assert desk.code(an_order(4)) == "FLATTEN_SWITCH"
    closes = desk.gate.flatten_orders()
    assert [(o.sleeve, o.pair, o.side, o.quantity) for o in closes] == [
        ("control", "EUR.USD", SELL, 40_000),
        ("ml", "USD.JPY", BUY, 20_000),  # no larger than the pair's order limit; the rest follows
    ]
    assert all(o.order_type is OrderType.MARKET and o.tif is TimeInForce.IOC for o in closes)
    verdicts = [desk.check(order) for order in closes]
    assert all(v.allowed and v.outcome is Outcome.EXITS_ONLY and v.code == "FLATTEN_SWITCH" for v in verdicts)
    assert desk.gate.flatten_orders() == (), "a line with an order in flight gets no second close"
    desk.fill(closes[0], BID)
    desk.fill(closes[1], 151_240)
    rest = desk.gate.flatten_orders()
    assert [(o.pair, o.side, o.quantity) for o in rest] == [("USD.JPY", BUY, 20_000)]
    assert rest[0].client_order_id != closes[1].client_order_id
    assert desk.check(rest[0]).allowed
    desk.fill(rest[0], 151_240)
    assert desk.gate.flatten_orders() == () and desk.gate.book.open_lines() == {}


def test_a_close_is_for_what_the_book_holds_never_for_what_might_fill(tmp_path):
    desk = Desk(tmp_path)
    entry = an_order(1, BUY, 40_000)
    assert desk.check(entry).allowed
    desk.fill(entry, ASK, 20_000)  # half is filled; the order still works
    desk.switched.add(FLATTEN)
    assert desk.gate.flatten_orders() == (), "the line has an order in flight: closing now could leave it short"
    desk.end(entry, OrderState.CANCELLED, 20_000)
    (close,) = desk.gate.flatten_orders()
    assert (close.side, close.quantity) == (SELL, 20_000)


def test_a_position_nobody_here_ordered_is_closed_too_under_the_name_the_book_gives_it(tmp_path):
    desk = Desk(tmp_path)
    by_hand = Fill("m1", "manual-7", "EUR.USD", BUY, 30_000, ASK, T0)  # a fill of an order that is not the platform's
    desk.gate.observe(by_hand)
    assert desk.gate.book.open_lines() == {("unattributed", "EUR.USD"): 30_000}
    desk.switched.add(FLATTEN)
    (close,) = desk.gate.flatten_orders()
    assert (close.sleeve, close.side, close.quantity) == ("unattributed", SELL, 30_000)
    verdict = desk.check(close)
    assert verdict.allowed and codes(verdict) == ["FLATTEN_SWITCH", "SLEEVE_NOT_CONFIGURED"]
    desk.fill(close, BID)
    assert desk.gate.book.open_lines() == {} and desk.gate.flatten_orders() == ()


def test_switches_that_cannot_be_read_stop_entries_and_propose_nothing(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.switched.add(FLATTEN)
    desk.unreadable = True
    entry = desk.check(an_order(2))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "SWITCHES_UNREADABLE")
    assert desk.check(an_order(3, SELL)).allowed and desk.gate.flatten_orders() == ()


def test_a_switch_is_a_file_in_the_state_directory_and_is_on_while_it_exists(tmp_path):
    state = tmp_path / "state"
    read = file_switches(state, [KILL, FLATTEN])
    with pytest.raises(OSError, match="not there"):
        read()
    state.mkdir()
    assert read() == frozenset()
    (state / KILL).write_text("")
    (state / "PAUSE").write_text("")
    assert read() == {KILL}
    (state / FLATTEN).write_text("anything")
    assert read() == {KILL, FLATTEN}
    (state / KILL).unlink()
    assert read() == {FLATTEN}


# ---- limits ---------------------------------------------------------------------------------------------------------


def test_an_order_larger_than_the_pairs_order_limit_is_refused_entry_or_exit(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_order_size": 20_000})
    assert desk.check(an_order(1, quantity=20_000)).allowed
    assert desk.code(an_order(2, quantity=21_000)) == "ORDER_SIZE"
    assert desk.code(an_order(3, SELL, 21_000)) == "ORDER_SIZE"


def test_a_limit_price_further_from_the_mid_than_the_collar_is_refused_on_either_side(tmp_path):
    desk = Desk(tmp_path)  # the mid is 108505, the collar ten increments of five
    mid_x2, reach = BID + ASK, 50
    assert desk.check(an_order(1, BUY, limit=(mid_x2 + 2 * reach) // 2)).allowed, "at the collar"
    assert desk.code(an_order(2, BUY, limit=108_560)) == "PRICE_COLLAR"
    assert desk.code(an_order(3, BUY, limit=108_450)) == "PRICE_COLLAR", "far below the market is refused too"
    assert desk.code(an_order(4, SELL, limit=108_450)) == "PRICE_COLLAR"
    assert desk.check(an_order(5, SELL, limit=108_455)).allowed


def test_a_market_order_is_collared_by_the_touch_it_would_take(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_spread_ticks": 1_000})
    desk.quote(108_455, 108_555)  # each touch is exactly ten increments from the mid
    assert desk.check(an_order(1)).allowed
    desk.quote(108_450, 108_555)
    assert desk.code(an_order(2)) == "PRICE_COLLAR" and desk.code(an_order(3, SELL)) == "PRICE_COLLAR"


def test_orders_in_flight_count_in_full_against_the_position_limit_and_time_releases_nothing(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_position": 40_000})
    first, second = an_order(1), an_order(2)
    assert desk.check(first).allowed and desk.check(second).allowed
    assert desk.code(an_order(3)) == "POSITION_LIMIT", "nothing has filled, and both could"
    assert desk.check(an_order(4, SELL)).allowed, "the other side has its own room"
    desk.after(50 * SECOND)
    desk.quote()
    assert desk.code(an_order(5)) == "POSITION_LIMIT"
    desk.end(first)  # cancelled without a fill
    assert desk.check(an_order(6)).allowed


def test_an_order_reported_filled_stays_held_until_the_fill_itself_is_booked(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_position": 20_000})
    first = an_order(1)
    assert desk.check(first).allowed
    desk.end(first, OrderState.FILLED, 20_000)  # the report has overtaken the fill
    assert desk.gate.book.open_lines() == {}
    assert desk.code(an_order(2)) == "POSITION_LIMIT", "the book is flat, the venue says it is not"
    desk.fill(first, ASK, report=False)
    assert desk.code(an_order(3)) == "POSITION_LIMIT" and desk.check(an_order(4, SELL)).allowed


def test_a_partly_filled_order_that_is_cancelled_releases_only_what_did_not_fill(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_position": 60_000})
    first = an_order(1, quantity=40_000)
    assert desk.check(first).allowed
    desk.end(first, OrderState.CANCELLED, 20_000)  # cancelled with 20,000 filled, the fill not yet seen
    assert desk.check(an_order(2, quantity=40_000)).allowed
    assert desk.code(an_order(3, quantity=20_000)) == "POSITION_LIMIT", "20,000 owed + 40,000 in flight is the cap"
    desk.fill(first, ASK, 20_000, report=False)
    assert desk.code(an_order(4, quantity=20_000)) == "POSITION_LIMIT"


def test_the_cap_holds_for_each_sleeve_and_for_the_desk_as_a_whole(tmp_path):
    desk = Desk(tmp_path)  # 60,000 a sleeve, 100,000 the pair
    for number in (1, 2, 3):
        desk.hold(number)
    assert desk.code(an_order(4)) == "POSITION_LIMIT", "the sleeve's own cap"
    desk.hold(5, sleeve="ml", quantity=40_000)
    assert desk.code(an_order(6, sleeve="ml")) == "POSITION_LIMIT", "the sleeve has room, the desk has none"
    assert desk.check(an_order(7, SELL, sleeve="ml")).allowed


def test_a_position_taken_past_the_cap_by_other_means_can_be_reduced_and_not_added_to(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_position": 20_000})
    desk.fill(an_order(1, quantity=60_000), ASK)  # a fill of an order this gate never ruled on
    assert desk.code(an_order(2)) == "POSITION_LIMIT"
    assert desk.check(an_order(3, SELL, 60_000)).allowed


def test_no_new_pair_is_opened_beyond_the_number_allowed_at_once(tmp_path):
    desk = Desk(tmp_path, {"desk/max_open_pairs": 1})
    waiting = an_order(1)
    assert desk.check(waiting).allowed, "an order in flight opens its pair"
    other = desk.check(an_order(2, pair="USD.JPY"))
    assert (other.outcome, other.allowed, other.code) == (Outcome.EXITS_ONLY, False, "MAX_OPEN_PAIRS")
    assert desk.check(an_order(3)).allowed, "more of a pair that is open is not a new pair"
    desk.end(waiting)
    desk.end(an_order(3))
    assert desk.check(an_order(4, pair="USD.JPY")).allowed


def test_a_sleeve_sends_no_more_orders_in_a_minute_than_its_limit_and_exits_count_too(tmp_path):
    desk = Desk(tmp_path, {**ROOMY, "per_sleeve/control/max_orders_per_minute": 3})
    desk.hold(1)
    for number in (2, 3):
        assert desk.check(an_order(number)).allowed
        desk.after(10 * SECOND)
        desk.quote()
    assert desk.code(an_order(4)) == "ORDER_RATE_SLEEVE"
    assert desk.code(an_order(5, SELL)) == "ORDER_RATE_SLEEVE", "a flood of exits is still a flood"
    assert desk.check(an_order(6, sleeve="ml")).allowed, "another sleeve has its own count"
    desk.after(40 * SECOND - 1)
    desk.quote()
    assert desk.code(an_order(7)) == "ORDER_RATE_SLEEVE"
    desk.after(1)
    assert desk.check(an_order(8)).allowed, "sixty seconds after the first, it no longer counts"


def test_the_desk_sends_no_more_orders_in_a_minute_than_its_limit_whoever_asks(tmp_path):
    desk = Desk(tmp_path, {**ROOMY, "desk/max_orders_per_minute": 3, "per_sleeve/ml/max_position": 10**9})
    assert desk.check(an_order(1)).allowed and desk.check(an_order(2, sleeve="ml")).allowed
    assert desk.check(an_order(3)).allowed
    assert desk.code(an_order(4, sleeve="ml")) == "ORDER_RATE_DESK"
    assert desk.code(an_order(5, quantity=10**7)) == "ORDER_SIZE", "a refused order is not counted"
    desk.after(MINUTE)
    desk.quote()
    assert desk.check(an_order(6, sleeve="ml")).allowed


def test_orders_that_do_not_trade_run_into_the_order_to_trade_ratio(tmp_path):
    desk = Desk(tmp_path, {**ROOMY, "desk/max_order_to_trade_ratio": 3, "desk/order_to_trade_window_seconds": 100})
    sent = [an_order(number, limit=108_500) for number in (1, 2, 3)]
    for order in sent:
        assert desk.check(order).allowed
        desk.end(order)  # cancelled, nothing traded
    refused = desk.check(an_order(4))
    assert codes(refused) == ["ORDER_TO_TRADE_SLEEVE", "ORDER_TO_TRADE_DESK"] and refused.outcome is Outcome.REFUSE
    assert desk.code(an_order(5, sleeve="ml")) == "ORDER_TO_TRADE_DESK", "the desk's ratio is everybody's"
    desk.after(100 * SECOND)
    desk.quote()
    assert desk.check(an_order(6)).allowed, "the window has rolled past all three"


def test_a_trade_buys_room_and_a_cancel_uses_it(tmp_path):
    desk = Desk(tmp_path, {**ROOMY, "desk/max_order_to_trade_ratio": 3})
    desk.hold(1)
    desk.hold(2)  # two orders, two trades: six messages are allowed in the window
    for number in (3, 4):
        assert desk.check(an_order(number, limit=108_500)).allowed
    desk.gate.count_cancel(an_order(3, limit=108_500).client_order_id)
    assert desk.check(an_order(5)).allowed, "the sixth message"
    assert desk.code(an_order(6)) == "ORDER_TO_TRADE_SLEEVE"
    desk.fill(an_order(5), ASK)
    assert desk.check(an_order(7)).allowed, "a third order traded: nine are allowed"


# ---- market guards --------------------------------------------------------------------------------------------------


def test_no_order_without_a_quote_or_on_one_the_venue_has_not_confirmed_lately(tmp_path):
    desk = Desk(tmp_path, ready=False)
    desk.gate.observe(ConnectionEvent(ConnectionState.READY, T0, 0))
    assert desk.code(an_order(1)) == "NO_QUOTE"
    desk.quote()
    desk.after(2_000 * MS)
    assert desk.check(an_order(2)).allowed, "exactly as old as the limit"
    desk.after(1)
    assert desk.code(an_order(3)) == "STALE_QUOTE" and desk.code(an_order(4, SELL)) == "STALE_QUOTE"
    desk.gate.quiet("EUR.USD", desk.clock.now_ns())  # the venue answered: the book has simply not moved
    assert desk.check(an_order(5)).allowed
    desk.gate.quiet("USD.JPY", desk.clock.now_ns())
    assert desk.code(an_order(6, pair="USD.JPY")) == "NO_QUOTE", "a confirmation of nothing confirms nothing"


def test_a_crossed_quote_refuses_everything_and_a_wide_one_stops_entries(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.quote(108_510, 108_500)
    assert desk.code(an_order(2)) == "CROSSED_QUOTE" and desk.code(an_order(3, SELL)) == "CROSSED_QUOTE"
    desk.quote(108_500, 108_520)  # four increments: at the limit
    assert desk.check(an_order(4)).allowed
    desk.quote(108_495, 108_520)
    wide = desk.check(an_order(5))
    assert (wide.outcome, wide.allowed, wide.code) == (Outcome.EXITS_ONLY, False, "SPREAD_TOO_WIDE")
    assert desk.check(an_order(6, SELL)).allowed, "getting out is still allowed"
    desk.quote(108_500, 108_500)
    assert desk.check(an_order(7)).allowed, "a locked quote is a quote"


def test_nothing_is_sent_while_the_market_is_shut_for_the_weekend_or_a_holiday(tmp_path):
    friday_close = next_close(T0)
    assert Desk(tmp_path / "a", at_ns=friday_close - HOUR).check(an_order(1, at_ns=friday_close - HOUR)).allowed
    shut = Desk(tmp_path / "b", at_ns=friday_close)
    assert shut.code(an_order(1)) == "MARKET_SHUT" and shut.code(an_order(2, SELL)) == "MARKET_SHUT"
    opened = next_open(friday_close)
    assert Desk(tmp_path / "c", at_ns=opened).code(an_order(1)) == "ROLLOVER_WINDOW", "the week opens inside one"
    assert Desk(tmp_path / "c2", at_ns=opened + 10 * MINUTE).check(an_order(1)).allowed
    holiday = Calendar(closures=(Closure("a holiday", T0 - HOUR, T0 + HOUR),))
    assert Desk(tmp_path / "d", calendar=holiday).code(an_order(1)) == "MARKET_SHUT"
    assert Desk(tmp_path / "e", calendar=holiday, at_ns=T0 + HOUR).check(an_order(1)).allowed


def test_a_macro_blackout_stops_entries_in_pairs_of_its_currencies_only(tmp_path):
    release = MacroEvent("a release", T0 + 30 * MINUTE, frozenset({"JPY"}), 5 * MINUTE, 10 * MINUTE)
    calendar = Calendar(events=(release,))
    edges = ((25 * MINUTE - 1, False), (25 * MINUTE, True), (40 * MINUTE, True), (40 * MINUTE + 1, False))
    for later, blacked_out in edges:
        desk = Desk(tmp_path / str(later), calendar=calendar, at_ns=T0 + later)
        yen = desk.check(an_order(1, pair="USD.JPY"))
        assert (yen.code == "MACRO_BLACKOUT") == blacked_out and yen.allowed != blacked_out, later
        assert desk.check(an_order(2)).allowed, "EUR.USD has no yen in it"
        desk.fill(an_order(3, pair="USD.JPY"), 151_240)
        assert desk.check(an_order(4, SELL, pair="USD.JPY")).allowed, "an exit goes through a blackout"


def test_around_the_daily_rollover_only_exits_pass(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    edges = ((-10 * MINUTE - 1, False), (-10 * MINUTE, True), (0, True), (20 * MINUTE, True), (20 * MINUTE + 1, False))
    for offset, inside in edges:
        desk = Desk(tmp_path / str(offset), at_ns=rollover + offset)
        desk.fill(an_order(1), ASK)
        entry = desk.check(an_order(2))
        assert (entry.code == "ROLLOVER_WINDOW") == inside and entry.allowed != inside, offset
        out = desk.check(an_order(3, SELL))
        assert out.allowed and (out.outcome is Outcome.EXITS_ONLY) == inside


def test_in_the_last_minutes_before_the_weekly_close_only_exits_pass(tmp_path):
    close = next_close(T0)
    early = Desk(tmp_path / "early", at_ns=close - 30 * MINUTE - 1)
    assert early.check(an_order(1)).allowed
    late = Desk(tmp_path / "late", at_ns=close - 30 * MINUTE)
    late.fill(an_order(1), ASK)
    entry = late.check(an_order(2))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "WEEKLY_CLOSE")
    assert late.check(an_order(3, SELL)).allowed
    last = Desk(tmp_path / "last", at_ns=close - 5 * MINUTE)
    assert codes(last.check(an_order(1))) == ["ROLLOVER_WINDOW", "WEEKLY_CLOSE"], "the close is a rollover too"


# ---- loss control ---------------------------------------------------------------------------------------------------

DESK_APART = {"desk/loss_latches/USD/daily_loss": "1000.00", "desk/loss_latches/USD/drawdown": "1000.00"}
ONLY_THE_SLEEVES_DAILY = {**DESK_APART, "per_sleeve/control/loss_latches/USD/drawdown": "1000.00"}
SLEEVE_DAILY = "DAILY_LOSS_SLEEVE/control/USD"


def test_a_sleeve_that_has_lost_its_daily_limit_marked_at_the_quote_is_stopped_and_only_that_sleeve(tmp_path):
    desk = Desk(tmp_path, ONLY_THE_SLEEVES_DAILY)
    desk.hold(1)  # long 20,000 at 1.08510
    desk.quote(ASK - 495, ASK - 485)  # the bid is 495 points lower: 99.00 dollars down
    assert desk.gate.latches() == () and desk.check(an_order(2)).allowed
    desk.quote(ASK - 500, ASK - 490)  # 100.00 dollars down: the limit
    assert desk.gate.latches() == (SLEEVE_DAILY,)
    entry = desk.check(an_order(3))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "DAILY_LOSS_SLEEVE")
    assert desk.check(an_order(4, sleeve="ml")).allowed, "the other sleeve lost nothing, and the desk's latch is wider"
    out = desk.check(an_order(5, SELL, 20_000))
    assert (out.outcome, out.allowed) == (Outcome.EXITS_ONLY, True)


def test_a_tripped_latch_stays_tripped_through_a_recovery_a_new_day_and_a_restart(tmp_path):
    desk = Desk(tmp_path, ONLY_THE_SLEEVES_DAILY)
    desk.hold(1)
    desk.quote(ASK - 500, ASK - 490)
    desk.quote(BID + 500, ASK + 500)  # the price comes all the way back, and more
    assert desk.code(an_order(2)) == "DAILY_LOSS_SLEEVE"
    desk.after(24 * HOUR)
    desk.quote(BID + 500, ASK + 500)
    assert desk.code(an_order(3, at_ns=desk.clock.now_ns())) == "DAILY_LOSS_SLEEVE", "a new day does not reset it"
    desk.restart()
    desk.quote(BID + 500, ASK + 500)
    assert desk.gate.latches() == (SLEEVE_DAILY,) and desk.code(an_order(4)) == "DAILY_LOSS_SLEEVE"
    trips = [entry for entry in desk.journal() if entry["type"] == "trip"]
    assert len(trips) == 1 and trips[0]["latch"] == SLEEVE_DAILY and trips[0]["reason"] == "DAILY_LOSS_SLEEVE"
    assert (trips[0]["result"], trips[0]["day_open"], trips[0]["limit"]) == (-100_000_000, 0, 100_000_000)


def test_only_an_explicit_reset_clears_a_latch_and_it_is_journalled_with_who_asked(tmp_path):
    desk = Desk(tmp_path, ONLY_THE_SLEEVES_DAILY)
    desk.hold(1)
    desk.quote(ASK - 500, ASK - 490)
    with pytest.raises(LatchError, match="no latch named"):
        desk.gate.reset("DAILY_LOSS_DESK/USD", operator="owner")
    with pytest.raises(ValueError):
        desk.gate.reset(SLEEVE_DAILY, operator="not a name")
    desk.gate.reset(SLEEVE_DAILY, operator="owner")
    assert desk.gate.latches() == ()
    assert desk.check(an_order(2)).allowed, "it starts again from the result as it stands, so it does not trip at once"
    last = desk.journal()[-1]
    assert (last["type"], last["latch"], last["operator"]) == ("reset", SLEEVE_DAILY, "owner")
    desk.quote(ASK - 995, ASK - 985)  # another 99.00 down since the reset
    assert desk.gate.latches() == ()
    desk.quote(ASK - 1_000, ASK - 990)
    assert desk.gate.latches() == (SLEEVE_DAILY,), "a second hundred dollars trips it again"
    desk.restart()
    assert desk.gate.latches() == (SLEEVE_DAILY,), "the journal ends on a trip"


def test_the_desk_latch_stops_every_sleeve(tmp_path):
    desk = Desk(tmp_path, {"per_sleeve/control/loss_latches/USD/daily_loss": "1000.00"})
    desk.hold(1)
    desk.quote(ASK - 500, ASK - 490)
    assert desk.gate.latches() == ("DAILY_LOSS_DESK/USD",)
    assert desk.code(an_order(2)) == "DAILY_LOSS_DESK" and desk.code(an_order(3, sleeve="ml")) == "DAILY_LOSS_DESK"
    assert desk.code(an_order(4, pair="USD.JPY", sleeve="ml")) == "DAILY_LOSS_DESK", "in every pair"


def test_a_fall_from_the_highest_result_trips_the_drawdown_latch_even_on_a_winning_day(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.quote(ASK + 1_000, ASK + 1_010)  # 200.00 up
    assert desk.gate.latches() == ()
    desk.quote(ASK + 255, ASK + 265)  # 51.00 up: 149.00 below the peak
    assert desk.gate.latches() == ()
    desk.quote(ASK + 250, ASK + 260)  # 150.00 below the peak, and still 50.00 up on the day
    assert desk.gate.latches() == ("DRAWDOWN_DESK/USD", "DRAWDOWN_SLEEVE/control/USD")
    assert codes(desk.check(an_order(2))) == ["DRAWDOWN_DESK", "DRAWDOWN_SLEEVE"]
    desk.gate.reset("DRAWDOWN_DESK/USD", operator="owner")
    desk.gate.reset("DRAWDOWN_SLEEVE/control/USD", operator="owner")
    desk.quote(ASK + 250, ASK + 260)
    assert desk.gate.latches() == () and desk.check(an_order(4)).allowed, "the high-water mark starts again from here"


def test_a_loss_made_before_a_restart_is_not_forgiven_by_it(tmp_path):
    desk = Desk(tmp_path, DESK_APART)
    desk.hold(1)
    desk.quote(ASK - 300, ASK - 290)  # 60.00 down
    desk.restart()
    desk.quote(ASK - 300, ASK - 290)
    assert desk.gate.latches() == ()
    desk.quote(ASK - 495, ASK - 485)
    assert desk.gate.latches() == (), "99.00 down on the day, counted from the day's opening result"
    desk.quote(ASK - 500, ASK - 490)
    assert desk.gate.latches() == (SLEEVE_DAILY,), "not from where the restart found it, which would allow 160.00"


def test_the_high_water_mark_survives_a_restart_too(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.quote(ASK + 1_000, ASK + 1_010)  # the peak, 200.00 up
    desk.after(2 * SECOND)
    desk.quote(ASK + 900, ASK + 910)
    desk.restart()
    desk.quote(ASK + 255, ASK + 265)
    assert desk.gate.latches() == ()
    desk.quote(ASK + 250, ASK + 260)
    assert "DRAWDOWN_DESK/USD" in desk.gate.latches(), "measured from the peak the first process reached"


def test_the_day_a_loss_is_counted_over_starts_at_the_new_york_rollover(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    desk = Desk(tmp_path, ONLY_THE_SLEEVES_DAILY, at_ns=rollover - HOUR)
    desk.hold(1)
    desk.quote(ASK - 400, ASK - 390)  # 80.00 down before the rollover
    desk.after(2 * HOUR)
    desk.quote(ASK - 400, ASK - 390)  # the new day opens 80.00 down
    desk.quote(ASK - 895, ASK - 885)  # 99.00 down on the new day
    assert desk.gate.latches() == ()
    desk.quote(ASK - 900, ASK - 890)
    assert desk.gate.latches() == (SLEEVE_DAILY,)


def test_money_in_different_currencies_is_never_added_a_latch_is_per_currency(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1, pair="USD.JPY")  # long 20,000 dollars at 151.240 yen
    desk.hold(2, SELL)  # and a dollar-quoted position that is 80.00 dollars UP below
    desk.quote(BID - 410, BID - 400)
    desk.quote(151_240 - 745, 151_240 - 735, pair="USD.JPY")  # 14,900 yen down
    assert desk.gate.latches() == (), "80 dollars up and 14,900 yen down are not one number"
    desk.quote(151_240 - 750, 151_240 - 740, pair="USD.JPY")  # 15,000 yen down: the yen limit
    assert desk.gate.latches() == ("DAILY_LOSS_DESK/JPY", "DAILY_LOSS_SLEEVE/control/JPY")
    assert all(entry["currency"] == "JPY" for entry in desk.journal() if entry["type"] == "trip")
    assert desk.code(an_order(3, sleeve="ml")) == "DAILY_LOSS_DESK", "a desk latch in yen stops entries in every pair"


def test_commissions_count_against_the_latch_of_the_currency_they_are_charged_in(tmp_path):
    desk = Desk(tmp_path, DESK_APART)
    desk.hold(1)
    desk.quote(ASK - 495, ASK - 485)  # 99.00 down
    assert desk.gate.latches() == ()
    desk.gate.observe(CommissionEvent("x1", 1_000_000, "USD", T0, 0))  # and a dollar of commission
    assert desk.code(an_order(2)) == "DAILY_LOSS_SLEEVE", "judged at the next verdict, without waiting for a quote"


def test_a_loss_in_a_currency_with_no_latch_stops_entries_by_itself(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.gate.observe(CommissionEvent("x1", 2_000_000, "AUD", T0, 0))  # charged in a currency nobody set a latch for
    entry = desk.check(an_order(2))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "UNLATCHED_CURRENCY")
    assert desk.check(an_order(3, SELL)).allowed and desk.gate.latches() == (), "a stop, not a latch"


def test_a_position_with_no_quote_to_value_it_at_stops_entries_everywhere(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.restart()  # the fills are replayed; no quote has arrived yet
    assert desk.code(an_order(2)) == "NO_QUOTE"
    desk.quote(151_230, 151_240, pair="USD.JPY")
    other = desk.check(an_order(3, pair="USD.JPY"))
    assert (other.outcome, other.allowed, other.code) == (Outcome.EXITS_ONLY, False, "UNMARKED_POSITION")
    desk.quote()
    assert desk.check(an_order(4, pair="USD.JPY")).allowed


def test_a_loss_latch_cannot_start_again_while_the_position_it_measures_has_no_quote(tmp_path):
    desk = Desk(tmp_path, ONLY_THE_SLEEVES_DAILY)
    desk.hold(1)
    desk.quote(ASK - 500, ASK - 490)
    desk.restart()
    with pytest.raises(LatchError, match="has no quote"):
        desk.gate.reset(SLEEVE_DAILY, operator="owner")
    assert desk.gate.latches() == (SLEEVE_DAILY,)
    desk.quote(ASK - 500, ASK - 490)
    desk.gate.reset(SLEEVE_DAILY, operator="owner")
    assert desk.gate.latches() == ()


def test_a_streak_of_venue_rejects_trips_the_breaker_and_an_accepted_order_breaks_the_streak(tmp_path):
    desk = Desk(tmp_path, ROOMY)

    def answered(number: int, state: OrderState) -> None:
        order = an_order(number, limit=108_500)
        assert desk.check(order).allowed
        desk.end(order, state)

    answered(1, OrderState.REJECTED)
    answered(2, OrderState.REJECTED)
    desk.end(an_order(2, limit=108_500), OrderState.REJECTED)  # the same reject, reported twice
    desk.end(an_order(99), OrderState.REJECTED)  # a reject of an order this gate never allowed
    assert desk.gate.latches() == ()
    answered(3, OrderState.WORKING)
    answered(4, OrderState.REJECTED)
    answered(5, OrderState.REJECTED)
    assert desk.gate.latches() == ()
    answered(6, OrderState.REJECTED)
    assert desk.gate.latches() == ("REJECT_STREAK",)
    entry = desk.check(an_order(7))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "REJECT_STREAK")
    desk.restart()
    desk.quote()
    assert desk.code(an_order(8)) == "REJECT_STREAK"
    desk.gate.reset("REJECT_STREAK", operator="owner")
    assert desk.check(an_order(9)).allowed


# ---- books ----------------------------------------------------------------------------------------------------------


def test_while_a_reconciliation_break_is_flagged_entries_stop_and_exits_pass(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.gate.observe(BreakEvent(True, T0))
    entry = desk.check(an_order(2))
    assert (entry.outcome, entry.allowed, entry.code) == (Outcome.EXITS_ONLY, False, "RECONCILIATION_BREAK")
    assert desk.check(an_order(3, SELL)).allowed
    desk.gate.observe(BreakEvent(False, T0))
    assert desk.check(an_order(4)).allowed


def test_a_halt_stops_entries_until_an_operator_resets_it_and_survives_a_restart(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.gate.halt()
    desk.gate.halt()
    assert desk.gate.latches() == ("ORDER_FAULT",) and desk.code(an_order(2)) == "ORDER_FAULT"
    assert desk.check(an_order(3, SELL)).allowed
    desk.restart()
    desk.quote()
    assert desk.code(an_order(4)) == "ORDER_FAULT"
    assert [entry["type"] for entry in desk.journal()].count("trip") == 1
    desk.gate.reset("ORDER_FAULT", operator="owner")
    assert desk.check(an_order(5)).allowed


def test_the_gates_own_book_halts_on_two_fills_under_one_id_and_ignores_a_true_repeat(tmp_path):
    desk = Desk(tmp_path)
    order = desk.hold(1)
    fill = desk.booked[0]
    desk.gate.observe(fill)
    desk.gate.observe(fill)
    assert desk.gate.latches() == () and desk.gate.book.position("control", "EUR.USD").quantity == 20_000
    desk.gate.observe(Fill("x1", order.client_order_id, "EUR.USD", BUY, 30_000, ASK, T0))
    assert desk.gate.latches() == ("ORDER_FAULT",)
    assert desk.gate.book.position("control", "EUR.USD").quantity == 20_000, "and it books neither twice"


def test_the_gate_rebuilds_positions_from_fills_and_nothing_a_sleeve_says(tmp_path):
    desk = Desk(tmp_path)
    assert desk.check(an_order(1)).allowed
    assert desk.gate.book.open_lines() == {}, "an allowed order is not a position"
    desk.gate.observe(OrderEvent(an_order(1).client_order_id, OrderState.FILLED, T0, 0, filled_quantity=20_000))
    assert desk.gate.book.open_lines() == {}, "nor is a report: only a fill is"
    desk.fill(an_order(1), ASK, report=False)
    assert desk.gate.book.open_lines() == {("control", "EUR.USD"): 20_000}


def test_the_journal_is_a_verified_chain_of_codes_and_integers(tmp_path):
    desk = Desk(tmp_path, DESK_APART)
    desk.hold(1)
    desk.quote(ASK - 500, ASK - 490)
    desk.gate.reset("DAILY_LOSS_SLEEVE/control/USD", operator="owner")
    assert verify(tmp_path / "risk.jsonl").entries == len(desk.journal())
    kinds = [entry["type"] for entry in desk.journal()]
    assert kinds[0] == "start" and {"mark", "trip", "reset"} <= set(kinds)
    assert desk.journal()[0]["decision"] == "HFT-DEC-9999"


def test_a_quote_from_before_a_lost_connection_is_stale_and_no_confirmation_can_freshen_it(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.reconnect()
    assert desk.code(an_order(2)) == "STALE_QUOTE"
    desk.gate.quiet("EUR.USD", desk.clock.now_ns())  # the venue answers; it has not shown this connection a book
    assert desk.code(an_order(3)) == "STALE_QUOTE" and desk.code(an_order(4, SELL)) == "STALE_QUOTE"
    assert desk.gate.book.open_lines() and "UNMARKED_POSITION" not in codes(desk.check(an_order(5)))
    desk.quote(BID + 1_000, ASK + 1_000)
    assert desk.check(an_order(6)).allowed
    desk.gate.quiet("EUR.USD", desk.clock.now_ns())
    assert desk.check(an_order(7)).allowed


def test_the_books_own_names_are_not_sleeves(tmp_path):
    from hftlab.core import Order
    from hftlab.core.ids import client_order_id
    from hftlab.risk import RiskConfigError, parse_risk_config
    from tests.risk.kit import raw

    desk = Desk(tmp_path)
    desk.hold(1)
    order_id = client_order_id(sleeve="desk", pair="EUR.USD", side=SELL, decision_seq=2)
    posing = Order(order_id, "desk", "EUR.USD", SELL, 20_000, OrderType.MARKET, TimeInForce.FOK, T0)
    assert desk.code(posing) == "ORDER_ID", "it would be judged on the whole desk's position, as an exit"
    for name in ("desk", "unattributed"):
        with pytest.raises(RiskConfigError, match="keeps for itself"):
            parse_risk_config(raw({f"per_sleeve/{name}": raw()["per_sleeve"]["ml"]}))


# ---- exits have their own collar; a close that was refused waits its interval (ADR 0015, addendum) ------------------


def test_an_order_that_only_reduces_the_position_is_held_to_the_wider_exit_collar_and_still_to_a_collar(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_spread_ticks": 10_000})  # entry collar 10 increments, exit collar 40
    desk.hold(1)
    desk.quote(108_355, 108_655)  # each touch is thirty increments from the mid
    assert desk.code(an_order(2)) == "PRICE_COLLAR", "a buy would add: the entry collar holds"
    assert desk.check(an_order(3, SELL)).allowed, "a sell of what is held is inside the exit collar"
    desk.end(an_order(3, SELL))
    desk.quote(108_300, 108_710)  # forty-one increments: beyond the exit collar too
    assert desk.code(an_order(4, SELL)) == "PRICE_COLLAR", "wider is not absent"
    assert desk.code(an_order(5, SELL, limit=108_300)) == "PRICE_COLLAR", "and it holds for a limit price as well"


def test_the_exit_collar_cannot_be_used_to_open_or_to_go_through_zero(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_spread_ticks": 10_000})
    desk.quote(108_355, 108_655)
    assert desk.code(an_order(1, SELL)) == "PRICE_COLLAR", "flat: a sell opens a short"
    desk.quote()
    desk.hold(2)
    desk.quote(108_355, 108_655)
    assert desk.code(an_order(3, SELL, 40_000)) == "PRICE_COLLAR", "a sell of more than is held ends short"
    first = an_order(4, SELL)
    assert desk.check(first).allowed
    assert desk.code(an_order(5, SELL)) == "PRICE_COLLAR", "with the first exit in flight a second could go through"
    desk.end(first)
    assert desk.check(an_order(6, SELL)).allowed
    assert desk.code(an_order(7, sleeve="ml", side=SELL)) == "PRICE_COLLAR", "another sleeve's position is not its exit"


def test_a_file_whose_exit_collar_is_narrower_than_its_entry_collar_is_refused():
    with pytest.raises(RiskConfigError, match=r"per_pair\.EUR\.USD\.exit_collar_ticks is narrower"):
        limits({"per_pair/EUR.USD/exit_collar_ticks": 9})
    assert limits({"per_pair/EUR.USD/exit_collar_ticks": 10}).pairs["EUR.USD"].exit_collar_ticks == 10


def test_a_flatten_close_that_was_refused_is_proposed_again_only_after_the_configured_interval(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_spread_ticks": 10_000})  # the interval is five seconds
    desk.hold(1)
    desk.switched.add(FLATTEN)
    desk.quote(108_300, 108_710)  # beyond the exit collar
    (close,) = desk.gate.flatten_orders()
    assert desk.code(close) == "PRICE_COLLAR"
    assert desk.gate.flatten_orders() == (), "not again at once: a book that stays bad costs one refusal an interval"
    desk.after(5 * SECOND - 1)
    desk.quote(108_300, 108_710)
    assert desk.gate.flatten_orders() == ()
    desk.after(1)
    (again,) = desk.gate.flatten_orders()
    assert again.client_order_id != close.client_order_id and desk.code(again) == "PRICE_COLLAR"
    desk.after(5 * SECOND)
    desk.quote(108_355, 108_655)  # inside the exit collar
    (third,) = desk.gate.flatten_orders()
    assert desk.check(third).allowed
    desk.fill(third, 108_355)
    assert desk.gate.flatten_orders() == () and desk.gate.book.open_lines() == {}


def test_a_close_that_was_allowed_is_followed_by_the_rest_without_waiting(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_order_size": 20_000})
    desk.hold(1)
    desk.hold(2)
    desk.switched.add(FLATTEN)
    (first,) = desk.gate.flatten_orders()
    assert desk.check(first).allowed
    desk.fill(first, BID)
    (rest,) = desk.gate.flatten_orders()
    assert rest.quantity == 20_000 and desk.check(rest).allowed


# ---- a restart is not a reset (ADR 0015, addendum) ------------------------------------------------------------------


def test_the_running_count_of_rejects_is_in_the_journal_and_a_restart_goes_on_counting(tmp_path):
    desk = Desk(tmp_path, ROOMY)  # the breaker trips at three

    def rejected(number: int) -> None:
        order = an_order(number, limit=108_500)
        assert desk.check(order).allowed
        desk.end(order, OrderState.REJECTED)

    rejected(1)
    rejected(2)
    assert [e["rejects"] for e in desk.journal() if e["type"] == "streak"] == [1, 2]
    desk.restart()
    desk.quote()
    assert desk.gate.latches() == ()
    rejected(3)
    assert desk.gate.latches() == ("REJECT_STREAK",), "two before the restart and one after are three"
    desk.gate.reset("REJECT_STREAK", operator="owner")
    desk.restart()
    desk.quote()
    rejected(4)
    rejected(5)
    assert desk.gate.latches() == (), "the reset is in the journal too: the count started again from nothing"


def test_what_the_outbox_hands_back_fills_the_rate_windows_again_and_they_empty_at_the_same_moments(tmp_path):
    desk = Desk(tmp_path, {**ROOMY, "per_sleeve/control/max_orders_per_minute": 3})
    before = [(desk.clock.now_ns() + n * 10 * SECOND, an_order(n + 1)) for n in (0, 1, 2)]
    desk.after(30 * SECOND)
    desk.restart()
    desk.quote()
    assert desk.check(an_order(9)).allowed, "a gate that is told nothing starts with empty windows"
    desk.restart()
    desk.quote()
    desk.gate.recall(allowed=before, in_flight={}, unanswered=(), cancels=(), trades=())
    assert desk.code(an_order(4)) == "ORDER_RATE_SLEEVE", "three were allowed in the last minute, before the restart"
    assert desk.code(an_order(1)) == "DUPLICATE_ORDER", "and an id is ruled on once, across restarts"
    desk.after(30 * SECOND)
    desk.quote()
    assert desk.check(an_order(5)).allowed, "sixty seconds after the first of them, it no longer counts"
    assert desk.code(an_order(6)) == "ORDER_RATE_SLEEVE"


def test_what_the_outbox_hands_back_counts_towards_the_trade_ratio(tmp_path):
    desk = Desk(tmp_path, {**ROOMY, "desk/max_order_to_trade_ratio": 2})
    now = desk.clock.now_ns()
    first, second = an_order(1, limit=108_500), an_order(2)
    desk.restart()
    desk.quote()
    desk.gate.recall(
        allowed=[(now, first), (now, second)], in_flight={}, unanswered=(),
        cancels=[(now, first.client_order_id)], trades=(),
    )  # fmt: skip
    assert desk.code(an_order(3)) == "ORDER_TO_TRADE_SLEEVE", "two orders and a cancel, and nothing traded"
    desk.gate.recall(allowed=(), in_flight={}, unanswered=(), cancels=(), trades=[(now, "control"), (now, "control")])
    assert desk.check(an_order(4)).allowed, "two orders that traded allow four messages"
    assert desk.code(an_order(5)) == "ORDER_TO_TRADE_SLEEVE"


def test_an_order_in_flight_before_the_restart_holds_its_size_and_its_first_answer_is_still_counted(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_position": 40_000, "desk/reject_streak_breaker": 1})
    now = desk.clock.now_ns()
    resting, part = an_order(1, limit=108_500), an_order(2, quantity=40_000)
    desk.restart()
    desk.quote()
    desk.gate.recall(allowed=[(now, resting)], in_flight={resting.client_order_id: 0}, unanswered=(), cancels=(),
                     trades=())  # fmt: skip
    assert desk.code(an_order(3, quantity=40_000)) == "POSITION_LIMIT", "it may still fill: its size is held"
    desk.end(resting, OrderState.REJECTED)
    assert desk.gate.latches() == (), "the venue had answered it before the restart: no second count"
    assert desk.check(an_order(4, quantity=40_000)).allowed
    desk.end(an_order(4, quantity=40_000))
    desk.gate.recall(allowed=[(now, part)], in_flight={part.client_order_id: 20_000},
                     unanswered={part.client_order_id}, cancels=(), trades=())  # fmt: skip
    assert desk.code(an_order(5, quantity=40_000)) == "POSITION_LIMIT", "half of it is booked, half may still fill"
    desk.end(part, OrderState.REJECTED)
    assert desk.gate.latches() == ("REJECT_STREAK",), "never answered before the restart: its reject counts now"


# ---- the room on the event tape (ADR 0027, addendum) ---------------------------------------------------------------


def test_a_tape_at_its_soft_limit_stops_entries_and_lets_exits_through_and_proposes_no_close(tmp_path):
    desk = Desk(tmp_path)
    held = desk.hold(1)
    desk.gate.observe(TapeRoom(SOFT))
    entry = desk.check(an_order(2))
    assert (entry.outcome, entry.allowed, codes(entry)) == (Outcome.EXITS_ONLY, False, ["TAPE_SOFT_LIMIT"])
    assert desk.gate.flatten_orders() == (), "the soft limit closes nothing: the sleeves leave by their own rules"
    leaving = desk.check(an_order(3, SELL, held.quantity))
    assert (leaving.outcome, leaving.allowed, leaving.code) == (Outcome.EXITS_ONLY, True, "TAPE_SOFT_LIMIT")
    desk.fill(an_order(3, SELL, held.quantity), BID)
    desk.gate.observe(TapeRoom(ROOM))
    assert desk.check(an_order(4)).outcome is Outcome.ALLOW, "room again: nothing of it is kept"


def test_a_tape_at_its_hard_limit_closes_what_the_book_holds_by_the_flatten_orders_with_no_switch_set(tmp_path):
    desk = Desk(tmp_path, {"per_pair/USD.JPY/max_order_size": 20_000})
    desk.hold(1, BUY, 40_000)
    for number in (2, 3):
        desk.hold(number, SELL, 20_000, pair="USD.JPY", sleeve="ml")
    desk.gate.observe(TapeRoom(HARD))
    assert desk.switched == set() and codes(desk.check(an_order(4))) == ["TAPE_HARD_LIMIT"]
    closes = desk.gate.flatten_orders()
    assert [(o.sleeve, o.pair, o.side, o.quantity) for o in closes] == [
        ("control", "EUR.USD", SELL, 40_000), ("ml", "USD.JPY", BUY, 20_000),
    ]  # fmt: skip
    verdicts = [desk.check(order) for order in closes]
    assert all(v.allowed and v.outcome is Outcome.EXITS_ONLY and v.code == "TAPE_HARD_LIMIT" for v in verdicts)
    assert desk.gate.flatten_orders() == (), "a line with an order in flight gets no second close"
    desk.fill(closes[0], BID)
    desk.fill(closes[1], 151_240)
    (rest,) = desk.gate.flatten_orders()
    assert desk.check(rest).allowed
    desk.fill(rest, 151_240)
    assert desk.gate.flatten_orders() == () and desk.gate.book.open_lines() == {}


def test_the_closes_of_the_hard_limit_are_proposed_when_the_switches_cannot_be_read(tmp_path):
    desk = Desk(tmp_path)
    desk.hold(1)
    desk.unreadable = True
    assert desk.gate.flatten_orders() == (), "an unreadable switch stops entries; it is not an order to close"
    desk.gate.observe(TapeRoom(HARD))
    (close,) = desk.gate.flatten_orders()
    verdict = desk.check(close)
    assert verdict.allowed and codes(verdict) == ["SWITCHES_UNREADABLE", "TAPE_HARD_LIMIT"]


def test_a_close_of_the_hard_limit_keeps_the_exit_collar_and_the_retry_interval(tmp_path):
    desk = Desk(tmp_path, {"per_pair/EUR.USD/max_spread_ticks": 10_000})
    desk.hold(1)
    desk.gate.observe(TapeRoom(HARD))
    desk.quote(108_300, 108_710)  # beyond the exit collar
    (close,) = desk.gate.flatten_orders()
    assert desk.code(close) == "PRICE_COLLAR", "a record that is filling does not force an order through a bad book"
    assert desk.gate.flatten_orders() == ()
    desk.after(5 * SECOND)
    desk.quote(108_355, 108_655)  # inside the exit collar, far outside the entry collar
    (again,) = desk.gate.flatten_orders()
    assert desk.check(again).allowed
    desk.fill(again, 108_355)
    desk.gate.observe(TapeRoom(SOFT))
    desk.quote()
    assert desk.gate.flatten_orders() == () and desk.code(an_order(9)) == "TAPE_SOFT_LIMIT"


def test_the_room_on_the_tape_is_one_of_three_levels_and_is_not_kept_across_a_restart(tmp_path):
    for wrong in (3, -1, True, "hard", None):
        with pytest.raises(ValueError, match="the tape's room is one of"):
            TapeRoom(wrong)
    desk = Desk(tmp_path)
    desk.gate.observe(TapeRoom(HARD))
    before = verify(tmp_path / "risk.jsonl").entries
    desk.gate.observe(TapeRoom(SOFT))
    assert verify(tmp_path / "risk.jsonl").entries == before, "it is the tape's to say, at every start: not a latch"
    desk.restart()
    desk.quote()
    assert desk.check(an_order(1)).outcome is Outcome.ALLOW, "the trader shows a new gate the room on its first turn"
