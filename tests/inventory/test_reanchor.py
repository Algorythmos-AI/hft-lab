"""The way out of a break in cash: an operator asks for a new anchor (ADR 0031, note of 2026-10-13).

When it is taken, when it is not, and what the float's journal holds afterwards. Fake broker, made-up balances.
"""
from __future__ import annotations

from hftlab.book import FLOAT, effect
from tests.inventory.kit import UNIT, Bureau, account, limits, put_stage
from tests.venues.kit import SECOND, T0


def requests(desk: Bureau) -> list[tuple[str, int, str]]:
    return [(str(e["what"]), int(e["done"]), str(e["refused"])) for e in desk.of("request")]


def funded(folder, config=None, *, stage: str | None = "min_size", risk=None) -> Bureau:
    if stage:
        put_stage(folder, "control", stage)
    desk = Bureau(folder, config, risk=risk)
    desk.fund()
    return desk


def broken(folder, *, stage: str | None = "min_size") -> Bureau:
    """A float that stands, and then cash that no fill explains: a break, after its grace time."""
    desk = funded(folder, stage=stage)
    desk.market.post("USD", -40 * UNIT)
    for _ in range(6):
        desk.run(30)
        if desk.of("break"):
            break
    assert [e["broken"] for e in desk.of("break")] == [1]
    return desk


def test_a_new_anchor_ends_a_break_in_cash_and_says_what_it_explained_away(tmp_path):
    desk = broken(tmp_path)
    assert desk.trader.stages.stage("control") == "stepped_down"
    desk.run(10)  # the sleeve's exits go on in a break; by now nothing is on its way
    desk.ask("anchor", "float", operator="someone")
    desk.run(3)
    assert requests(desk)[-1] == ("anchor", 1, "")
    entry = desk.ledger()[-1]
    assert (entry["type"], entry["operator"], entry["why"]) == ("anchor", "someone", "operator")
    assert entry["absorbed"] == {"USD": -40 * UNIT}
    book = desk.trader.broker_book
    moved, seen = effect(book), desk.market.cash()
    assert entry["cash"] == {c: seen.get(c, 0) - moved.get(c, 0) for c in sorted(set(seen) | set(moved))}
    floats = effect(book, FLOAT)
    assert entry["baseline"] == {c: entry["cash"].get(c, 0) + floats.get(c, 0) for c in entry["cash"]}
    assert desk.trader.float_status()["baseline"] == entry["baseline"]
    assert [e["broken"] for e in desk.of("break")] == [1, 0], "the comparison made at once agreed"
    assert [e["type"] for e in desk.reconciled()][-1] == "break_cleared"
    assert "reconciliation_break" in desk.pages.resolved
    assert desk.trader.gate.cash() == seen, "and the risk gate counts from the new anchor"
    assert desk.trader.stages.stage("control") == "stepped_down", "a stage comes back by its own rule, not by this"
    desk.run(90)
    assert [e["broken"] for e in desk.of("break")] == [1, 0], "it stays agreed"


def test_a_new_anchor_restates_the_position_a_sleeve_holds_from_the_book(tmp_path):
    desk = funded(tmp_path, risk=limits(None, settle_seconds=1))
    lines: list[list[object]] = []
    for _ in range(200):
        desk.run(1)
        held = {key: q for key, q in desk.trader.broker_book.open_lines().items() if key[0] != FLOAT}
        if not held:
            continue
        desk.ask("anchor", "float")
        desk.run(1)
        last = desk.ledger()[-1]
        if requests(desk)[-1] == ("anchor", 1, "") and last["restated"]:
            lines = last["restated"]
            break
    assert lines and all(name == "control" and quantity for name, _, quantity in lines)
    assert desk.ledger()[-1]["absorbed"] == {}, "nothing was off: the book was the account's"
    refused = {said for what, done, said in requests(desk) if what == "anchor" and not done}
    assert refused <= {"NOT_SETTLED"}, "while an order is on its way or a fill is fresh: ask again in a moment"
    desk.run(60)
    assert not desk.of("break") and desk.reconciled() == []


def test_no_anchor_is_taken_on_a_connection_that_is_not_safe_or_just_after_a_fill(tmp_path):
    down = T0 + 45 * SECOND
    desk = funded(tmp_path, account(outages=((down, down + 20 * SECOND),)), stage=None)
    entries = len(desk.ledger())
    desk.run(15)
    assert desk.clock.now_ns() > down and desk.trader.manager.blind
    desk.ask("anchor", "float")
    desk.ask("unfund", "float")
    desk.run(3)
    assert requests(desk)[-2:] == [("anchor", 0, "CONNECTION_NOT_SAFE"), ("unfund", 0, "CONNECTION_NOT_SAFE")]
    assert len(desk.ledger()) == entries, "nothing was written"
    desk.run(30)
    assert not desk.trader.manager.blind
    desk.ask("anchor", "float")
    desk.run(3)
    assert requests(desk)[-1] == ("anchor", 1, ""), "and on the connection made safe it is taken, break or none"
    assert desk.ledger()[-1]["absorbed"] == {} and desk.ledger()[-1]["restated"] == []


def test_a_new_anchor_is_taken_while_a_conversion_stands_still_and_it_is_kept_across_a_restart(tmp_path):
    desk = broken(tmp_path, stage=None)
    desk.ask("unfund", "float")
    desk.run(2)
    assert requests(desk)[-1] == ("unfund", 1, "")
    desk.ask("anchor", "float")
    for _ in range(30):
        desk.run(2)
        if requests(desk)[-1][0] == "anchor" and requests(desk)[-1][1]:
            break
        if requests(desk)[-1] == ("anchor", 0, "NOT_SETTLED"):
            desk.ask("anchor", "float")
    assert requests(desk)[-1] == ("anchor", 1, ""), "a conversion in hand does not lock the way out"
    desk.run(60)
    assert [e["broken"] for e in desk.of("break")] == [1, 0]
    desk.restart()
    desk.run(70)
    assert desk.trader.gate.cash() == desk.market.cash(), "the ledger was read again: anchor plus fills is the cash"
    assert [e["broken"] for e in desk.of("break")] == [1, 0], "and nothing differs on the new connection"
