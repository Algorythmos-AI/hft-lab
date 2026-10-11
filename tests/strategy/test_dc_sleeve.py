"""The Directional Change sleeve: two plain rules on confirmed events, exact, replayable, and no better than chance."""
from __future__ import annotations

import ast
import copy
import dataclasses
import itertools
import json
from fractions import Fraction
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.book import SleeveView
from hftlab.calendar import Calendar, Closure
from hftlab.core import Fill, OrderEvent, OrderState, Position, Quote, Side, TimeInForce, client_order_id
from hftlab.record.format import Heartbeat, Marker
from hftlab.strategy import Cancel, ControlSleeve, Intent, Placed, Refused
from hftlab.strategy.dc import DOWN, UP, Detector, from_percent
from hftlab.strategy.dc.sleeve import CONTRARIAN, ENTERING, EXITING, FLAT, HOLDING, DcSleeve
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.shadow.kit import session, walk
from tests.strategy.dc_kit import (
    BIG,
    FEE,
    ROUND_TRIP,
    SIZE,
    TENTH_PERCENT,
    UNIT,
    dc,
    decided,
    filled,
    net,
    quotes,
    zigzag,
)
from tests.support import ROOT
from tests.venues.kit import MS, SECOND, T0, plain_costs

MINUTE = 60 * SECOND
TICK = 100 * MS
CUT = 744  # one tick after the sixth event of the seven-leg zig-zag: the stream ends as the last exit is reported
ZIGZAG = zigzag(7)


def at(tick: int) -> int:
    return T0 + tick * TICK


def events_of(mids, warm_up: int = 2) -> list[tuple]:
    """What a detector of its own finds in the mids: (tick, number, direction, warm-up)."""
    detector, found = Detector([TENTH_PERCENT], warm_up=warm_up), []
    state = detector.initial()
    for tick, middle in enumerate(mids):
        state, confirmed = detector.step(state, Fraction(middle), tick)
        found += [(tick, e.number, e.direction, e.warm_up) for e in confirmed]
    return found


def view(name: str = "dc", **quantities: int) -> SleeveView:
    positions = {pair.replace("_", "."): Position(pair.replace("_", "."), q) for pair, q in quantities.items()}
    return SleeveView(name, MappingProxyType(positions), MappingProxyType({}), MappingProxyType({}))


def an_id(number: int = 1) -> str:
    return client_order_id(sleeve="dc", pair="EUR.USD", side=Side.BUY, decision_seq=number)


def leaves(value):
    if isinstance(value, dict | list):
        for inner in value.values() if isinstance(value, dict) else value:
            yield from leaves(inner)
    else:
        yield value


def shown(sleeve: DcSleeve, stream, state=None, held: int = 0, answer=None):
    """Show a sleeve a stream with a book that never changes. The final state and (tick, intent) for each intent.

    `answer(intent)` is what the runner says to each intent, shown to the sleeve at once; by default nothing.
    """
    state, wanted = sleeve.initial() if state is None else state, []
    for tick, event in enumerate(stream):
        state, actions = sleeve.decide(state, event, event.recv_ts_ns, view(EUR_USD=held))
        for intent in actions:
            wanted.append((tick, intent))
            if answer is not None:
                state, _ = sleeve.decide(state, answer(intent), event.recv_ts_ns, view(EUR_USD=held))
    return state, wanted


# ---- the zig-zag, by hand -------------------------------------------------------------------------------------------
#
# The mid runs from 1.08000 up to 1.08600 and back, five price units a tick, with a spread of ten units. The
# threshold is a tenth of a percent. From the high of 108,600 the level is 108,491.4, and the first tick at or
# beyond it is 108,490: 22 ticks after the high. From the low of 108,000 the level is 108,108, first met at
# 108,110, 22 ticks after the low. So the events are at ticks 142, 262, 382, 502, 622 and 742, alternately
# DOWN at 108,490 and UP at 108,110. The first two are warm-up.


def test_the_zig_zag_has_the_events_the_arithmetic_says():
    assert events_of(ZIGZAG) == [
        (142, 1, DOWN, True), (262, 2, UP, True), (382, 3, DOWN, False), (502, 4, UP, False), (622, 5, DOWN, False),
        (742, 6, UP, False),
    ]  # fmt: skip
    assert [ZIGZAG[tick] for tick in (142, 262)] == [108_490, 108_110]


def test_the_trend_variant_on_the_zig_zag_trades_and_earns_exactly_what_can_be_worked_out_by_hand(tmp_path):
    result, entries, _ = session(tmp_path / "j.jsonl", [dc()], quotes(ZIGZAG[:CUT]))
    # Event 3, DOWN at a mid of 108,490: it sells at the bid, 108,485.
    # Event 4, UP at 108,110: it buys back at the ask, 108,115 (370 units made), is told so one tick later, and
    #   on that tick, at a mid of 108,115, buys at the ask, 108,120.
    # Event 5, DOWN at 108,490: it sells at 108,485 (365 made) and one tick later sells again at the bid, 108,480.
    # Event 6, UP at 108,110: it buys back at 108,115 (365 made). One tick later it asks to buy, and the stream ends.
    assert decided(entries) == [
        (at(382), "entry", "SELL", 0), (at(502), "exit", "BUY", 0), (at(503), "entry", "BUY", 0),
        (at(622), "exit", "SELL", 0), (at(623), "entry", "SELL", 0), (at(742), "exit", "BUY", 0),
        (at(743), "entry", "BUY", 0),
    ]  # fmt: skip
    assert filled(entries) == [
        ("SELL", 108_485), ("BUY", 108_115), ("BUY", 108_120), ("SELL", 108_485), ("SELL", 108_480), ("BUY", 108_115),
    ]  # fmt: skip
    counts = result["sleeves"]["dc"]
    assert (counts["decisions"], counts["orders"], counts["fills"], counts["refused"]) == (7, 7, 6, 0)
    pair = counts["pairs"]["EUR.USD"]
    assert pair["quantity"] == 0 and pair["realised"] == (370 + 365 + 365) * SIZE and pair["fees_micro"] == 6 * FEE
    assert net(result) == 1_100 * UNIT - 6 * FEE == 208_000_000, "220.00 USD made, 12.00 USD of commissions"
    # Mid to mid each trip is 380 units. The spread takes 10, and entering a tick late takes 5 more.
    assert 1_100 == 3 * 380 - 3 * 10 - 2 * 5


def test_the_contrarian_variant_on_the_zig_zag_trades_and_loses_exactly_what_can_be_worked_out_by_hand(tmp_path):
    sleeve = dc(variant=CONTRARIAN, overshoot_multiple=Fraction(2))
    result, entries, _ = session(tmp_path / "j.jsonl", [sleeve], quotes(ZIGZAG[:CUT]))
    # After event 3 (DOWN, confirmed at 108,490) two thresholds of overshoot are 216.98 units: first reached at
    #   a mid of 108,270, 44 ticks on. It buys against the fall at the ask, 108,275.
    # Event 4, UP at 108,110: it sells at the bid, 108,105. 170 units lost: it bought into a fall that went on.
    # After event 4 (UP at 108,110) two thresholds are 216.22 units: first reached at 108,330. It sells at 108,325.
    # Event 5, DOWN at 108,490: it buys back at 108,495. 170 lost. Then once more as after event 3.
    assert decided(entries) == [
        (at(426), "entry", "BUY", 0), (at(502), "exit", "SELL", 0), (at(546), "entry", "SELL", 0),
        (at(622), "exit", "BUY", 0), (at(666), "entry", "BUY", 0), (at(742), "exit", "SELL", 0),
    ]  # fmt: skip
    assert filled(entries) == [
        ("BUY", 108_275), ("SELL", 108_105), ("SELL", 108_325), ("BUY", 108_495), ("BUY", 108_275), ("SELL", 108_105),
    ]  # fmt: skip
    assert [ZIGZAG[tick] for tick in (425, 426, 545, 546)] == [108_275, 108_270, 108_325, 108_330]
    assert Fraction(215, 108_490) < 2 * TENTH_PERCENT <= Fraction(220, 108_490), "one tick short, then reached"
    assert net(result) == -3 * 170 * UNIT - 6 * FEE == -114_000_000, "102.00 USD lost, 12.00 USD of commissions"
    assert result["sleeves"]["dc"]["pairs"]["EUR.USD"]["quantity"] == 0


def test_the_same_events_give_the_same_journal_byte_for_byte_and_no_float_reaches_it(tmp_path):
    def journal(name: str, **changes) -> bytes:
        sleeves = [dc("trend", **changes), dc("contra", variant=CONTRARIAN, overshoot_multiple=Fraction(1), **changes)]
        costs = plain_costs(latency_min_ns=20 * MS, latency_max_ns=80 * MS)
        session(tmp_path / name, sleeves, walk(3, 4_000), seed=1, costs=costs)
        return (tmp_path / name).read_bytes()

    small = {"threshold": from_percent("0.0484")}
    base = journal("a.jsonl", **small)
    assert base == journal("b.jsonl", **small) and base.count(b'"type":"fill"') > 40
    assert base != journal("c.jsonl", threshold=from_percent("0.06")), "another threshold: other entries"
    assert not any(type(leaf) is float for line in base.splitlines() for leaf in leaves(json.loads(line)))


# ---- warm-up and gaps -----------------------------------------------------------------------------------------------


def test_it_never_trades_on_a_warm_up_event(tmp_path):
    for warm_up, first in ((0, 142), (1, 262), (2, 382), (3, 502)):
        _, entries, _ = session(tmp_path / f"{warm_up}.jsonl", [dc(warm_up=warm_up)], quotes(ZIGZAG[:CUT]))
        assert decided(entries)[0][0] == at(first), "the first order is on the first event that is not flagged"
    for sleeve in (dc(warm_up=6), dc(warm_up=6, variant=CONTRARIAN, overshoot_multiple=Fraction(1))):
        result, entries, _ = session(tmp_path / f"{sleeve.variant}.jsonl", [sleeve], quotes(ZIGZAG[:CUT]))
        assert decided(entries) == [] and net(result) == 0, "six events, all of them flagged: nothing"


def test_after_a_quiet_time_the_next_events_are_warm_up_again_and_a_position_waits_for_one_that_counts(tmp_path):
    stream = quotes(ZIGZAG[:CUT])
    late = [dataclasses.replace(q, recv_ts_ns=q.recv_ts_ns + 10 * SECOND) for q in stream[441:]]
    _, entries, _ = session(tmp_path / "j.jsonl", [dc()], [*stream[:441], *late])
    # Short since event 3 at tick 382. Then the book is silent for ten seconds, twice the gap time. Events 4 and
    # 5 are the first two after it and are flagged: no exit on 4, no trade on 5. Event 6 counts, and is upwards.
    assert decided(entries) == [
        (at(382), "entry", "SELL", 0),
        (at(742) + 10 * SECOND, "exit", "BUY", 0),
        (at(743) + 10 * SECOND, "entry", "BUY", 0),
    ]
    still = [dataclasses.replace(q, recv_ts_ns=q.recv_ts_ns + 5 * SECOND - TICK) for q in stream[441:]]
    _, entries, _ = session(tmp_path / "k.jsonl", [dc()], [*stream[:441], *still])
    assert [leg for _, leg, _, _ in decided(entries)][:3] == ["entry", "exit", "entry"]
    assert decided(entries)[1][0] == at(502) + 5 * SECOND - TICK, "silent for exactly the gap time: not a gap"


def test_a_heartbeat_keeps_a_quiet_book_from_being_a_gap():
    sleeve, stream = dc(), quotes(ZIGZAG[:CUT])
    pause = [Heartbeat("EUR.USD", at(440) + n * 4 * SECOND, at(440), 0) for n in (1, 2)]
    late = [dataclasses.replace(q, recv_ts_ns=q.recv_ts_ns + 10 * SECOND) for q in stream[441:]]
    _, wanted = shown(sleeve, [*stream[:441], *pause, *late])
    assert wanted[0][0] == 382, "entered, and the book in these runs never fills"
    state, _ = shown(sleeve, [*stream[:441], *pause, *late[:70]])
    assert state[0].live == 4, "ten seconds without a quote, confirmed alive twice: event 4 counts"
    state, _ = shown(sleeve, [*stream[:441], *late[:70]])
    assert state[0].live == 0, "and without the heartbeats it is a gap, and event 4 is flagged"


def test_it_leaves_after_the_maximum_holding_time_and_that_event_is_good_for_no_second_position(tmp_path):
    _, entries, _ = session(tmp_path / "j.jsonl", [dc(max_hold_ns=5 * SECOND)], quotes(ZIGZAG[:CUT]))
    # Sold on tick 382; the shadow venue filled 5 ms later. Five seconds after that is between ticks 432 and 433.
    assert decided(entries)[:3] == [
        (at(382), "entry", "SELL", 0), (at(433), "exit", "BUY", 0), (at(502), "entry", "BUY", 0),
    ], "out on the clock, flat until the next event, and then in on that one"  # fmt: skip


# ---- the reasons not to enter ---------------------------------------------------------------------------------------


def test_it_does_not_enter_when_the_threshold_is_small_against_the_spread():
    # At the mid of event 3, 108,490, the threshold is worth 108.49 price units.
    for spread, ratio, first in (
        (10, Fraction(5), 382),  # 108.49 against 50: in
        (20, Fraction(5), 382),  # against 100: in
        (30, Fraction(5), None),  # against 150: never, anywhere on this stream
        (10, Fraction(10_849, 1_000), 382),  # against 108.49 exactly: the comparison is inclusive
        (10, Fraction(10_849, 1_000) + Fraction(1, 10**9), 579),  # a hair more: not on event 3, nor at event 4
    ):
        _, wanted = shown(dc(min_spread_ratio=ratio), quotes(ZIGZAG[:CUT], spread=spread))
        assert [tick for tick, _ in wanted][:1] == ([] if first is None else [first]), (spread, ratio)
    # The last case enters late: event 4 is still the last event when the rising mid makes the threshold large
    # enough, at 108,495. The ratio is asked of the quote in hand at each entry, not once.
    assert ZIGZAG[579] == 108_495 and ZIGZAG[578] == 108_490
    for ratio in (Fraction(0), Fraction(99, 100), 5, 5.0):
        with pytest.raises(ValueError, match="at least 1"):
            dc(min_spread_ratio=ratio)


def test_it_does_not_enter_on_a_crossed_book():
    stream = quotes(ZIGZAG[:CUT])
    stream[382] = Quote("EUR.USD", 108_495, 108_485, BIG, BIG, at(382), 383)  # the same mid, the sides swapped
    _, wanted = shown(dc(), stream)
    assert wanted[0][0] == 383, "the event is confirmed all the same, and entered a tick later"


ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York
WINDOWS = {"no_fill_before_ns": MINUTE, "no_fill_after_ns": MINUTE, "widen_before_ns": 3 * MINUTE,
           "widen_after_ns": 4 * MINUTE, "spread_multiplier_x100": 300}  # fmt: skip


def test_it_does_not_enter_in_the_widened_or_the_no_fill_window_and_is_never_kept_from_leaving(tmp_path):
    # One tick a second, 200 ticks a leg: events at ticks 222, 422, 622, 822 and 1,022. Event 3 is 190 s before
    # the rollover, event 4 ten seconds after it, event 5 at 210 s after.
    mids, start = zigzag(7, high=109_000), ROLLOVER - 812 * SECOND
    assert [tick for tick, *_ in events_of(mids)][2:5] == [622, 822, 1_022]
    costs = plain_costs(**WINDOWS)
    sleeve = dc(rollover=((MINUTE, MINUTE), (3 * MINUTE, 4 * MINUTE)), retry_ns=10 * SECOND)
    stream = quotes(mids, start_ns=start, interval_ns=SECOND)
    _, entries, _ = session(tmp_path / "j.jsonl", [sleeve], stream, costs=costs)
    found = [(when - ROLLOVER, leg, attempt) for when, leg, _, attempt in decided(entries)]
    assert found[0] == (-190 * SECOND, "entry", 0), "ten seconds before the widened window opens: allowed"
    # Event 4 ends the position inside the no-fill window. It asks to leave at once, and again every ten
    # seconds while the shadow venue fills nothing. The window ends 60 s after the rollover; the order sent in
    # that second reaches the venue 5 ms later, which is after it.
    assert found[1:7] == [((10 + 10 * n) * SECOND, "exit", n) for n in range(6)]
    cancelled = [e for e in entries if e["type"] == "order_event" and e["state"] == "CANCELLED"]
    assert len(cancelled) == 5 and filled(entries)[1][0] == "BUY", "five came to nothing, the sixth got it out"
    assert {e["reason"] for e in cancelled} == {"inside the rollover window or the market is shut"}
    # Flat from then on. Events 4 and 5 are live in turn and neither is entered on inside the widened window,
    # which ends 240 s after the rollover, that second included. Event 5 is entered on the second after.
    assert found[7] == (241 * SECOND, "entry", 0)
    assert not [f for f in found if f[1] == "entry" and -3 * MINUTE <= f[0] <= 4 * MINUTE]
    for offset in range(-200, 260, 7):
        inside = costs.phase(ROLLOVER + offset * SECOND) != "normal"
        assert sleeve._may_enter(stream[0], ROLLOVER + offset * SECOND) is not inside, "the cost model's own windows"


def test_it_does_not_enter_while_the_market_is_shut_and_what_follows_a_closure_is_warm_up(tmp_path):
    stream = quotes(ZIGZAG[:CUT])
    holiday = Calendar((Closure("holiday", at(142), at(200)),))
    always, shut = dc(warm_up=0), dc(warm_up=0, calendar=holiday)
    _, wanted = shown(always, stream)
    assert wanted[0][0] == 142, "with no flagged events and an open market it enters on the first event"
    state, wanted = shown(shut, stream[:200])
    assert wanted == [] and state[0].live == 0, "the same event falls on the first shut tick: nothing"
    assert state[0].detector.machines[0].events == 0, "a shut market is a gap at every tick: no event confirms in it"

    weekend = Calendar((Closure("holiday", at(450), at(500)),))
    _, entries, _ = session(tmp_path / "j.jsonl", [dc(calendar=weekend)], stream, calendar=weekend)
    # Short since tick 382. Shut from tick 450 to 499. The machine starts again at tick 500, at a mid of 108,100,
    # and its next two events are flagged; the third, at tick 742, counts.
    assert decided(entries) == [
        (at(382), "entry", "SELL", 0), (at(742), "exit", "BUY", 0), (at(743), "entry", "BUY", 0),
    ]  # fmt: skip
    plain = shown(dc(), stream)[0][0]
    crossed = shown(dc(calendar=Calendar((Closure("holiday", at(300) + 1, at(301) - 1),))), stream[:383])
    assert plain.live == 6 and crossed[1] == [], "shut for a moment between two ticks: a gap, though no tick saw it"
    assert crossed[0][0].detector.machines[0].warm_left == 1, "event 3 was the first after it, and was flagged"


def test_the_weekend_is_a_gap_whatever_the_gap_time_says():
    close = 1_760_130_000 * SECOND  # Friday 17:00 in New York, 2025-10-10
    sunday = close + (48 * 60 + 15) * MINUTE  # Sunday 17:15
    week, after = quotes(ZIGZAG[:300], start_ns=close - MINUTE), quotes(ZIGZAG[300:CUT], start_ns=sunday)
    sleeve = dc(gap_ns=10**18)
    state, _ = shown(sleeve, week)
    assert state[0].detector.machines[0].events == 2 and state[0].open_until_ns == close
    state, wanted = shown(sleeve, after[:83], state)
    machine = state[0].detector.machines[0]
    assert (machine.events, machine.warm_left) == (3, 1) and wanted == [], "event 3 is the first of a new week"


# ---- orders that come to nothing ------------------------------------------------------------------------------------


def test_an_event_gets_a_bounded_number_of_entry_orders_each_a_wait_after_the_one_before():
    stream = quotes(ZIGZAG)  # the whole of it: 840 ticks, so that the sixth event has time for its orders too
    for attempts in (1, 2, 3):
        sleeve = dc(max_attempts=attempts, retry_ns=2 * SECOND)
        _, wanted = shown(sleeve, stream, answer=lambda intent: Refused(intent, "no"))
        by_event = [[(tick, i.attempt) for tick, i in wanted if first <= tick < first + 120] for first in (382, 502)]
        for first, sent in zip((382, 502), by_event, strict=True):
            assert sent == [(first + 20 * n, n) for n in range(attempts)], "two seconds apart, and then no more"
        assert len(wanted) == 4 * attempts, "four events that count, and nothing is ever filled"
        assert {i.leg for _, i in wanted} == {"entry"}
    with pytest.raises(ValueError, match="at least one entry order"):
        dc(max_attempts=0)


def test_an_exit_that_comes_to_nothing_is_sent_again_once_a_wait_until_the_book_is_flat():
    sleeve = dc(retry_ns=30 * SECOND)
    held = dataclasses.replace(sleeve.initial()[0], phase=HOLDING, keyed_up=True, leave=True, since_ns=T0)
    stream = [Quote("EUR.USD", 108_500, 108_510, BIG, BIG + n % 2, T0 + n * TICK, n + 1) for n in range(6_000)]
    _, wanted = shown(sleeve, stream, (held,), held=SIZE, answer=lambda intent: Refused(intent, "no"))
    assert [(tick, i.leg, i.attempt) for tick, i in wanted] == [(300 * n, "exit", n) for n in range(20)], (
        "ten minutes of quotes and nothing taken: 600 s / 30 s orders, not 6,000"
    )
    assert {(i.side, i.quantity) for _, i in wanted} == {(Side.SELL, SIZE)}


def test_through_the_shadow_runner_the_orders_of_a_market_that_fills_nothing_stay_bounded(tmp_path):
    costs = plain_costs(stale_after_ns=0)  # every quote is too old by the time an order arrives: nothing fills
    sleeve = dc(max_attempts=2, retry_ns=2 * SECOND)
    result, entries, _ = session(tmp_path / "j.jsonl", [sleeve], quotes(ZIGZAG), costs=costs)
    counts = result["sleeves"]["dc"]
    assert (counts["orders"], counts["fills"]) == (8, 0), "four events that count, two orders each, 841 quotes"
    assert [(leg, attempt) for _, leg, _, attempt in decided(entries)] == [("entry", 0), ("entry", 1)] * 4


# ---- one round trip, step by step -----------------------------------------------------------------------------------


def test_a_round_trip_step_by_step():
    sleeve, stream = dc(), quotes(ZIGZAG[:CUT])
    state, wanted = shown(sleeve, stream[:383])
    ((tick, entry),) = wanted
    assert tick == 382 and (entry.pair, entry.side, entry.quantity, entry.leg) == ("EUR.USD", Side.SELL, SIZE, "entry")
    assert entry.tif is TimeInForce.FOK and entry.limit_price is None, "a market order, all or nothing"
    mine = state[0]
    assert (mine.phase, mine.tried_ns, mine.tried, mine.attempts, mine.keyed_up) == (ENTERING, at(382), 3, 1, False)

    state, actions = sleeve.decide(state, Placed(entry, an_id()), at(382), view())
    assert state[0].order_id == an_id() and not actions
    state, actions = sleeve.decide(state, stream[383], at(383), view())
    assert not actions, "one entry at a time"
    short = view(EUR_USD=-SIZE)
    fill = Fill("S.1", an_id(), "EUR.USD", Side.SELL, SIZE, 108_485, at(383), venue_ts_ns=at(382) + 5 * MS)
    state, actions = sleeve.decide(state, fill, at(383), short)
    mine = state[0]
    assert (mine.phase, mine.since_ns, mine.order_id, mine.entered) == (HOLDING, at(382) + 5 * MS, None, 3)
    assert mine.tried_ns is None and not actions, "held from the venue's time of the fill, and nothing to wait for"

    for number in range(383, 502):
        state, actions = sleeve.decide(state, stream[number], at(number), short)
        assert not actions and not state[0].leave
    state, (leave,) = sleeve.decide(state, stream[502], at(502), short)
    assert (leave.side, leave.quantity, leave.leg, leave.attempt) == (Side.BUY, SIZE, "exit", 0)
    assert (state[0].phase, state[0].live, state[0].live_up) == (EXITING, 4, True)
    state, _ = sleeve.decide(state, Placed(leave, an_id(2)), at(502), short)
    out = Fill("S.2", an_id(2), "EUR.USD", Side.BUY, SIZE, 108_115, at(503), venue_ts_ns=at(502) + 5 * MS)
    state, actions = sleeve.decide(state, out, at(503), view())
    mine = state[0]
    assert (mine.phase, mine.trips, mine.leave, mine.exits, mine.tried_ns) == (FLAT, 1, False, 0, None) and not actions
    state, (again,) = sleeve.decide(state, stream[503], at(503), view())
    assert (again.side, again.leg) == (Side.BUY, "entry") and state[0].keyed_up, "event 4 is its next entry"


def test_the_exit_is_for_what_the_book_says_it_holds_and_a_heartbeat_can_leave_but_never_enter():
    sleeve = dc()
    due = dataclasses.replace(sleeve.initial()[0], phase=HOLDING, leave=True, since_ns=T0)
    beat = Heartbeat("EUR.USD", T0 + SECOND, T0 + SECOND, 0)
    _, (leave,) = sleeve.decide((due,), beat, beat.recv_ts_ns, view(EUR_USD=-37_000))
    assert (leave.side, leave.quantity, leave.leg) == (Side.BUY, 37_000, "exit")
    state, _ = shown(sleeve, quotes(ZIGZAG[:382]))
    live = dataclasses.replace(state[0], live=2, live_up=True)  # as if event 2 had counted and were still to enter on
    late = Heartbeat("EUR.USD", at(382), at(382), 0)
    assert sleeve.decide((live,), late, at(382), view())[1] == (), "a heartbeat is no price to enter on"
    _, (entry,) = sleeve.decide((live,), quotes([ZIGZAG[381]], start_ns=at(382))[0], at(382), view())
    assert (entry.side, entry.leg) == (Side.BUY, "entry"), "a quote is, even one that moves nothing"


@pytest.mark.parametrize("how", ["cancelled", "rejected", "refused"])
def test_an_entry_that_comes_to_nothing_leaves_the_pair_flat_and_waits(how):
    sleeve, stream = dc(), quotes(ZIGZAG[:CUT])
    state, ((_, entry),) = shown(sleeve, stream[:383])
    if how == "refused":
        state, actions = sleeve.decide(state, Refused(entry, "no"), at(382), view())
    else:
        state, _ = sleeve.decide(state, Placed(entry, an_id()), at(382), view())
        stranger = OrderEvent(an_id(99), OrderState.CANCELLED, at(382), 2)
        assert sleeve.decide(state, stranger, at(382), view()) == (state, ()), "another order's end is not this one's"
        dead = OrderState.CANCELLED if how == "cancelled" else OrderState.REJECTED
        state, actions = sleeve.decide(state, OrderEvent(an_id(), dead, at(383), 3), at(383), view())
    assert (state[0].phase, state[0].order_id, state[0].entered) == (FLAT, None, 0) and not actions
    for number in range(383, 392):
        state, actions = sleeve.decide(state, stream[number], at(number), view())
        assert not actions, "not again at once"
    state, (again,) = sleeve.decide(state, stream[392], at(392), view())
    assert (again.attempt, again.side, state[0].attempts) == (1, Side.SELL, 2), "a second for the same event, a wait on"


def test_pairs_are_independent_and_what_is_not_its_own_changes_nothing():
    sleeve = dc(sizes={"EUR.USD": SIZE, "USD.JPY": 30_000})
    both = [q for pair in zip(quotes(ZIGZAG[:383]), quotes(ZIGZAG[:383], pair="USD.JPY"), strict=True) for q in pair]
    state, wanted = shown(sleeve, both)
    assert [(i.pair, i.quantity) for _, i in wanted] == [("EUR.USD", SIZE), ("USD.JPY", 30_000)]
    assert [mine.phase for mine in state] == [ENTERING, ENTERING]
    stray = Fill("X1", an_id(), "GBP.USD", Side.BUY, 1, 1, T0)
    others = (
        quotes([108_000], pair="GBP.USD")[0], stray, Refused(Cancel(an_id()), "no"), Marker(True, T0, 1),
        ConnectionEvent(ConnectionState.READY, T0, 1), Refused(Intent("GBP.USD", Side.BUY, 1), "no"),
        Placed(Intent("GBP.USD", Side.BUY, 1), an_id(5)),
    )  # fmt: skip
    for other in others:
        assert sleeve.decide(state, other, at(383), view()) == (state, ())


# ---- settings -------------------------------------------------------------------------------------------------------


def test_settings_that_make_no_sense_are_refused_and_no_number_has_a_silent_default():
    bad_settings = (
        {"sizes": {}}, {"sizes": {"EUR.USD": 0}}, {"variant": "martingale"}, {"overshoot_multiple": Fraction(1)},
        {"variant": CONTRARIAN}, {"variant": CONTRARIAN, "overshoot_multiple": Fraction(0)},
        {"variant": CONTRARIAN, "overshoot_multiple": 1.5}, {"max_hold_ns": -1}, {"gap_ns": 0}, {"retry_ns": -1},
        {"threshold": 0.001}, {"threshold": Fraction(0)}, {"warm_up": -1}, {"rollover": ((1, -1),)}, {"price": "last"},
        {"rollover": ((1, 2, 3),)}, {"max_attempts": 1.0},
    )  # fmt: skip
    for bad in bad_settings:
        with pytest.raises(ValueError):
            dc(**bad)
    with pytest.raises(ValueError, match="sleeve name"):
        dc("Not A Name")
    needed = {"sizes", "variant", "threshold", "overshoot_multiple", "max_hold_ns", "min_spread_ratio", "warm_up",
              "gap_ns", "retry_ns", "max_attempts", "calendar", "rollover"}  # fmt: skip
    for missing in needed:
        with pytest.raises(TypeError, match=missing):
            DcSleeve("dc", **{key: value for key, value in vars_of(dc()).items() if key != missing})


def vars_of(sleeve: DcSleeve) -> dict:
    """The arguments a sleeve was built with."""
    return {
        "sizes": sleeve.sizes, "variant": sleeve.variant, "threshold": sleeve.threshold,
        "overshoot_multiple": sleeve.overshoot_multiple, "max_hold_ns": sleeve.max_hold_ns,
        "min_spread_ratio": sleeve.min_spread_ratio, "warm_up": 2, "gap_ns": sleeve.gap_ns,
        "retry_ns": sleeve.retry_ns, "max_attempts": sleeve.max_attempts, "calendar": Calendar(),
        "rollover": sleeve.rollover,
    }  # fmt: skip


def test_the_settings_it_logs_name_the_price_series_and_are_whole_numbers_and_text():
    said = dc(variant=CONTRARIAN, overshoot_multiple=Fraction(3, 2), rollover=((1, 2),)).settings()
    assert said == {
        "price": "mid", "thresholds": [[1, 1000]], "warm_up": 2, "start": "up", "variant": "contrarian",
        "overshoot_multiple": [3, 2], "max_hold_ns": 3_600 * SECOND, "min_spread_ratio": [5, 1],
        "gap_ns": 5 * SECOND, "retry_ns": SECOND, "max_attempts": 3, "rollover_ns": [[1, 2]],
        "sizes": {"EUR.USD": SIZE},
    }  # fmt: skip
    assert dc().settings()["overshoot_multiple"] is None


# ---- pure, and blind to what has not happened yet -------------------------------------------------------------------

MIDS = st.lists(st.integers(-8, 8), min_size=1, max_size=60)
HELD = st.sampled_from([-SIZE, 0, 0, SIZE])


@given(moves=MIDS, held=st.lists(HELD, min_size=60, max_size=60), variant=st.sampled_from(["trend", "contrarian"]))
def test_a_decision_is_a_function_of_its_arguments_and_leaves_the_sleeve_as_it_was(moves, held, variant):
    multiple = Fraction(1) if variant == CONTRARIAN else None
    sleeve = dc(variant=variant, overshoot_multiple=multiple, warm_up=0, retry_ns=0)
    before = (sorted(vars(sleeve)), copy.deepcopy(sleeve.settings()))
    mids = list(itertools.accumulate(moves, lambda mid, move: mid + 25 * move, initial=108_000))
    stream, state, states = quotes(mids), sleeve.initial(), []
    for quote, holding in zip(stream, held, strict=False):
        arguments = (state, quote, quote.recv_ts_ns, view(EUR_USD=holding))
        first, second = sleeve.decide(*arguments), sleeve.decide(*arguments)
        assert first == second, "asked twice, it answers the same"
        assert arguments[0] == state and hash(state) == hash(arguments[0]), "the state it was given is a value"
        state = first[0]
        states.append(first)
    assert (sorted(vars(sleeve)), sleeve.settings()) == before, "it holds settings only: deciding does not change it"
    again, replayed = dc(variant=variant, overshoot_multiple=multiple, warm_up=0, retry_ns=0), []
    state = again.initial()
    for quote, holding in zip(stream[: len(stream) // 2], held, strict=False):
        found = again.decide(state, quote, quote.recv_ts_ns, view(EUR_USD=holding))
        state = found[0]
        replayed.append(found)
    assert replayed == states[: len(replayed)], "what it decided on the first half does not depend on the second"


def test_the_sleeve_reads_confirmed_events_and_the_provisional_overshoot_and_never_a_completed_one():
    tree = ast.parse((ROOT / "src/hftlab/strategy/dc/sleeve.py").read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "Completed" not in names and not attributes & {"completes", "total_move", "extreme_price", "extreme_ns"}
    assert {"provisional", "warm_up", "overshoot", "number", "direction"} <= attributes
    imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names]
    assert "Completed" not in imported and "DcEvent" not in imported


# ---- where there is nothing to earn ---------------------------------------------------------------------------------

SEEDS = range(30)
SMALL = from_percent("0.0484")  # 52.5 price units at these prices: a little over five spreads, eleven steps of the walk


def test_on_a_driftless_walk_each_variant_pays_the_cost_of_its_round_trips_as_the_control_sleeve_does(tmp_path):
    """Where there is nothing to earn it must not look as if there were.

    On a walk with no drift a round trip earns nothing before costs, on average, whoever decides it. So each
    sleeve's result, plus the modelled cost of the round trips it made, is noise around zero. The test is
    stated per sleeve over 30 seeded walks: the mean of that excess is within three standard errors of zero
    (the errors measured across the 30 walks themselves), each sleeve's total is a loss, and no sleeve's loss
    per round trip is distinguishable from the control sleeve's by the same measure.
    """
    names = ("trend", "contra", "control")
    excess: dict[str, list[int]] = {name: [] for name in names}
    trips: dict[str, int] = dict.fromkeys(names, 0)
    total: dict[str, int] = dict.fromkeys(names, 0)
    for seed in SEEDS:
        sleeves = [
            dc("trend", threshold=SMALL),
            dc("contra", threshold=SMALL, variant=CONTRARIAN, overshoot_multiple=Fraction(1)),
            ControlSleeve("control", seed=seed, sizes={"EUR.USD": SIZE}, entry_per_million=5_000,
                          hold_ns=10 * SECOND, retry_ns=SECOND),
        ]  # fmt: skip
        result, _, _ = session(tmp_path / f"{seed}.jsonl", sleeves, walk(1_000 + seed, 6_000), seed=seed)
        for name in names:
            done, half = divmod(result["sleeves"][name]["fills"], 2)
            # As in the gate of F3: a position still open has paid one commission and stands a spread from its entry.
            excess[name].append(net(result, name) + done * ROUND_TRIP + half * (ROUND_TRIP - FEE))
            trips[name] += done
            total[name] += net(result, name)
    assert trips["trend"] > 800 and trips["contra"] > 300 and trips["control"] > 400, "enough round trips to mean it"
    for name in names:
        count, whole, squares = len(SEEDS), sum(excess[name]), sum(e * e for e in excess[name])
        # mean^2 <= 9 * variance / count, with the variance of the 30 values, in whole numbers.
        assert whole * whole * (count - 1) <= 9 * (count * squares - whole * whole), f"{name}: more than noise"
        assert total[name] < 0, f"{name} ends on a loss"
        assert 2 * abs(whole) < trips[name] * ROUND_TRIP, f"{name}: and the noise is well below the cost itself"

    def squared_error(who: str) -> Fraction:
        """The squared standard error of a sleeve's mean excess per round trip, from the spread across the walks."""
        count, whole, squares = len(SEEDS), sum(excess[who]), sum(e * e for e in excess[who])
        return Fraction(count * squares - whole * whole, (count - 1) * trips[who] ** 2)

    for name in ("trend", "contra"):
        # Per round trip, against the control sleeve: the difference of the two means against its standard error.
        ours, theirs = Fraction(sum(excess[name]), trips[name]), Fraction(sum(excess["control"]), trips["control"])
        assert (ours - theirs) ** 2 <= 9 * (squared_error(name) + squared_error("control")), f"{name} against control"
