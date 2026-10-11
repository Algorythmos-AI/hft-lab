"""Labels: priced to the unit as the shadow venue prices, barriers in price increments, and a window that ends."""
from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.calendar import Calendar, Closure
from hftlab.core import Fill, Quote, Side, SimClock, TimeInForce
from hftlab.labels import HINDSIGHT, LOSS, PROFIT, TIME, Barrier, Label, LabelError, Labeller, LabelSpec, PairTape
from hftlab.record.format import Heartbeat
from hftlab.venues.port import CommissionEvent
from hftlab.venues.sim import SimVenue
from tests.venues.kit import EURUSD, MS, SECOND, T0, USDJPY, an_order, plain_costs

SIZE = 20_000
FEE = 2_000_000
ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York, later the same day as T0
MINUTE = 60 * SECOND


def quote(at_ms: int, bid: int = 108_500, ask: int = 108_510, sizes=(10**6, 10**6), start: int = T0) -> Quote:
    return Quote("EUR.USD", bid, ask, *sizes, start + at_ms * MS, at_ms + 1)


def labeller(horizon_ns: int = SECOND, costs=None, calendar: Calendar | None = None, **barriers) -> Labeller:
    return Labeller(LabelSpec(horizon_ns, **barriers), EURUSD, costs or plain_costs(), calendar or Calendar())


def label(stream, n: int, **options) -> Label:
    return labeller(**options).label(PairTape(stream, "EUR.USD"), n)


# ---- the outcome, by hand --------------------------------------------------------------------------------------------


def test_the_outcome_is_the_move_less_the_spread_and_both_commissions_in_millionths():
    stream = [quote(0), quote(500, 108_600, 108_610), quote(2_000, 108_600, 108_610)]
    found = label(stream, 1)
    assert found.buy_micro == (108_600 - 108_510) * SIZE * 10 - 2 * FEE == 14_000_000, "in at the ask, out at the bid"
    assert found.sell_micro == (108_500 - 108_610) * SIZE * 10 - 2 * FEE == -26_000_000
    assert (found.pair, found.n, found.decided_ns) == ("EUR.USD", 1, T0)
    assert found.end_ns == T0 + SECOND + 5 * MS, "the window ends when the exit arrives: the horizon and one latency"


def test_on_a_book_that_does_not_move_either_side_loses_half_the_models_round_trip_figure():
    costs = plain_costs(slippage_increments=2, commission_rate_bps_x100=20)
    stream = [quote(at) for at in range(0, 3_000, 100)]
    found = label(stream, 3, costs=costs)
    doubled = costs.round_trip_cost_micro(EURUSD, SIZE, (108_500, 108_510), (108_500, 108_510))
    assert 2 * found.buy_micro == -doubled
    assert found.sell_micro == found.buy_micro == -(10 + 2 * 10) * SIZE * 10 - 2 * FEE, "spread, slippage, fees"


def test_the_entry_meets_the_quote_in_force_when_it_arrives_not_the_one_the_decision_saw():
    stream = [quote(0), quote(3, 108_520, 108_530), quote(5, 108_540, 108_550), quote(6, 108_900, 108_910),
              quote(1_005, 108_700, 108_710), quote(1_006, 108_000, 108_010), quote(3_000)]  # fmt: skip
    found = label(stream, 1)
    assert found.buy_micro == (108_700 - 108_550) * SIZE * 10 - 2 * FEE, "in at 5 ms, out at 1005 ms"


def test_the_latency_is_the_cost_models_largest():
    costs = plain_costs(latency_min_ns=1 * MS, latency_max_ns=40 * MS)
    stream = [quote(0), quote(20, 108_600, 108_610), quote(60, 108_700, 108_710), quote(5_000)]
    assert labeller(costs=costs).latency_ns == 40 * MS and labeller(costs=costs).window_ns == SECOND + 40 * MS
    assert label(stream, 1, costs=costs).buy_micro == (108_700 - 108_610) * SIZE * 10 - 2 * FEE


def test_a_side_the_venue_would_not_have_filled_has_no_outcome():
    thin = [quote(0, sizes=(10**6, 19_000)), quote(2_000)]
    assert label(thin, 1).buy_micro is None and label(thin, 1).sell_micro is None, "no ask size to buy, or to buy back"
    one_sided = [quote(0, sizes=(19_000, 10**6)), quote(500, sizes=(10**6, 10**6)), quote(2_000)]
    assert label(one_sided, 1).sell_micro is None and label(one_sided, 1).buy_micro == -6_000_000, "each side by itself"
    stale = plain_costs(stale_after_ns=500 * MS)
    gone_quiet = [quote(0), quote(1_800)]
    assert label(gone_quiet, 1, costs=stale).buy_micro is None, "the exit would meet a quote nobody confirmed"
    kept = [quote(0), Heartbeat("EUR.USD", T0 + 600 * MS, T0, 0), quote(1_800)]
    assert label(kept, 1, costs=stale).buy_micro == -6_000_000, "a heartbeat confirms it"
    holiday = Calendar((Closure("holiday", T0 + 500 * MS, T0 + 5 * SECOND),))
    assert label([quote(0)], 1, calendar=holiday).buy_micro is None


def test_near_the_rollover_the_label_widens_and_refuses_exactly_as_the_cost_model_does():
    costs = plain_costs(no_fill_before_ns=MINUTE, no_fill_after_ns=MINUTE, widen_before_ns=10 * MINUTE,
                        widen_after_ns=10 * MINUTE, spread_multiplier_x100=300)  # fmt: skip
    early = [quote(0, start=ROLLOVER - 20 * MINUTE), quote(3_000, start=ROLLOVER - 20 * MINUTE)]
    assert label(early, 1, costs=costs).buy_micro == -6_000_000
    widened = [quote(0, start=ROLLOVER - 5 * MINUTE), quote(3_000, start=ROLLOVER - 5 * MINUTE)]
    assert label(widened, 1, costs=costs).buy_micro == -(10 + 20) * SIZE * 10 - 2 * FEE, "each side moved out by 10"
    inside = [quote(0, start=ROLLOVER - 30 * SECOND), quote(3_000, start=ROLLOVER - 30 * SECOND)]
    assert label(inside, 1, costs=costs).buy_micro is None


# ---- to the unit what the shadow venue does ------------------------------------------------------------------------


def venue_outcome(stream, n: int, side: Side, horizon_ns: int, costs, calendar: Calendar) -> int | None:
    """Send the two orders to a real shadow venue, as a runner would, and add up what it reports."""
    clock = SimClock()
    venue = SimVenue(clock, (EURUSD, USDJPY), costs, calendar=calendar, seed=3)
    venue.connect()
    venue.mark_reconciled()
    reports: list = venue.poll()
    times, now = [], 0
    for event in stream:
        now = max(now, event.recv_ts_ns)
        times.append(now)
    leave_at, left = times[n - 1] + horizon_ns, False

    def leave() -> None:
        clock.advance_to(max(clock.now_ns(), leave_at))
        venue.place(an_order(2, side=side.opposite, quantity=SIZE, tif=TimeInForce.FOK, created_ns=leave_at), EURUSD)

    for number, (event, at) in enumerate(zip(stream, times, strict=True), 1):
        if number > n and not left and at > leave_at:
            leave()
            left = True
        clock.advance_to(at)
        if isinstance(event, Quote):
            venue.feed(dataclasses.replace(event, recv_ts_ns=at))
        else:
            venue.quiet(event.pair, at)
        reports += venue.poll()
        if number == n:
            venue.place(an_order(1, side=side, quantity=SIZE, tif=TimeInForce.FOK, created_ns=at), EURUSD)
    if not left:
        leave()
    clock.advance_to(max(clock.now_ns(), leave_at + costs.latency_max_ns))
    reports += venue.poll()
    fills = [r for r in reports if isinstance(r, Fill)]
    if len(fills) != 2:
        return None
    cash = sum(-fill.side.sign * fill.price * fill.quantity * 10 for fill in fills)
    return cash - sum(r.commission for r in reports if isinstance(r, CommissionEvent))


STEPS = st.lists(
    st.tuples(st.integers(0, 700), st.integers(-4, 4), st.integers(0, 4), st.sampled_from([0, 19_000, 10**6]),
              st.sampled_from([0, 19_000, 10**6]), st.booleans()),
    min_size=1, max_size=40,
)  # fmt: skip


@given(STEPS, st.data(), st.sampled_from([0, 1, 7]), st.sampled_from([0, 3]), st.sampled_from([0, 20, 250]))
def test_the_outcome_is_to_the_unit_what_the_shadow_venue_does(steps, data, latency_ms, slippage, rate):
    costs = plain_costs(latency_min_ns=latency_ms * MS, latency_max_ns=latency_ms * MS, slippage_increments=slippage,
                        commission_rate_bps_x100=rate, stale_after_ns=900 * MS)  # fmt: skip
    stream, at, mid = [], 0, 108_500
    for number, (wait, move, spread, bid_size, ask_size, beat) in enumerate(steps, 1):
        at, mid = at + wait, mid + 5 * move
        if beat and stream:
            stream.append(Heartbeat("EUR.USD", T0 + at * MS, T0, 0))
        else:
            stream.append(Quote("EUR.USD", mid, mid + 5 * (spread - 1), bid_size, ask_size, T0 + at * MS, number))
    n = data.draw(st.integers(1, len(stream)))
    horizon = data.draw(st.sampled_from([1, 300, 1_000])) * MS
    found = labeller(horizon, costs).label(PairTape(stream, "EUR.USD"), n)
    for side, outcome in ((Side.BUY, found.buy_micro), (Side.SELL, found.sell_micro)):
        if not isinstance(stream[n - 1], Quote) and PairTape(stream[:n], "EUR.USD").in_force(T0 + 10**15) is None:
            continue  # no book yet: the venue refuses the order outright, and the label has no outcome
        assert outcome == venue_outcome(stream, n, side, horizon, costs, Calendar()), side


# ---- the three barriers ----------------------------------------------------------------------------------------------


def test_the_barrier_label_says_which_came_first_and_when():
    def path(*bids_at: tuple[int, int]) -> list[Quote]:
        return [quote(0)] + [quote(at, bid, bid + 10) for at, bid in bids_at] + [quote(9_000)]

    spec = {"horizon_ns": 2 * SECOND, "profit_increments": 8, "loss_increments": 6}  # 40 and 30 price units
    up = label(path((400, 108_540), (700, 108_550)), 1, **spec)
    assert up.buy_barrier == Barrier(PROFIT, T0 + 700 * MS), "bought at 108510: worth +40 when the bid reaches 108550"
    assert up.sell_barrier == Barrier(LOSS, T0 + 400 * MS), "sold at 108500: worth -50 when the ask is 108550"
    down_then_up = label(path((300, 108_480), (600, 108_600)), 1, **spec)
    assert down_then_up.buy_barrier == Barrier(LOSS, T0 + 300 * MS), "the first barrier touched decides"
    flat = label(path((500, 108_505)), 1, **spec)
    assert flat.buy_barrier == flat.sell_barrier == Barrier(TIME, T0 + 2 * SECOND)
    late = label(path((2_001, 108_900)), 1, **spec)
    assert late.buy_barrier == Barrier(TIME, T0 + 2 * SECOND), "a move after the time limit is not seen"
    assert up.end_ns == T0 + 2 * SECOND + 5 * MS and up.buy_barrier.end_ns <= up.end_ns


def test_a_spread_wider_than_the_loss_barrier_stops_the_position_on_the_book_it_entered_on():
    wide = [quote(0, 108_500, 108_560), quote(9_000)]
    found = label(wide, 1, horizon_ns=SECOND, profit_increments=8, loss_increments=6)
    assert found.buy_barrier == found.sell_barrier == Barrier(LOSS, T0 + 5 * MS)


def test_a_barrier_label_needs_an_entry_and_no_label_without_barriers_has_one():
    thin = [quote(0, sizes=(10**6, 19_000)), quote(9_000)]
    found = label(thin, 1, horizon_ns=SECOND, profit_increments=8, loss_increments=6)
    assert found.buy_barrier is None and found.sell_barrier == Barrier(TIME, T0 + SECOND)
    assert label([quote(0), quote(9_000)], 1).buy_barrier is None


# ---- what a label is, and is not -------------------------------------------------------------------------------------


def test_a_label_is_hindsight_and_never_a_price_level():
    assert HINDSIGHT is True
    fields = {field.name for field in dataclasses.fields(Label)}
    assert fields == {"pair", "n", "decided_ns", "end_ns", "buy_micro", "sell_micro", "buy_barrier", "sell_barrier"}
    assert not {name for name in fields if "price" in name or "level" in name or "mid" in name}
    plain = {"version": 1, "horizon_ns": SECOND, "profit_increments": 0, "loss_increments": 0}
    assert LabelSpec(SECOND).as_dict() == plain


@given(st.lists(st.tuples(st.integers(1, 400), st.integers(-3, 3)), min_size=2, max_size=30), st.data())
def test_a_label_reads_nothing_before_its_decision_but_the_book_then_in_force(steps, data):
    stream, at, mid = [], 0, 108_500
    for number, (wait, move) in enumerate(steps, 1):
        at, mid = at + wait, mid + 5 * move
        stream.append(Quote("EUR.USD", mid, mid + 10, 10**6, 10**6, T0 + at * MS, number))
    n = data.draw(st.integers(1, len(stream)))
    found = label(stream, n, horizon_ns=300 * MS, profit_increments=3, loss_increments=3)
    rewritten = [dataclasses.replace(q, bid=q.bid + 995, ask=q.ask + 995) for q in stream[: n - 1]] + stream[n - 1 :]
    assert label(rewritten, n, horizon_ns=300 * MS, profit_increments=3, loss_increments=3) == found
    assert found.end_ns == found.decided_ns + 305 * MS > found.decided_ns


@pytest.mark.parametrize("wrong", [{"horizon_ns": 0}, {"horizon_ns": SECOND, "profit_increments": 5},
                                   {"horizon_ns": SECOND, "loss_increments": -1}, {"horizon_ns": 1.5}])  # fmt: skip
def test_a_spec_without_a_horizon_or_with_one_barrier_is_refused(wrong):
    with pytest.raises((ValueError, TypeError)):
        LabelSpec(**wrong)


def test_a_decision_the_stream_does_not_hold_and_a_pair_that_cannot_be_priced_are_refused():
    tape = PairTape([quote(0)], "EUR.USD")
    for n in (0, 2):
        with pytest.raises(LabelError, match="no event"):
            labeller().label(tape, n)
    with pytest.raises(LabelError, match="no event"):
        labeller().label(PairTape([quote(0)], "USD.JPY"), 1)
    with pytest.raises(LabelError, match="cannot price"):
        Labeller(LabelSpec(SECOND), EURUSD, plain_costs(commission_minimum={"JPY": 1}), Calendar())
