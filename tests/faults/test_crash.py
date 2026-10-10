"""Kill the order manager at every step of the outbox and start it again: nothing lost, nothing sent twice."""
from __future__ import annotations

import pytest

from hftlab.core import Side
from hftlab.venues.fake import FakeConfig
from tests.exec.kit import MS, SECOND, Floor
from tests.faults.kit import AFTER, BEFORE, TORN, Kill, Killed, consistent

CONFIG = FakeConfig(fill_chunks=2, chunk_gap_ns=10 * MS)


def orders(floor: Floor) -> list:
    return [
        floor.order(1, Side.BUY, 40_000),  # fills in two executions, ten milliseconds apart
        floor.order(2, Side.BUY, 20_000, limit=108_400),  # rests
        floor.order(3, Side.SELL, 20_000),
    ]


def session(floor: Floor) -> None:
    """A short trading session that touches every kind of step: orders, fills in pieces, a resting order, a cancel."""
    first, resting, third = orders(floor)
    floor.oms.propose(first)
    floor.pump(3 * MS)  # acknowledged
    floor.pump(4 * MS)  # its first execution
    floor.oms.propose(resting)
    floor.pump(10 * MS)  # the second execution of the first order; the resting order acknowledged
    floor.oms.propose(third)
    floor.pump(50 * MS)
    floor.oms.withdraw(resting.client_order_id)
    floor.pump(20 * MS)


def steps(tmp_path) -> list[str]:
    """Every step the session takes when nothing kills it."""
    floor, watcher = Floor(tmp_path / "whole", CONFIG), Kill()
    watcher.arm(floor)
    session(floor)
    return watcher.steps


def killed_at(tmp_path, at: int, how: str) -> tuple[Floor, Kill]:
    """Run the session, kill it at step `at`, and start a new process on what is left."""
    floor, kill = Floor(tmp_path / f"{how}-{at}", CONFIG), Kill(at, how)
    kill.arm(floor)
    with pytest.raises(Killed):
        session(floor)
    Kill.disarm(floor)
    floor.clock.advance(2 * SECOND)  # the time a restart takes
    floor.restart()
    return floor, kill


def test_the_session_passes_through_every_step_the_kill_is_meant_to_land_on(tmp_path):
    taken = steps(tmp_path)
    assert len(taken) > 40
    first_intent, first_send = taken.index("journal:intent"), taken.index("send")
    assert first_intent < first_send, "the outbox is written before the broker is handed anything"
    assert taken[first_send + 1] == "journal:stage", "and the send is recorded after it returned"
    assert taken.count("journal:intent") == taken.count("send") == 3 and taken.count("cancel") == 1
    assert taken.count("journal:fill") == taken.count("report:Fill") == 4, "two executions for each of two orders"
    assert {"report:OrderEvent", "report:CommissionEvent", "journal:commission", "journal:cancel"} <= set(taken)


@pytest.mark.parametrize("how", [BEFORE, AFTER])
def test_killed_at_any_step_and_restarted_no_order_is_lost_none_is_duplicated_and_the_book_is_right(tmp_path, how):
    for at, label in enumerate(steps(tmp_path), 1):
        floor, kill = killed_at(tmp_path, at, how)
        said = f"killed {how} step {at} ({label})"
        assert kill.died_at == label and floor.oms.faults == () and floor.gate.latches() == (), said
        consistent(floor, said=said)


def test_killed_half_way_through_any_line_of_the_outbox_the_torn_line_is_cut_off_and_nothing_is_lost(tmp_path):
    lines = [(at, label) for at, label in enumerate(steps(tmp_path), 1) if label.startswith("journal:")]
    assert len(lines) > 20
    for at, label in lines:
        floor, _ = killed_at(tmp_path, at, TORN)
        assert floor.oms.faults == (), label
        consistent(floor, said=f"killed while writing step {at} ({label})")


@pytest.mark.parametrize(
    ("label", "how", "at_broker"),
    [
        ("journal:intent", BEFORE, 0),  # before the journal: nothing written, nothing sent
        ("journal:intent", AFTER, 0),  # after the journal, before the send: written, and not at the broker
        ("send", BEFORE, 0),  # the same, seen from the other side
        ("send", AFTER, 1),  # after the send, before the acknowledgement, with nothing recorded but the intent
        ("report:OrderEvent", BEFORE, 1),  # the acknowledgement arrives and is never taken
    ],
)
def test_the_first_order_killed_at_each_step_of_its_way_out_is_settled_by_what_the_broker_holds(
    tmp_path, label, how, at_broker
):
    at = steps(tmp_path).index(label) + 1
    floor, _ = killed_at(tmp_path, at, how)
    first = orders(floor)[0]
    journalled = [e for e in floor.outbox() if e["type"] == "intent"]
    assert len(floor.at_broker()) == at_broker
    if label == "journal:intent" and how == BEFORE:
        assert journalled == [] and floor.oms.tracked(first.client_order_id) is None
    elif at_broker == 0:
        assert floor.stage(first) == "EXPIRED", "written down, never arrived, and not sent again"
        causes = [e["cause"] for e in floor.outbox() if e["type"] == "stage"]
        assert causes == ["RESTART", "NO_TRACE"]
    else:
        assert floor.stage(first) == "FILLED" and floor.held() == (40_000, 40_000, 40_000)
        assert "RESTART" in [e.get("cause") for e in floor.outbox()], "it was unknown until the broker was asked"
    consistent(floor, said=f"{how} {label}")


def test_killed_between_the_two_executions_of_an_order_the_second_is_booked_from_the_broker_once(tmp_path):
    taken = steps(tmp_path)
    mid_fill = taken.index("journal:fill") + 1  # the first execution is journalled; the second has not happened yet
    floor, kill = killed_at(tmp_path, mid_fill, AFTER)
    first = orders(floor)[0]
    assert kill.died_at == "journal:fill" and floor.stage(first) == "FILLED"
    assert floor.oms.tracked(first.client_order_id).booked == 40_000 and floor.held() == (40_000, 40_000, 40_000)
    assert floor.kinds().count("fill") == 2
    consistent(floor, said="mid-fill")


def test_the_same_decisions_made_again_after_a_restart_are_not_sent_a_second_time(tmp_path):
    for at, label in enumerate(steps(tmp_path), 1):
        floor, _ = killed_at(tmp_path, at, AFTER)
        known = set(floor.at_broker())
        for order in orders(floor):  # the trader starts over and decides what it decided before
            proposal = floor.oms.propose(order)
            assert proposal.sent == (floor.oms.tracked(order.client_order_id) is not None and proposal.code == "ALLOW")
            if order.client_order_id in known:
                assert proposal.code == "ALREADY_KNOWN", f"{label}: an order at the broker was proposed to it again"
        floor.pump(50 * MS)
        for order in orders(floor):
            floor.oms.withdraw(order.client_order_id)
        floor.pump(50 * MS)
        assert len(floor.at_broker()) == len(set(floor.at_broker())) <= 3
        consistent(floor, said=f"decided again after a kill at step {at} ({label})")
