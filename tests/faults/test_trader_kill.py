"""The trader killed at a chosen step and started again on the same files: nothing is sent or booked twice.

A step is anything that leaves a mark outside the process: a line of the tape, of the outbox, of the shadow
book's journal, of the decision journal, of the stage journal, and an order or a cancel handed to the broker.
The process dies before the step or just after it; what it held in memory is gone, the broker lives on, and a
new process starts where a restart would: from the journals, blind until it has reconciled.
"""
from __future__ import annotations

import pytest

from hftlab.book import DESK
from hftlab.core import InstrumentKind
from hftlab.exec import TERMINAL
from hftlab.journal import read, verify
from tests.faults.kit import AFTER, BEFORE, Kill, Killed
from tests.trader.kit import TRADED, Desk, put_stage

RUN_SECONDS = 12


def arm(kill: Kill, desk: Desk) -> None:
    """Put the kill between the trader and everything it touches. Every line reaches the file as it is written."""
    trader = desk.trader

    def lines(label: str, journal) -> None:
        write = journal.write

        def written(kind: str, **fields):
            def line():
                found = write(kind, **fields)
                journal.push()
                return found

            return kill.step(f"{label}:{kind}", line)

        journal.write = written

    lines("outbox", trader.manager._outbox._journal)
    lines("decisions", trader._journal)
    lines("shadow", trader._shadow_journal)
    lines("stages", trader.stages._journal)
    tape, submit, cancel = desk.edge.write, desk.market.submit, desk.market.request_cancel
    desk.edge.write = lambda row: kill.step(f"tape:{row[0]}", lambda: tape(row))
    desk.market.submit = lambda *order: kill.step("send", lambda: submit(*order))
    desk.market.request_cancel = lambda *which: kill.step("cancel", lambda: cancel(*which))


def disarm(desk: Desk) -> None:
    for name in ("submit", "request_cancel"):
        desk.market.__dict__.pop(name, None)


def a_desk(folder) -> Desk:
    put_stage(folder, "control", "min_size")
    return Desk(folder)


def consistent(desk: Desk, said: str) -> None:
    """After a kill and a recovery: no order twice, no fill twice, and the books say what the broker says."""
    folder, manager = desk.folder, desk.trader.manager
    intents = [str(e["client_order_id"]) for e in desk.outbox() if e["type"] == "intent"]
    assert len(intents) == len(set(intents)), f"{said}: an order was journalled twice"
    assert set(desk.market._orders) <= set(intents), f"{said}: the broker holds an order the outbox never recorded"
    for order_id in intents:
        tracked = manager.tracked(order_id)
        assert tracked is not None, said
        executed = desk.market._orders[order_id].executed if order_id in desk.market._orders else 0
        if tracked.stage in TERMINAL:
            assert tracked.booked == executed, f"{said}: {order_id} booked {tracked.booked}, the broker {executed}"
    executions = sorted(e.exec_id for e in desk.market.executions())
    assert manager.book.snapshot()["fills"] == executions, f"{said}: a broker fill was lost or booked twice"
    journalled = [str(e["fill_id"]) for e in desk.outbox() if e["type"] == "fill"]
    assert sorted(journalled) == executions, f"{said}: a fill is in the outbox twice, or not at all"
    for instrument in TRADED:
        theirs = desk.market.positions().get((instrument.pair, InstrumentKind.CASH), 0)
        assert manager.book.position(DESK, instrument.pair).quantity == theirs, f"{said}: {instrument.pair} differs"
        assert desk.trader.gate.book.position(DESK, instrument.pair).quantity == theirs, said
    shadow = [str(e["fill_id"]) for e in read(folder / "shadow.jsonl")[1] if e["type"] == "fill"]
    assert len(shadow) == len(set(shadow)), f"{said}: a shadow fill was journalled twice"
    assert sorted(shadow) == desk.trader.shadow_book.snapshot()["fills"], f"{said}: the shadow book is not its journal"
    told = [str(e["fill_id"]) for e in desk.decisions() if e["type"] == "broker_fill"]
    assert len(told) == len(set(told)) and set(told) <= set(executions), f"{said}: a sleeve was told of a fill twice"
    for name in ("outbox", "risk", "reconcile", "stages", "shadow"):
        assert verify(folder / f"{name}.jsonl").entries > (0 if name != "reconcile" else -1), name
    assert verify(desk.day() / "decisions.jsonl").entries > 0


def steps_of_a_run(folder) -> list[str]:
    desk, kill = a_desk(folder), Kill()
    arm(kill, desk)
    desk.run(RUN_SECONDS)
    return kill.steps


def interesting(steps: list[str]) -> list[int]:
    """Every step from the first order's journalling to the end of its round trip, and a sample of the rest."""
    first = steps.index("outbox:intent") + 1
    second = first + steps[first:].index("outbox:intent") + 1
    third = second + steps[second:].index("outbox:intent") + 1
    return sorted({*range(first - 3, third + 2), *range(1, first, 7), *range(third, len(steps), 23)})


def test_the_steps_of_a_run_include_everything_that_leaves_a_mark(tmp_path):
    steps = steps_of_a_run(tmp_path)
    for label in ("tape:step", "tape:ask", "tape:sent", "outbox:intent", "send", "outbox:stage", "outbox:fill",
                  "decisions:verdict", "decisions:shadow_order", "shadow:fill", "decisions:broker_fill"):  # fmt: skip
        assert label in steps, label
    assert steps.index("outbox:intent") < steps.index("send") < steps.index("tape:sent")
    assert len(interesting(steps)) > 60


@pytest.mark.parametrize("how", [BEFORE, AFTER])
def test_a_kill_at_any_step_leaves_nothing_duplicated_and_the_next_start_reconciles_first(tmp_path, how):
    total = steps_of_a_run(tmp_path / "count")
    for at in interesting(total):
        folder = tmp_path / f"{how}-{at}"
        desk, kill = a_desk(folder), Kill(at, how)
        arm(kill, desk)
        with pytest.raises(Killed):
            desk.run(RUN_SECONDS)
        said = f"killed {how} step {at} ({kill.died_at})"
        disarm(desk)
        sent_before = len(desk.market._orders)
        desk.start()  # nothing of the dead process is closed or flushed: it is simply gone
        assert desk.trader.manager.blind, said
        arm(recorder := Kill(), desk)
        desk.run(RUN_SECONDS)
        disarm(desk)
        kinds = recorder.steps
        if "send" in kinds:
            assert "outbox:reconciled" in kinds[: kinds.index("send")], f"{said}: an order went out before reconciling"
        desk.run(3 * RUN_SECONDS)
        desk.stop()
        assert len(desk.market._orders) > sent_before, f"{said}: the trader never traded again"
        consistent(desk, said)
        found = desk.replay(tmp_path / f"replay-{how}-{at}")
        assert all(one.identical for one in found), f"{said}: {[one.as_dict() for one in found]}"
        assert [one.stopped for one in found] == [False, True], said
