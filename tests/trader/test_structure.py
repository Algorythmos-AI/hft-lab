"""Where an order can go from the trader: to the order manager, or to the shadow venue it built. Nowhere else.

In the spirit of the shadow runner's structure test: not a promise, a scan. The trader holds a broker venue only
to hand it to the order manager; the calls that send are the manager's (tests/venues/test_guard.py pins every
one in the package by its exact location, the trader's shadow venue among them).
"""
from __future__ import annotations

import ast
import inspect

from hftlab.exec import OrderManager
from hftlab.risk import RiskGate
from hftlab.trader import LiveEdge, session, tape
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.sim import SimVenue
from tests.support import called_name, imports, sources
from tests.trader.kit import Desk
from tests.venues.kit import T0

ORDER_METHODS = {"place", "cancel", "mark_reconciled"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess"}
TRADER = "src/hftlab/trader/"


def trader_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(TRADER)]
    assert [rel.removeprefix(TRADER) for rel, _ in found] == [
        "__init__.py", "__main__.py", "config.py", "replay.py", "resume.py", "session.py", "slippage.py", "stages.py",
        "tape.py",
    ]  # fmt: skip
    return found


def test_every_order_call_in_the_package_is_on_the_shadow_venue_the_session_built_itself():
    calls = []
    for rel, tree in trader_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and called_name(node) in ORDER_METHODS:
                calls.append((rel.removeprefix(TRADER), ast.unparse(node.func)))
            if isinstance(node, ast.Call) and called_name(node) in BY_NAME:
                raise AssertionError(f"{rel} line {node.lineno}: a lookup by name in the trader package")
    assert sorted(calls) == [
        ("session.py", "self._shadow.cancel"),
        ("session.py", "self._shadow.mark_reconciled"),
        ("session.py", "self._shadow.place"),
    ]


def test_the_shadow_venue_is_assigned_once_from_its_constructor_and_is_no_broker(tmp_path):
    made = []
    for rel, tree in trader_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = [n.attr for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Attribute)]
                if "_shadow" in names:
                    made.append((rel.removeprefix(TRADER), ast.unparse(node.value).split("(")[0]))
    assert made == [("session.py", "SimVenue")]
    desk = Desk(tmp_path)
    shadow = desk.trader._shadow
    assert type(shadow) is SimVenue and not isinstance(shadow, GuardedVenue) and shadow.managed_accounts() == ()
    assert shadow is not desk.broker and isinstance(desk.broker, GuardedVenue)


def test_the_broker_venue_is_only_ever_handed_to_the_order_manager(tmp_path):
    """The session never binds the venue to a name of its own: it passes `edge.venue` straight to the manager."""
    tree = dict(trader_sources())["src/hftlab/trader/session.py"]
    uses = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "venue"]
    assert set(uses) <= {"edge.venue", "opened.venue"}, uses
    handed = [
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and called_name(node) == "OrderManager"
    ]
    assert handed == ["edge.venue"]
    desk = Desk(tmp_path)
    assert desk.trader.manager._venue is desk.broker and isinstance(desk.trader.manager, OrderManager)
    assert isinstance(desk.trader.gate, RiskGate) and desk.trader.manager._gate is desk.trader.gate


def test_the_edge_that_runs_on_a_venue_takes_a_guarded_one_and_nothing_shaped_like_it(tmp_path):
    desk = Desk(tmp_path, start=False)
    import pytest

    from hftlab.core import SimClock

    with pytest.raises(TypeError, match="guarded venue"):
        LiveEdge(
            SimVenue(SimClock(T0), [], desk.costs), SimClock(T0), pairs=(), switches=frozenset,
            requests=tmp_path, should_stop=lambda: True, venue=SimVenue(SimClock(T0), [], desk.costs),
        )  # fmt: skip
    annotation = inspect.signature(LiveEdge.__init__).parameters["venue"].annotation
    assert annotation == "Venue | None"


def test_the_tape_module_sends_nothing_its_replay_venue_only_answers():
    tree = dict(trader_sources())["src/hftlab/trader/tape.py"]
    assert [called_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call) and called_name(n) in ORDER_METHODS] == []
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"place", "cancel"} <= defined, "the replay venue is the manager's venue in a replay"
    assert not hasattr(tape.LiveEdge, "place") and not hasattr(tape.LiveEdge, "cancel")
    assert not hasattr(tape._Recorded, "place") and not hasattr(session._Listening, "place")


def test_the_package_imports_no_network_and_reaches_the_broker_side_only_to_build_the_two_paper_venues():
    reached = set()
    for rel, tree in trader_sources():
        for _, name in imports(tree, rel):
            assert name.split(".")[0] not in WIRES, (rel, name)
            if name.startswith(("hftlab.venues.ibkr", "hftlab.venues.fake")):
                reached.add((rel.removeprefix(TRADER), name))
    assert reached == {
        ("__main__.py", "hftlab.venues.ibkr.paper.paper_venue"),
        ("__main__.py", "hftlab.venues.ibkr.feed.market_data"),
        ("__main__.py", "hftlab.venues.fake.FakeConfig"),
        ("__main__.py", "hftlab.venues.fake.build"),
    }
