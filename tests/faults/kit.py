"""Shared pieces for the fault tests: a way to kill the trading process at a chosen step, and what must hold after."""
from __future__ import annotations

import os

from hftlab.book import DESK
from hftlab.core import InstrumentKind
from hftlab.exec import TERMINAL
from hftlab.journal import verify
from tests.exec.kit import TRADED, Floor

BEFORE, AFTER, TORN = "before", "after", "torn"


class Killed(BaseException):
    """The process is gone. Not an `Exception`: nothing in the code under test may catch it and carry on."""


class Kill:
    """Counts the steps a trading process takes and ends it at one of them.

    A step is anything that leaves a mark outside the process's memory, or brings one in: a line written to the
    outbox, an order or a cancel handed to the broker, a report taken from it. `at` is the number of the step
    to die at; `how` says whether the step happens first (AFTER), does not happen (BEFORE), or, for a line of
    the outbox, is cut off half way through (TORN).
    """

    def __init__(self, at: int | None = None, how: str = BEFORE) -> None:
        self.at, self.how, self.steps = at, how, []
        self.died_at: str | None = None

    def step(self, label: str, do, tear=None):
        self.steps.append(label)
        mine = len(self.steps) == self.at
        if mine and self.how == BEFORE:
            self.died_at = label
            raise Killed(label)
        result = do()
        if mine and (self.how == AFTER or tear is not None):
            self.died_at = label
            if self.how == TORN:
                tear()
            raise Killed(label)
        return result

    def arm(self, floor: Floor) -> None:
        """Put this kill between the process and everything it touches."""
        journal = floor.oms._outbox._journal
        write, handle = journal.write, floor.oms.handle
        submit, cancel = floor.market.submit, floor.market.request_cancel

        def line(kind: str, fields: dict) -> str:
            """One line of the outbox, as far as the file: a kill after this step finds the whole line there."""
            written = write(kind, **fields)
            journal.push()
            return written

        def tear() -> None:
            data = journal.path.read_bytes()
            start = data.rstrip(b"\n").rfind(b"\n") + 1
            os.truncate(journal.path, start + (len(data) - start) // 2)

        journal.write = lambda kind, **fields: self.step(f"journal:{kind}", lambda: line(kind, fields), tear)
        floor.market.submit = lambda *order: self.step("send", lambda: submit(*order))
        floor.market.request_cancel = lambda *which: self.step("cancel", lambda: cancel(*which))
        floor.oms.handle = lambda event: self.step(f"report:{type(event).__name__}", lambda: handle(event))

    @staticmethod
    def disarm(floor: Floor) -> None:
        """The broker lives on after the process that was talking to it."""
        for name in ("submit", "request_cancel"):
            floor.market.__dict__.pop(name, None)


def consistent(floor: Floor, *, said: str = "") -> None:
    """After a recovery: no order lost, none duplicated, every fill booked once, and the books agree."""
    intents = [str(e["client_order_id"]) for e in floor.outbox() if e["type"] == "intent"]
    assert len(intents) == len(set(intents)), f"{said}: an order was journalled twice"
    at_broker = floor.market._orders
    assert set(at_broker) <= set(intents), f"{said}: the broker holds an order the outbox never recorded"
    for order_id in intents:
        tracked = floor.oms.tracked(order_id)
        assert tracked is not None and tracked.stage in TERMINAL, f"{said}: {order_id} was not settled"
        executed = at_broker[order_id].executed if order_id in at_broker else 0
        assert tracked.booked == executed, f"{said}: {order_id} booked {tracked.booked}, the broker filled {executed}"
    assert floor.market.working() == [] and floor.oms.in_flight() == (), f"{said}: something still works"
    executions = sorted(e.exec_id for e in floor.market.executions())
    assert floor.oms.book.snapshot()["fills"] == executions, f"{said}: a fill was lost or booked that never happened"
    journalled = [str(e["fill_id"]) for e in floor.outbox() if e["type"] == "fill"]
    assert sorted(journalled) == executions, f"{said}: a fill is in the outbox twice, or not at all"
    for instrument in TRADED:
        theirs = floor.market.positions().get((instrument.pair, InstrumentKind.CASH), 0)
        cash = -sum(e.side.sign * e.quantity * e.price for e in floor.market.executions() if e.pair == instrument.pair)
        for book in (floor.oms.book, floor.gate.book):
            position = book.position(DESK, instrument.pair)
            assert (position.quantity, position.quote_cash) == (theirs, cash), f"{said}: {instrument.pair} differs"
    assert floor.oms.book.fees() == ({"USD": 2_000_000 * len(executions)} if executions else {}), said
    assert verify(floor.folder / "outbox.jsonl").entries > 0 and verify(floor.folder / "risk.jsonl").entries > 0
