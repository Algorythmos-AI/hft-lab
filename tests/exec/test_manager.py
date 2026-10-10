"""The order manager: one way to the venue, through the gate and the outbox, and one booking of every fill."""
from __future__ import annotations

import pytest

from hftlab.book import DESK
from hftlab.core import Fill, InstrumentKind, OrderEvent, OrderState, Side, TimeInForce
from hftlab.exec import Cause, Fault, Held, OrderManager, Outbox, OutboxError, Proposal, Stage
from hftlab.exec.outbox import order_fields
from hftlab.journal import JournalError, verify
from hftlab.safety import PaperOnlyError
from hftlab.venues.fake import FakeConfig
from hftlab.venues.port import ConnectionState, OpenOrder, VenueUnavailable
from tests.exec.kit import ASK, BID, MS, SECOND, T0, TRADED, Floor
from tests.risk.kit import KILL

BUY, SELL = Side.BUY, Side.SELL
READY, RECONCILING = ConnectionState.READY, ConnectionState.RECONCILING


def outage(start_ms: int, end_ms: int, **more) -> FakeConfig:
    return FakeConfig(outages=((T0 + start_ms * MS, T0 + end_ms * MS),), **more)


# ---- the one way out ------------------------------------------------------------------------------------------------


def test_an_allowed_order_is_journalled_sent_and_followed_to_its_fill(tmp_path):
    floor = Floor(tmp_path)
    order = floor.order(1)
    assert floor.oms.propose(order) == Proposal(order.client_order_id, True, "ALLOW", Stage.SENT)
    assert floor.at_broker() == [order.client_order_id]
    floor.pump(50 * MS)
    assert floor.stage(order) == "FILLED" and floor.held() == (20_000, 20_000, 20_000)
    assert floor.oms.book.fees() == {"USD": 2_000_000} and floor.oms.faults == () and floor.oms.in_flight() == ()
    kept = ("intent", "stage", "fill")
    told = [(e["type"], e.get("to"), e.get("cause")) for e in floor.outbox() if e["type"] in kept]
    assert told == [
        ("intent", None, None),
        ("stage", "SENT", "PLACED"),
        ("stage", "WORKING", "REPORT"),
        ("fill", None, None),
        ("stage", "FILLED", "FILL"),
    ]
    assert floor.kinds()[:2] == ["start", "reconciled"] and floor.kinds()[-1] == "commission"


def test_the_intent_is_on_the_disk_before_the_venue_is_handed_the_order(tmp_path, monkeypatch):
    floor = Floor(tmp_path, fsync=True)
    order = floor.order(1)
    seen: list[str] = []
    real_fsync, real_submit = __import__("os").fsync, floor.market.submit

    def fsync(descriptor: int) -> None:
        seen.append("fsync")
        real_fsync(descriptor)

    def submit(*arguments):
        on_disk = (tmp_path / "outbox.jsonl").read_text()
        assert f'"client_order_id":"{order.client_order_id}"' in on_disk and '"type":"intent"' in on_disk
        seen.append("sent")
        return real_submit(*arguments)

    monkeypatch.setattr("hftlab.journal.os.fsync", fsync)
    monkeypatch.setattr(floor.market, "submit", submit)
    assert floor.oms.propose(order).sent
    assert seen[:2] == ["fsync", "sent"], "written, synced, and only then sent"
    intent = next(e for e in floor.outbox() if e["type"] == "intent")
    assert (intent["code"], intent["outcome"], intent["quantity"], intent["side"]) == ("ALLOW", "ALLOW", 20_000, "BUY")


def test_the_same_order_proposed_again_is_the_same_order_and_is_not_sent_twice(tmp_path):
    floor = Floor(tmp_path)
    order = floor.order(1)
    assert floor.oms.propose(order).sent
    again = floor.oms.propose(order)
    assert again == Proposal(order.client_order_id, False, "ALREADY_KNOWN", Stage.SENT)
    floor.pump(50 * MS)
    assert floor.oms.propose(order).stage is Stage.FILLED and floor.at_broker() == [order.client_order_id]
    assert floor.kinds().count("intent") == 1 and floor.held() == (20_000, 20_000, 20_000)


def test_another_order_under_a_used_id_is_refused_and_is_a_fault(tmp_path):
    floor = Floor(tmp_path)
    order = floor.order(1)
    floor.oms.propose(order)
    twice_the_size = type(order)(
        order.client_order_id, order.sleeve, order.pair, order.side, 40_000, order.order_type, order.tif, T0
    )
    assert floor.oms.propose(twice_the_size).code == Held.ID_REUSED.value
    assert floor.oms.faults == ((Fault.ID_REUSED, order.client_order_id),) and len(floor.at_broker()) == 1
    assert floor.gate.latches() == ("ORDER_FAULT",)


def test_an_order_the_gate_refuses_is_recorded_with_the_gates_code_and_goes_nowhere(tmp_path):
    floor = Floor(tmp_path)
    floor.switched.add(KILL)
    order = floor.order(1)
    assert floor.oms.propose(order) == Proposal(order.client_order_id, False, "KILL_SWITCH", None)
    assert floor.at_broker() == [] and floor.oms.tracked(order.client_order_id) is None
    floor.pump()
    refused = floor.outbox()[-1]
    assert (refused["type"], refused["code"]) == ("refused", "KILL_SWITCH")
    assert refused["client_order_id"] == order.client_order_id
    floor.switched.clear()
    assert floor.oms.propose(order).sent, "a refusal is not an order: the same decision may be proposed again"


def test_there_is_no_stand_in_for_the_gate(tmp_path):
    floor = Floor(tmp_path)

    class Yes:
        def check(self, order):
            return type("Verdict", (), {"allowed": True, "code": "ALLOW"})()

    with pytest.raises(TypeError, match="the risk gate itself"):
        OrderManager(floor.broker, Yes(), Outbox(tmp_path / "x.jsonl"), clock=floor.clock, instruments=TRADED)


def test_nothing_is_proposed_to_a_venue_until_its_connection_has_been_reconciled(tmp_path):
    floor = Floor(tmp_path, up=False)
    order = floor.order(1)
    assert floor.oms.blind and floor.oms.propose(order).code == "NOT_RECONCILED"
    floor.connect()
    floor.pump()
    assert floor.broker.state is RECONCILING and floor.oms.propose(order).code == "NOT_RECONCILED"
    assert floor.oms.reconcile() and floor.broker.state is READY and not floor.oms.blind
    assert floor.oms.propose(order).code == "VENUE_NOT_READY", "the gate has not been shown the connection yet"
    floor.pump()
    assert floor.oms.propose(order).sent and floor.at_broker() == [order.client_order_id]


def test_a_manager_built_on_a_venue_someone_else_marked_ready_is_still_blind_until_it_has_looked(tmp_path):
    floor = Floor(tmp_path, up=False)
    floor.connect()
    floor.broker.mark_reconciled()
    floor.pump()
    assert floor.broker.state is READY and floor.oms.propose(floor.order(1)).code == "NOT_RECONCILED"
    assert floor.oms.reconcile() and floor.oms.propose(floor.order(1)).sent


def test_an_order_in_a_pair_the_manager_was_given_no_instrument_for_goes_nowhere(tmp_path):
    floor = Floor(tmp_path)
    assert floor.oms.propose(floor.order(1, pair="GBP.USD")).code == "UNKNOWN_INSTRUMENT" and floor.at_broker() == []


# ---- what the venue does with it ------------------------------------------------------------------------------------


def test_an_order_the_venue_refuses_before_sending_is_rejected_here_and_the_gate_lets_go_of_its_size(tmp_path):
    floor = Floor(tmp_path, changes={"per_pair/EUR.USD/max_position": 20_500})
    odd = floor.order(1, quantity=20_500)  # not on the venue's size grid
    assert floor.oms.propose(odd) == Proposal(odd.client_order_id, False, "REFUSED_BY_VENUE", Stage.REJECTED)
    assert floor.at_broker() == [] and floor.oms.in_flight() == ()
    assert floor.oms.propose(floor.order(2)).sent, "the refused order no longer counts against the position limit"
    assert floor.oms.propose(floor.order(3)).code == "POSITION_LIMIT", "the one that was sent does"


def test_orders_the_venue_refuses_before_sending_count_towards_the_reject_streak(tmp_path):
    floor = Floor(tmp_path, changes={"desk/reject_streak_breaker": 2})
    floor.oms.propose(floor.order(1, quantity=20_500))
    assert floor.gate.latches() == ()
    floor.oms.propose(floor.order(2, quantity=20_500))
    assert floor.gate.latches() == ("REJECT_STREAK",) and floor.oms.propose(floor.order(3)).code == "REJECT_STREAK"


def test_an_order_the_venue_rejects_is_rejected_and_books_nothing(tmp_path):
    floor = Floor(tmp_path, reject=lambda order: "margin")
    order = floor.trade(1)
    assert floor.stage(order) == "REJECTED" and floor.held() == (0, 0, 0) and floor.oms.faults == ()


def test_partial_fills_are_each_booked_and_add_up_to_the_order(tmp_path):
    floor = Floor(tmp_path, FakeConfig(fill_chunks=3, chunk_gap_ns=10 * MS))
    order = floor.order(1, quantity=60_000)
    floor.oms.propose(order)
    floor.pump(8 * MS)
    assert floor.stage(order) == "PARTIALLY_FILLED" and floor.held() == (20_000, 20_000, 20_000)
    floor.pump(50 * MS)
    assert floor.stage(order) == "FILLED" and floor.held() == (60_000, 60_000, 60_000)
    assert floor.kinds().count("fill") == 3 and floor.oms.tracked(order.client_order_id).booked == 60_000


def test_an_immediate_order_the_venue_cannot_fill_is_cancelled_and_one_it_part_fills_keeps_the_part(tmp_path):
    floor = Floor(tmp_path, FakeConfig(respect_displayed_size=True))
    floor.market.script_quote(T0 + 20 * MS, "EUR.USD", BID, ASK, 1_000_000, 30_000)
    floor.pump(25 * MS)
    killed = floor.trade(1, quantity=40_000, tif=TimeInForce.FOK)
    assert floor.stage(killed) == "CANCELLED" and floor.held() == (0, 0, 0)
    part = floor.trade(2, quantity=40_000, tif=TimeInForce.IOC)
    assert floor.stage(part) == "CANCELLED" and floor.held() == (30_000, 30_000, 30_000)
    assert floor.oms.tracked(part.client_order_id).booked == 30_000 and floor.oms.faults == ()


def test_a_working_order_can_be_withdrawn_and_only_a_working_one(tmp_path):
    floor = Floor(tmp_path)
    resting = floor.trade(1, limit=108_400)
    assert floor.stage(resting) == "WORKING"
    assert floor.oms.withdraw(resting.client_order_id)
    floor.pump(50 * MS)
    assert floor.stage(resting) == "CANCELLED" and "cancel" in floor.kinds()
    assert not floor.oms.withdraw(resting.client_order_id), "it is finished"
    assert not floor.oms.withdraw(floor.order(9).client_order_id), "it was never an order"


def test_withdrawing_orders_counts_against_the_order_to_trade_ratio(tmp_path):
    floor = Floor(tmp_path, changes={"desk/max_order_to_trade_ratio": 4})
    for number in (1, 2):
        resting = floor.trade(number, limit=108_400)
        assert floor.oms.withdraw(resting.client_order_id)
    floor.pump(50 * MS)
    assert floor.oms.propose(floor.order(3)).code == "ORDER_TO_TRADE_SLEEVE", "two orders and two cancels, no trade"


# ---- reports that repeat, overtake and lag --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        FakeConfig(fill_chunks=4, duplicate_every=1),
        FakeConfig(fill_chunks=4, chunk_gap_ns=0, reorder_every=1),
        FakeConfig(fill_chunks=4, chunk_gap_ns=0, reorder_every=2, duplicate_every=3),
        FakeConfig(fill_chunks=2, late_ack_every=1),
        FakeConfig(fill_chunks=4, echo_every=1),
        FakeConfig(fill_chunks=3, chunk_gap_ns=0, reorder_every=1, duplicate_every=2, late_ack_every=1, echo_every=2),
    ],
)
def test_however_the_reports_arrive_the_result_is_the_clean_one_and_each_fill_is_booked_once(tmp_path, config):
    clean = Floor(tmp_path / "clean", FakeConfig(fill_chunks=config.fill_chunks))
    faulty = Floor(tmp_path / "faulty", config)
    for floor in (clean, faulty):
        for number, side in enumerate([BUY, BUY, SELL, BUY], 1):
            floor.oms.propose(floor.order(number, side, 60_000))
            floor.pump(30 * MS)
        floor.pump(5 * SECOND)
        floor.pump(5 * SECOND)
    assert faulty.oms.faults == () and faulty.gate.latches() == ()
    assert faulty.oms.book.snapshot() == clean.oms.book.snapshot() and faulty.held() == (120_000, 120_000, 120_000)
    stages = lambda floor: sorted((t.stage, t.booked, t.reported) for t in floor.oms._orders.values())  # noqa: E731
    assert stages(faulty) == stages(clean) == [(Stage.FILLED, 60_000, 60_000)] * 4
    fills = [e["fill_id"] for e in faulty.outbox() if e["type"] == "fill"]
    assert len(fills) == len(set(fills)) == 4 * config.fill_chunks, "each fill is journalled, and booked, exactly once"
    assert len([e for e in faulty.events if isinstance(e, Fill)]) >= len(fills)


def test_a_fill_before_its_acknowledgement_is_booked_when_it_comes(tmp_path):
    floor = Floor(tmp_path, FakeConfig(late_ack_every=1))
    order = floor.trade(1)
    kinds = [type(e).__name__ + (":" + e.state.name if isinstance(e, OrderEvent) else "") for e in floor.events[-5:]]
    assert kinds.index("Fill") < kinds.index("OrderEvent:WORKING"), "the scenario is the one the test is about"
    assert floor.stage(order) == "FILLED" and floor.held() == (20_000, 20_000, 20_000) and floor.oms.faults == ()


# ---- what cannot happen is recorded and stops entries ---------------------------------------------------------------


def test_a_fill_for_an_order_the_outbox_does_not_hold_is_booked_and_stops_entries(tmp_path):
    floor = Floor(tmp_path)
    stranger = floor.order(77)
    floor.market.submit(stranger, TRADED[0], floor.clock.now_ns())  # placed at the broker by another hand
    floor.pump(50 * MS)
    assert floor.held() == (20_000, 20_000, 20_000), "the book says what the venue says"
    assert (Fault.FILL_WITHOUT_ORDER, stranger.client_order_id) in floor.oms.faults
    assert floor.gate.latches() == ("ORDER_FAULT",)
    assert floor.oms.propose(floor.order(1)).code == "ORDER_FAULT"
    assert floor.oms.propose(floor.order(2, SELL)).sent, "getting out is still allowed"
    faults = [e for e in floor.outbox() if e["type"] == "fault"]
    assert [f["fault"] for f in faults] == ["REPORT_WITHOUT_ORDER", "FILL_WITHOUT_ORDER"]
    assert all(f["client_order_id"] == stranger.client_order_id for f in faults)


def test_a_report_that_contradicts_what_is_established_is_recorded_and_stops_entries(tmp_path):
    floor = Floor(tmp_path)
    order = floor.trade(1)
    now = floor.clock.now_ns()
    floor.oms.handle(OrderEvent(order.client_order_id, OrderState.CANCELLED, now, 0, filled_quantity=0))
    assert floor.oms.faults == ((Fault.CONTRADICTION, order.client_order_id),) and floor.stage(order) == "FILLED"
    floor.oms.handle(Fill("extra", order.client_order_id, "EUR.USD", BUY, 20_000, ASK, now))
    assert floor.oms.faults[-1] == (Fault.OVERFILL, order.client_order_id)
    assert floor.oms.book.position(DESK, "EUR.USD").quantity == 40_000, "a fill is booked even when it is a fault"
    assert floor.gate.latches() == ("ORDER_FAULT",) and floor.oms.propose(floor.order(2)).code == "ORDER_FAULT"
    floor.restart(up=False)
    floor.connect()
    assert floor.gate.latches() == ("ORDER_FAULT",), "a restart does not clear it"
    assert not floor.settle(tries=3), "and the book now holds 20,000 the venue never filled: no connection is marked"


def test_a_move_that_is_not_in_the_table_is_not_made_and_is_recorded_and_stops_entries(tmp_path):
    floor = Floor(tmp_path)
    order = floor.trade(1)
    tracked = floor.oms.tracked(order.client_order_id)
    floor.oms._move(tracked, Stage.WORKING, Cause.REPORT)  # a finished order cannot go back to working
    assert tracked.stage is Stage.FILLED and floor.oms.faults == ((Fault.IMPOSSIBLE_MOVE, order.client_order_id),)
    fault = floor.outbox()[-1]
    assert (fault["type"], fault["fault"]) == ("fault", "IMPOSSIBLE_MOVE")
    assert (fault["before"], fault["to"]) == ("FILLED", "WORKING")
    assert floor.gate.latches() == ("ORDER_FAULT",) and floor.oms.propose(floor.order(2)).code == "ORDER_FAULT"


def test_two_different_fills_under_one_id_are_a_fault_and_neither_is_booked_twice(tmp_path):
    floor = Floor(tmp_path)
    order = floor.trade(1)
    (first,) = [e for e in floor.events if isinstance(e, Fill)]
    floor.oms.handle(Fill(first.fill_id, order.client_order_id, "EUR.USD", BUY, 30_000, ASK, T0))
    assert floor.oms.faults == ((Fault.UNBOOKABLE_FILL, order.client_order_id),) and floor.held()[0] == 20_000


def test_a_fill_is_booked_even_when_the_gate_fails_on_it(tmp_path, monkeypatch):
    floor = Floor(tmp_path)
    order = floor.order(1)
    floor.oms.propose(order)

    def broken(event: object) -> None:
        raise OSError("the gate's journal cannot be written")

    monkeypatch.setattr(floor.gate, "observe", broken)
    floor.clock.advance(50 * MS)
    for event in floor.broker.poll():
        with pytest.raises(OSError):
            floor.oms.handle(event)
    assert floor.stage(order) == "FILLED" and floor.oms.book.position(DESK, "EUR.USD").quantity == 20_000
    assert floor.kinds().count("fill") == 1, "and it is in the outbox"


def test_an_order_that_could_not_be_journalled_is_not_sent_and_entries_stop(tmp_path, monkeypatch):
    floor = Floor(tmp_path)

    def full_disk(*arguments, **fields) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(floor.oms._outbox, "intend", full_disk)
    order = floor.order(1)
    assert floor.oms.propose(order) == Proposal(order.client_order_id, False, "NOT_JOURNALLED", None)
    assert floor.at_broker() == [] and floor.oms.faults == ((Fault.JOURNAL_FAILED, order.client_order_id),)
    assert floor.gate.latches() == ("ORDER_FAULT",)
    monkeypatch.undo()
    assert floor.oms.propose(floor.order(2)).code == "ORDER_FAULT"


def test_a_refusal_by_the_paper_lock_is_recorded_and_is_not_swallowed(tmp_path):
    floor = Floor(tmp_path)
    floor.environ["MODE"] = "production"
    order = floor.order(1)
    with pytest.raises(PaperOnlyError):
        floor.oms.propose(order)
    assert floor.at_broker() == [] and floor.stage(order) == "REJECTED"
    assert floor.broker.state is ConnectionState.REFUSED
    floor.pump()
    assert floor.oms.propose(floor.order(2)).code == "NOT_RECONCILED" and not floor.oms.reconcile()


# ---- a send that may or may not have happened -----------------------------------------------------------------------


@pytest.mark.parametrize("arrived", [True, False])
def test_an_order_whose_send_failed_part_way_is_settled_by_asking_never_by_sending_again(
    tmp_path, monkeypatch, arrived
):
    floor = Floor(tmp_path)
    real = floor.market.submit

    def cut(*arguments):
        if arrived:
            real(*arguments)
        raise VenueUnavailable("the connection dropped during the send")

    monkeypatch.setattr(floor.market, "submit", cut)
    order = floor.order(1, limit=108_400)
    assert floor.oms.propose(order) == Proposal(order.client_order_id, False, "SEND_UNCERTAIN", Stage.UNKNOWN)
    assert floor.oms.blind and floor.oms.in_flight() == (order.client_order_id,)
    monkeypatch.undo()
    assert floor.oms.propose(floor.order(2)).code == "NOT_RECONCILED", "nothing more goes out until the venue is asked"
    assert floor.oms.propose(order).code == "ALREADY_KNOWN", "and that order is not sent a second time"
    assert floor.settle()
    assert floor.stage(order) == ("CANCELLED" if arrived else "EXPIRED")
    moves = [(e["to"], e["cause"]) for e in floor.outbox() if e["type"] == "stage"]
    if arrived:
        assert [to for to, _ in moves] == ["UNKNOWN", "WORKING", "CANCELLED"], "found working, and taken back"
    else:
        assert moves == [("UNKNOWN", "SEND_FAILED"), ("EXPIRED", "NO_TRACE")]
    assert len(floor.at_broker()) == (1 if arrived else 0) and floor.market.working() == []
    assert floor.oms.propose(floor.order(3)).sent and floor.oms.faults == ()


def test_an_order_written_off_as_never_arrived_that_fills_after_all_is_booked_and_is_a_fault(tmp_path, monkeypatch):
    floor = Floor(tmp_path)
    late: list = []
    monkeypatch.setattr(floor.market, "submit", lambda *arguments: late.append(arguments) or (_ for _ in ()).throw(
        VenueUnavailable("lost on the way")))  # fmt: skip
    order = floor.order(1)
    floor.oms.propose(order)
    monkeypatch.undo()
    assert floor.settle() and floor.stage(order) == "EXPIRED"
    floor.market.submit(late[0][0], late[0][1], floor.clock.now_ns())  # it reaches the broker long after
    floor.pump(50 * MS)
    assert floor.held() == (20_000, 20_000, 20_000) and floor.stage(order) == "FILLED"
    assert Fault.CONTRADICTION in {fault for fault, _ in floor.oms.faults}
    assert floor.gate.latches() == ("ORDER_FAULT",)


# ---- a connection that drops ----------------------------------------------------------------------------------------


def test_on_a_lost_connection_every_order_in_flight_is_unknown_and_nothing_is_sent(tmp_path):
    floor = Floor(tmp_path, outage(100, 5_000))
    resting = floor.trade(1, limit=108_400)
    floor.pump(60 * MS)
    assert floor.broker.state is ConnectionState.DISCONNECTED and floor.oms.blind
    assert floor.stage(resting) == "UNKNOWN" and floor.oms.propose(floor.order(2)).code == "NOT_RECONCILED"
    assert not floor.oms.reconcile() and not floor.oms.withdraw(resting.client_order_id)


def test_on_reconnect_working_orders_are_cancelled_then_reconciled_and_only_then_is_the_connection_marked(tmp_path):
    floor = Floor(tmp_path, outage(100, 800, fill_delay_ns=30 * MS))
    resting = floor.trade(1, limit=108_400)
    filling = floor.order(2, quantity=40_000)
    floor.pump(30 * MS)
    floor.oms.propose(filling)  # sent 10 ms before the link drops: it fills while nobody is listening
    floor.pump(20 * MS)
    assert floor.stage(filling) == "UNKNOWN" and floor.held() == (0, 0, 0)
    floor.pump(1_100 * MS)
    assert floor.held() == (0, 0, 40_000), "filled at the broker, unknown here"
    assert floor.broker.state is RECONCILING, "back, and not yet safe"
    assert not floor.oms.reconcile(), "the first step only asks for the working order to be cancelled"
    assert floor.broker.state is RECONCILING and floor.market.working() != []
    assert floor.oms.propose(floor.order(3)).code == "NOT_RECONCILED"
    floor.pump(10 * MS)
    assert floor.oms.reconcile() and floor.broker.state is READY
    assert floor.market.working() == [] and floor.stage(resting) == "CANCELLED" and floor.stage(filling) == "FILLED"
    assert floor.held() == (40_000, 40_000, 40_000), "the fill that was missed is booked, once"
    order_of = [k for k in floor.kinds() if k in ("cancel", "fill", "reconciled")]
    assert order_of[-3:] == ["cancel", "fill", "reconciled"]
    floor.pump()
    assert floor.oms.propose(floor.order(4)).sent and floor.oms.faults == ()


def test_an_order_sent_as_the_link_drops_is_found_again_by_asking_the_venue(tmp_path):
    floor = Floor(tmp_path, outage(100, 800))
    floor.pump(40 * MS)
    floor.clock.advance(59 * MS)  # one millisecond before the link drops: not one report will come back
    order = floor.order(1)
    assert floor.oms.propose(order).sent and floor.stage(order) == "SENT"
    floor.pump(10 * MS)
    assert floor.stage(order) == "UNKNOWN" and [e for e in floor.events if isinstance(e, Fill)] == []
    floor.pump(1_200 * MS)
    assert floor.settle() and floor.stage(order) == "FILLED" and floor.held() == (20_000, 20_000, 20_000)
    settled = [e["cause"] for e in floor.outbox() if e["type"] == "stage" and e["to"] == "FILLED"]
    assert settled == ["FILL"] and len(floor.at_broker()) == 1 and floor.oms.faults == ()


def test_the_connection_is_not_marked_while_the_venue_holds_a_position_the_book_cannot_explain(tmp_path):
    floor = Floor(tmp_path, up=False)
    floor.market.carry(TRADED[0], 20_000)
    floor.connect()
    assert not floor.settle(tries=5) and floor.broker.state is RECONCILING and floor.oms.blind
    assert floor.kinds().count("positions_differ") == 1, "written once, not at every step"
    assert floor.outbox()[-1]["differences"] == [["EUR.USD", "CASH", 0, 20_000]]
    assert floor.oms.propose(floor.order(1)).code == "NOT_RECONCILED"
    floor.market.carry(TRADED[0], -20_000)  # the operator has closed it at the broker
    assert floor.settle() and floor.broker.state is READY


def test_a_working_order_that_is_not_ours_keeps_the_connection_unmarked_and_is_a_fault(tmp_path):
    floor = Floor(tmp_path, up=False)
    floor.connect()
    by_hand = OpenOrder(None, "B000099", "EUR.USD", BUY, 20_000, 0, floor.order(1).order_type, None, TimeInForce.DAY,
                        OrderState.WORKING)  # fmt: skip
    floor.broker.open_orders = lambda: (by_hand,)
    assert not floor.settle(tries=3) and floor.broker.state is RECONCILING
    assert floor.oms.faults == ((Fault.FOREIGN_ORDER, None),), "said once"
    del floor.broker.open_orders
    assert floor.settle()


def test_a_working_order_of_ours_the_outbox_does_not_hold_is_cancelled_and_is_a_fault(tmp_path):
    floor = Floor(tmp_path, up=False)
    orphan = floor.order(5, limit=108_400)
    floor.connect()
    floor.market.submit(orphan, TRADED[0], floor.clock.now_ns())
    assert floor.settle() and floor.market.working() == []
    assert (Fault.UNTRACKED_ORDER, orphan.client_order_id) in floor.oms.faults


# ---- a restart ------------------------------------------------------------------------------------------------------


def test_a_new_manager_on_the_same_outbox_has_the_same_book_and_the_gate_the_same_positions(tmp_path):
    floor = Floor(tmp_path, FakeConfig(fill_chunks=2))
    floor.trade(1, quantity=40_000)
    floor.trade(2, SELL, 20_000, sleeve="ml")
    floor.trade(3, pair="USD.JPY")
    before = floor.oms.book.snapshot()
    floor.restart(up=False)
    assert floor.oms.book.snapshot() == before, "from the journal alone, before the venue is asked anything"
    assert floor.gate.book.snapshot()["lines"] == before["lines"]
    assert floor.oms.blind and floor.oms.in_flight() == ()
    floor.up()
    assert floor.oms.book.snapshot() == before and floor.held() == (20_000, 20_000, 20_000)
    assert floor.oms.propose(floor.order(1, quantity=40_000)).code == "ALREADY_KNOWN", "an id is remembered for good"


def test_after_a_restart_an_order_that_was_in_flight_is_unknown_until_the_venue_has_been_asked(tmp_path):
    floor = Floor(tmp_path)
    resting = floor.trade(1, limit=108_400)
    filled = floor.trade(2)
    floor.restart(up=False)
    assert floor.stage(resting) == "UNKNOWN" and floor.stage(filled) == "FILLED"
    restart = [e for e in floor.outbox() if e.get("cause") == "RESTART"]
    assert [e["client_order_id"] for e in restart] == [resting.client_order_id]
    floor.up()
    assert floor.stage(resting) == "CANCELLED" and floor.market.working() == []
    assert floor.kinds().count("start") == 2 and verify(tmp_path / "outbox.jsonl").entries == len(floor.outbox())


def test_a_damaged_outbox_stops_the_start(tmp_path):
    floor = Floor(tmp_path)
    floor.trade(1)
    path = tmp_path / "outbox.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"quantity":20000', b'"quantity":20001', 1))
    with pytest.raises(JournalError, match="does not match its own hash"):
        floor.restart(up=False)


def test_an_outbox_that_is_a_valid_chain_and_makes_no_sense_stops_the_start_too(tmp_path):
    floor = Floor(tmp_path)

    def started_on(name: str, *entries: tuple[str, dict]) -> OrderManager:
        box = Outbox(tmp_path / name, fsync=False)
        box.open()
        for kind, fields in entries:
            box.record(kind, **fields)
        box.close()
        return OrderManager(floor.broker, floor.gate, Outbox(tmp_path / name), clock=floor.clock, instruments=TRADED)

    order = floor.order(1)
    intent = ("intent", {"at_ns": T0, "code": "ALLOW", "outcome": "ALLOW", **order_fields(order)})
    moved = {"client_order_id": order.client_order_id, "reported": 0, "acked": 1}
    assert started_on("good.jsonl", intent, ("stage", {**moved, "to": "SENT"})).in_flight() == (order.client_order_id,)
    with pytest.raises(OutboxError, match="never recorded"):
        started_on("orphan.jsonl", ("stage", {**moved, "to": "FILLED"}))
    with pytest.raises(OutboxError, match="a stage that does not exist"):
        started_on("stage.jsonl", intent, ("stage", {**moved, "to": "DONE"}))
    with pytest.raises(OutboxError, match="is not an order"):
        started_on("order.jsonl", ("intent", {**intent[1], "side": "HOLD"}))
    with pytest.raises(OutboxError, match="quantity is not a whole number"):
        started_on("float.jsonl", ("intent", {**intent[1], "quantity": "20000"}))


# ---- the shadow venue, unchanged ------------------------------------------------------------------------------------


def test_the_manager_works_unchanged_against_the_shadow_venue(tmp_path):
    from hftlab.core import Quote
    from hftlab.venues.sim import SimVenue
    from tests.venues.kit import plain_costs

    floor = Floor(tmp_path, up=False)
    venue = SimVenue(floor.clock, TRADED, plain_costs())
    oms = floor.manager(venue)

    def pump(duration_ns: int = 0, quote: bool = False) -> None:
        floor.clock.advance(duration_ns)
        if quote:
            venue.feed(Quote("EUR.USD", BID, ASK, 10**9, 10**9, floor.clock.now_ns(), floor.clock.now_ns() - T0 + 1))
        for event in venue.poll():
            oms.handle(event)

    venue.connect()
    venue.subscribe_quotes(venue.instrument("EUR.USD", InstrumentKind.CASH))
    pump(quote=True)
    assert oms.reconcile() and venue.state is READY
    pump()
    order = floor.order(1)
    assert oms.propose(order).sent
    pump(10 * MS, quote=True)
    assert oms.tracked(order.client_order_id).stage is Stage.FILLED and oms.faults == ()
    assert oms.book.position(DESK, "EUR.USD").quantity == 20_000 == venue.positions()[0].quantity
    resting = floor.order(2, limit=108_400)
    assert oms.propose(resting).sent
    pump(10 * MS, quote=True)
    assert oms.withdraw(resting.client_order_id)
    pump(10 * MS, quote=True)
    assert oms.tracked(resting.client_order_id).stage is Stage.CANCELLED
    venue.disconnect()
    pump()
    assert oms.blind and oms.propose(floor.order(3)).code == "NOT_RECONCILED"
    venue.connect()
    pump(quote=True)
    assert oms.reconcile() and not oms.blind
