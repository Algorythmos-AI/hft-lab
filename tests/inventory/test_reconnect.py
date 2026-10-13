"""Spot on a cash account is reconciled on cash, not on the positions the venue lists (ADR 0031, note of
2026-10-13; HFT-EXP-0009).

The fake broker lists a spot position as the real gateway was measured to: on the connection that saw its
executions and on no other. So after any reconnection, and after any restart, its position list is empty while
the float stands. That must open no break and stop nothing; the cash is what is compared, and a difference in
cash is still a break. Made-up balances.
"""
from __future__ import annotations

from hftlab.book import FLOAT, differences
from hftlab.core import InstrumentKind, Side
from hftlab.journal import read
from hftlab.venues.port import ConnectionState, VenuePosition
from tests.inventory.kit import TRADED, UNIT, Bureau, Till, account, put_stage
from tests.venues.kit import EURUSD, MS, SECOND, T0

DOWN = T0 + 45 * SECOND  # after the funding, which takes about half a minute here
BACK = DOWN + 6 * SECOND


def dropped(**changes):
    return account(outages=((DOWN, BACK),), **changes)


def funded(folder, config, *, stage: str | None = "min_size") -> Bureau:
    if stage:
        put_stage(folder, "control", stage)
    desk = Bureau(folder, config)
    desk.fund()
    assert desk.clock.now_ns() < DOWN, "the float stands before the link goes"
    return desk


def breaks(desk: Bureau) -> list[int]:
    return [int(entry["broken"]) for entry in desk.of("break")]


def test_the_fake_lists_a_spot_position_only_on_the_connection_that_saw_its_executions(tmp_path):
    desk = funded(tmp_path, dropped(), stage=None)
    listed = {held.pair: held.quantity for held in desk.broker.positions()}
    assert listed == desk.float_lines() and listed, "on the connection the conversions were made on"
    cash = desk.market.cash()
    desk.run(30)
    assert desk.clock.now_ns() > BACK and desk.broker.state is ConnectionState.READY
    assert desk.broker.positions() == (), "and on the next one, nothing"
    assert desk.market.cash() == cash and {c.currency: c.amount for c in desk.broker.cash()} == cash
    assert desk.positions() == desk.float_lines(), "nothing was sold: the account holds what it held"


def test_a_reconnection_that_lists_no_positions_opens_no_break_and_the_cash_is_compared_at_once(tmp_path):
    desk = funded(tmp_path, dropped(), stage=None)
    desk.run(30)
    states = [entry["state"] for entry in desk.of("connection")]
    assert "DISCONNECTED" in states and states[-1] == "READY"
    assert not desk.trader.manager.blind and desk.trader.manager.differing == ()
    assert breaks(desk) == [] and "reconciliation_break" not in desk.pages.fired
    assert not [e for e in desk.outbox() if e["type"] == "positions_differ"]
    assert (tmp_path / "reconcile.jsonl").read_bytes() == b"", "no difference was ever written down"
    assert BACK <= desk.trader.float_status()["cash_at_ns"] < BACK + 2 * SECOND, "not a minute later: at once"
    assert desk.trader.float_status()["cash"] == desk.market.cash()


def test_after_a_reconnection_a_sleeve_still_enters_and_still_exits(tmp_path):
    desk = funded(tmp_path, dropped())
    desk.run(120)
    after = [o for o in desk.of("order") if o["sleeve"] == "control" and o["at_ns"] > BACK]
    sent = {v["client_order_id"] for v in desk.of("verdict") if v["sent"] == 1}
    exits = [o for o in after if o["reducing"] == 1 and o["client_order_id"] in sent]
    entries = [o for o in after if o["reducing"] == 0 and o["client_order_id"] in sent]
    assert exits and entries, "both go on, on a connection that lists no position at all"
    fills = [f for f in desk.of("broker_fill") if f["sleeve"] == "control" and f["at_ns"] > BACK]
    assert len(fills) >= 2 and breaks(desk) == []
    assert desk.trader.stages.stage("control") == "min_size", "nothing stepped it down"


def test_a_restart_on_a_funded_float_resumes_from_the_book_and_opens_no_break(tmp_path):
    desk = funded(tmp_path, account())
    for _ in range(40):
        desk.run(1)
        if desk.held()[0] or desk.held("AUD.USD")[0]:
            break
    held = dict(desk.trader.broker_book.open_lines())
    assert any(name == "control" for name, _ in held), "a sleeve holds a position when the process dies"
    desk.restart()
    assert desk.broker.positions() == (), "a new process is a new connection: nothing is listed"
    desk.run(2)
    assert not desk.trader.manager.blind and breaks(desk) == []
    inherited = {(e["sleeve"], e["pair"]): e["quantity"] for e in desk.of("inherited")}
    assert inherited == {key: quantity for key, quantity in held.items() if key[0] == "control"}
    desk.run(60)
    assert not [key for key in desk.trader.broker_book.open_lines() if key in inherited] or desk.of("broker_fill")
    assert desk.float_lines() == {pair: q for (name, pair), q in held.items() if name == FLOAT}
    assert breaks(desk) == [] and desk.trader.float_status()["phase"] == "funded"


def test_flatten_after_a_reconnection_closes_every_sleeve_back_to_the_float_from_the_book(tmp_path):
    desk = funded(tmp_path, dropped())
    desk.run(30)
    assert desk.clock.now_ns() > BACK
    for _ in range(60):
        if any(name == "control" for name, _ in desk.trader.broker_book.open_lines()):
            break
        desk.run(1)
    floats = desk.float_lines()
    desk.switched.add("FLATTEN")
    desk.run(30)
    assert desk.float_lines() == floats, "the float is what everything is closed back to"
    assert not [key for key in desk.trader.broker_book.open_lines() if key[0] != FLOAT]
    assert breaks(desk) == []


def test_cash_that_is_not_the_books_after_a_reconnection_is_still_a_break(tmp_path):
    desk = funded(tmp_path, dropped())
    desk.run(11)
    assert DOWN - 2 * SECOND < desk.clock.now_ns() < DOWN
    desk.market.post("USD", -40 * UNIT)  # while nobody can see: no execution explains it
    desk.run(10)
    assert not desk.trader.manager.blind, "the connection is made safe: its orders and executions are settled"
    assert breaks(desk) == [], "a difference is given its grace time first"
    assert [e["type"] for e in desk.reconciled()] == ["differing"], "found by the comparison made at once"
    desk.run(65)  # the next comparison on the schedule finds it older than the grace time
    assert breaks(desk) == [1] and "reconciliation_break" in desk.pages.fired
    opened = [e for e in desk.reconciled() if e["type"] == "break_opened"]
    assert len(opened) == 1 and opened[0]["differences"] == [] and [c[0] for c in opened[0]["cash"]] == ["USD"]
    assert desk.trader.stages.stage("control") == "stepped_down"
    desk.run(60)
    late = [v for v in desk.of("verdict") if v["at_ns"] > opened[0]["at_ns"]]
    orders = {o["client_order_id"]: o for o in desk.of("order")}
    assert not [v for v in late if v["sent"] == 1 and orders[v["client_order_id"]]["reducing"] == 0]
    assert not [key for key in desk.trader.broker_book.open_lines() if key[0] != FLOAT], "exits went on"


def test_cash_within_the_tolerance_is_agreement(tmp_path):
    desk = funded(tmp_path, dropped(), stage=None)
    desk.market.post("USD", UNIT // 2)
    desk.run(90)
    assert breaks(desk) == [] and (tmp_path / "reconcile.jsonl").read_bytes() == b""


def across_an_outage(folder, *, spot_on_cash: bool) -> Till:
    """The gate, the manager and the reconciler alone: funded, then the link drops and comes back."""
    down = T0 + 20 * SECOND
    till = Till(folder, account(outages=((down, down + 5 * SECOND),)), spot_on_cash=spot_on_cash)
    till.fund()
    assert till.clock.now_ns() < down and till.oms.reconcile()
    while till.clock.now_ns() < down + 6 * SECOND or till.broker.state is ConnectionState.DISCONNECTED:
        till.pump(250 * MS)
    assert till.broker.state is ConnectionState.RECONCILING and till.oms.blind
    assert till.broker.positions() == () and till.oms.book.open_lines()
    return till


def test_without_a_float_in_the_limits_positions_are_reconciled_exactly_as_before(tmp_path):
    till = across_an_outage(tmp_path, spot_on_cash=False)
    for _ in range(20):
        assert not till.oms.reconcile()
        till.pump(250 * MS)
    assert till.oms.blind and till.broker.state is ConnectionState.RECONCILING, "nothing may go"
    lines = {pair: held for (_, pair), held in till.oms.book.open_lines().items()}
    assert {(d.pair, d.ours, d.venue) for d in till.oms.differing} == {(pair, q, 0) for pair, q in lines.items()}
    written = [e for e in read(tmp_path / "outbox.jsonl")[1] if e["type"] == "positions_differ"]
    assert len(written) == 1, "said once, however often it is found"
    assert till.send(Side.SELL, 20_000) == "NOT_RECONCILED"
    assert till.reconciler.check().differences and not till.reconciler.broken
    till.pump(till.risk.reconciliation_grace_ns)
    assert till.reconciler.check().broken, "and the reconciler's own comparison of positions breaks as before"


def test_with_a_float_the_manager_marks_the_connection_and_the_reconciler_compares_the_cash(tmp_path):
    till = across_an_outage(tmp_path, spot_on_cash=True)
    assert till.oms.reconcile() and not till.oms.blind and till.oms.differing == ()
    assert till.broker.state is ConnectionState.READY
    found = till.reconciler.check()
    assert found.asked and not found.differences and not found.cash and found.seen_cash == till.market.cash()
    till.tick()
    till.pump(50 * MS)
    assert till.send(Side.SELL, 20_000) == "ALLOW" and till.agrees()
    till.market.post("EUR", -3 * UNIT)
    assert [c.currency for c in till.reconciler.check().cash] == ["EUR"] and not till.reconciler.broken
    till.pump(till.risk.reconciliation_grace_ns)
    assert till.reconciler.check().broken, "cash that no fill explains is a break once the grace time is over"


def test_only_spot_is_left_to_the_cash_and_only_when_asked(tmp_path):
    till = Till(tmp_path)
    till.fund()
    book = till.oms.book
    assert differences(book, ()) != (), "the float's lines against an empty list: a difference, as it ever was"
    assert differences(book, (), spot_on_cash=True) == ()
    stray = (VenuePosition("EUR.USD", InstrumentKind.CFD, 5), VenuePosition("GBP.USD", InstrumentKind.CASH, 7))
    found = differences(book, stray, spot_on_cash=True)
    assert [(d.pair, d.kind, d.ours, d.venue) for d in found] == [("EUR.USD", InstrumentKind.CFD, 0, 5)]
    assert EURUSD in TRADED
