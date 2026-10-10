"""The order state machine: one table of moves, and a result that does not depend on the order reports arrive in."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.core import Fill, OrderEvent, OrderState, Side
from hftlab.exec import IN_FLIGHT, MOVES, TERMINAL, Fault, Stage, Tracked
from tests.venues.kit import ASK, T0, an_order

S = Stage
QUANTITY = 40_000


def tracked(stage: Stage = S.SENT, **more) -> Tracked:
    return Tracked(an_order(quantity=QUANTITY), stage, **more)


def fill(number: int, quantity: int = 20_000, **more) -> Fill:
    order = an_order(quantity=QUANTITY)
    pair, side = more.get("pair", order.pair), more.get("side", order.side)
    return Fill(f"x{number}", order.client_order_id, pair, side, quantity, ASK, T0)


def report(state: OrderState, filled: int = 0) -> OrderEvent:
    return OrderEvent(an_order(quantity=QUANTITY).client_order_id, state, T0, 0, filled_quantity=filled)


# ---- the table ------------------------------------------------------------------------------------------------------


def test_the_table_names_every_stage_and_a_finished_order_goes_nowhere():
    assert set(MOVES) == set(Stage) and {S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED} == TERMINAL
    assert all(MOVES[stage] == frozenset() for stage in TERMINAL)
    assert set(Stage) - TERMINAL - {S.INTENDED} == IN_FLIGHT
    assert all(stage not in MOVES[stage] for stage in Stage), "staying put is not a move"


def test_an_order_is_journalled_before_it_is_sent_and_sent_before_the_venue_can_know_it():
    assert MOVES[S.INTENDED] == {S.JOURNALLED, S.REJECTED}, "nothing is sent that was not written down first"
    assert MOVES[S.JOURNALLED] == {S.SENT, S.REJECTED, S.UNKNOWN}
    for early in (S.INTENDED, S.JOURNALLED):
        assert not MOVES[early] & {S.WORKING, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED}
    assert S.WORKING not in MOVES[S.PARTIALLY_FILLED], "an order does not become unfilled"
    assert S.REJECTED not in MOVES[S.PARTIALLY_FILLED], "nor rejected once part of it has traded"
    assert MOVES[S.UNKNOWN] == set(Stage) - {S.INTENDED, S.JOURNALLED, S.SENT, S.UNKNOWN}
    assert all(S.UNKNOWN in MOVES[stage] for stage in (S.JOURNALLED, S.SENT, S.WORKING, S.PARTIALLY_FILLED))


@pytest.mark.parametrize("before", list(Stage))
@pytest.mark.parametrize("to", list(Stage))
def test_a_move_is_made_only_if_it_is_in_the_table(before, to):
    order = tracked(before)
    assert order.move(to) == (to is before or to in MOVES[before])
    assert order.stage is (to if to in MOVES[before] else before)


# ---- the clean life of an order -------------------------------------------------------------------------------------


def test_an_order_is_acknowledged_partly_filled_and_filled():
    order = tracked()
    assert order.take_event(report(OrderState.SUBMITTED)) == () and order.stage is S.SENT and not order.acked
    assert order.take_event(report(OrderState.WORKING)) == () and order.stage is S.WORKING and order.acked
    assert order.take_fill(fill(1)) == () and order.stage is S.PARTIALLY_FILLED
    assert order.take_event(report(OrderState.PARTIALLY_FILLED, 20_000)) == () and order.owed == 0
    assert order.take_fill(fill(2)) == () and (order.stage, order.booked) == (S.FILLED, QUANTITY)
    assert order.take_event(report(OrderState.FILLED, QUANTITY)) == () and order.stage is S.FILLED
    assert order.fills == {"x1", "x2"}


@pytest.mark.parametrize("ending", [OrderState.CANCELLED, OrderState.EXPIRED])
def test_an_order_can_end_with_part_of_it_filled(ending):
    order = tracked(S.WORKING)
    order.take_fill(fill(1))
    assert order.take_event(report(ending, 20_000)) == ()
    assert (order.stage.value, order.booked, order.reported) == (ending.value, 20_000, 20_000)


def test_a_rejected_order_never_worked():
    order = tracked()
    assert order.take_event(report(OrderState.REJECTED)) == () and order.stage is S.REJECTED and order.acked


# ---- reports in any order, any number of times ----------------------------------------------------------------------


def test_a_fill_before_its_acknowledgement_is_booked_and_the_late_acknowledgement_changes_nothing():
    order = tracked()
    assert order.take_fill(fill(1, QUANTITY)) == () and order.stage is S.FILLED
    assert order.take_event(report(OrderState.WORKING)) == () and order.stage is S.FILLED


def test_a_filled_report_before_its_fills_leaves_them_owed_until_they_are_booked():
    order = tracked()
    assert order.take_event(report(OrderState.FILLED, QUANTITY)) == ()
    assert (order.stage, order.booked, order.owed) == (S.FILLED, 0, QUANTITY)
    assert order.take_fill(fill(1)) == () and order.owed == 20_000
    assert order.take_event(report(OrderState.PARTIALLY_FILLED, 20_000)) == (), "an old report after the last one"
    assert order.take_fill(fill(2)) == () and (order.stage, order.owed) == (S.FILLED, 0)


def test_a_fill_the_cancel_already_counted_may_arrive_after_the_cancel():
    order = tracked(S.WORKING)
    assert order.take_event(report(OrderState.CANCELLED, 20_000)) == () and order.owed == 20_000
    assert order.take_fill(fill(1)) == () and (order.stage, order.owed) == (S.CANCELLED, 0)


def history(kind: str) -> list:
    """The reports of one true life of an order, in the order they happened."""
    if kind == "filled":
        return [report(OrderState.WORKING), fill(1), report(OrderState.PARTIALLY_FILLED, 20_000), fill(2),
                report(OrderState.FILLED, QUANTITY)]  # fmt: skip
    if kind == "part":
        return [report(OrderState.WORKING), fill(1), report(OrderState.PARTIALLY_FILLED, 20_000),
                report(OrderState.CANCELLED, 20_000)]  # fmt: skip
    if kind == "cancelled":
        return [report(OrderState.WORKING), report(OrderState.CANCELLED)]
    if kind == "expired":
        return [report(OrderState.WORKING), fill(1), report(OrderState.EXPIRED, 20_000)]
    return [report(OrderState.REJECTED)]


def after(reports) -> tuple[Tracked, list[Fault]]:
    order, faults = tracked(), []
    for item in reports:
        if isinstance(item, Fill):
            if item.fill_id not in order.fills:  # the manager books a fill once and passes it on once
                faults += order.take_fill(item)
        else:
            faults += order.take_event(item)
    return order, faults


@settings(max_examples=300)
@given(st.sampled_from(["filled", "part", "cancelled", "expired", "rejected"]), st.data())
def test_the_same_history_reordered_and_repeated_ends_in_the_same_place_without_a_fault(kind, data):
    clean = history(kind)
    repeats = data.draw(st.lists(st.sampled_from(clean), max_size=6))
    delivered = data.draw(st.permutations(clean + repeats))
    expected, no_faults = after(clean)
    order, faults = after(delivered)
    assert no_faults == [] and faults == []
    assert (order.stage, order.booked, order.reported, order.fills) == (
        expected.stage, expected.booked, expected.reported, expected.fills,
    )  # fmt: skip
    assert order.owed == 0 and order.acked


# ---- what cannot happen ---------------------------------------------------------------------------------------------


def test_more_filled_than_ordered_is_a_fault_and_is_counted_all_the_same():
    order = tracked()
    order.take_fill(fill(1, QUANTITY))
    assert order.take_fill(fill(2, 10_000)) == (Fault.OVERFILL,) and order.booked == QUANTITY + 10_000
    assert tracked().take_event(report(OrderState.PARTIALLY_FILLED, QUANTITY + 1)) == (Fault.OVERFILL,)


def test_a_fill_in_another_pair_or_on_the_other_side_than_its_order_is_a_fault():
    assert tracked().take_fill(fill(1, pair="USD.JPY")) == (Fault.FILL_MISMATCH,)
    assert tracked().take_fill(fill(1, side=Side.SELL)) == (Fault.FILL_MISMATCH,)


@pytest.mark.parametrize("stage", [S.INTENDED, S.JOURNALLED])
def test_a_report_on_an_order_that_was_never_sent_is_a_fault_and_the_order_is_taken_as_sent(stage):
    order = tracked(stage)
    assert order.take_fill(fill(1)) == (Fault.REPORT_BEFORE_SEND,) and order.stage is S.PARTIALLY_FILLED
    order = tracked(stage)
    assert order.take_event(report(OrderState.WORKING)) == (Fault.REPORT_BEFORE_SEND,) and order.stage is S.WORKING
    assert tracked(stage).take_event(report(OrderState.SUBMITTED)) == (), "the manager's own echo says nothing"


def test_a_fill_on_an_order_the_venue_rejected_is_a_fault():
    order = tracked()
    order.take_event(report(OrderState.REJECTED))
    assert order.take_fill(fill(1)) == (Fault.FILL_ON_REJECTED,) and order.booked == 20_000
    filled_first = tracked()
    filled_first.take_fill(fill(1))
    assert filled_first.take_event(report(OrderState.REJECTED)) == (Fault.FILL_ON_REJECTED,)
    assert filled_first.stage is S.PARTIALLY_FILLED, "it is not moved to rejected with a fill in the book"


def test_a_fill_beyond_what_the_venue_said_was_filled_when_it_closed_the_order_is_a_fault():
    order = tracked(S.WORKING)
    order.take_event(report(OrderState.CANCELLED, 0))
    assert order.take_fill(fill(1)) == (Fault.FILL_AFTER_CLOSE,) and order.booked == 20_000
    booked_more = tracked(S.WORKING)
    booked_more.take_fill(fill(1))
    assert booked_more.take_event(report(OrderState.CANCELLED, 0)) == (Fault.CONTRADICTION,)


@pytest.mark.parametrize(
    ("first", "then"),
    [
        (report(OrderState.FILLED, QUANTITY), report(OrderState.CANCELLED, QUANTITY)),
        (report(OrderState.CANCELLED, 0), report(OrderState.FILLED, QUANTITY)),
        (report(OrderState.CANCELLED, 0), report(OrderState.PARTIALLY_FILLED, 20_000)),
        (report(OrderState.REJECTED), report(OrderState.CANCELLED)),
        (report(OrderState.EXPIRED, 0), report(OrderState.CANCELLED, 0)),
    ],
)
def test_two_reports_that_cannot_both_be_true_are_a_fault_and_the_first_stands(first, then):
    order = tracked()
    assert order.take_event(first) == ()
    stage = order.stage
    assert order.take_event(then) == (Fault.CONTRADICTION,) and order.stage is stage


def test_a_filled_report_for_less_than_the_order_is_a_contradiction():
    assert tracked().take_event(report(OrderState.FILLED, 20_000)) == (Fault.CONTRADICTION,)


def test_an_order_written_off_without_a_word_from_the_venue_comes_back_loudly_if_the_venue_speaks_of_it():
    written_off = tracked(S.EXPIRED)  # settled as never having arrived
    assert written_off.take_event(report(OrderState.WORKING)) == (Fault.CONTRADICTION,)
    assert written_off.stage is S.WORKING and written_off.acked, "the venue's word stands: it is in flight again"
    filled = tracked(S.EXPIRED)
    assert filled.take_fill(fill(1)) == (Fault.FILL_AFTER_CLOSE,) and filled.stage is S.PARTIALLY_FILLED
    refused = tracked(S.REJECTED)  # refused before it was sent, as far as this side knew
    assert refused.take_fill(fill(1, QUANTITY)) == (Fault.FILL_AFTER_CLOSE,) and refused.stage is S.FILLED
    same = tracked(S.CANCELLED)
    assert same.take_event(report(OrderState.CANCELLED)) == (), "the venue agreeing with the conclusion is no fault"
