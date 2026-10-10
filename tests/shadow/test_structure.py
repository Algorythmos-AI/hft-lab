"""No broker order can be sent in shadow mode. Not a promise: the objects that could send one are not there."""
from __future__ import annotations

import ast
import inspect

from hftlab.shadow import Journal, Shadow
from hftlab.shadow import runner as runner_module
from hftlab.strategy import IdleSleeve
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.ibkr import feed
from hftlab.venues.sim import SimVenue
from hftlab.venues.view import MarketDataView, QuoteSource
from tests.shadow.kit import TRADED
from tests.support import called_name, imports, sources
from tests.venues.kit import plain_costs

ORDER_METHODS = {"place", "cancel", "mark_reconciled"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess"}
SHADOW = "src/hftlab/shadow/"


def shadow_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(SHADOW)]
    assert [rel.removeprefix(SHADOW) for rel, _ in found] == ["__init__.py", "__main__.py", "journal.py", "runner.py"]
    return found


def test_the_only_thing_orders_are_placed_on_is_a_shadow_venue_the_runner_made_itself(tmp_path):
    shadow = Shadow(TRADED, plain_costs(), [IdleSleeve()], Journal(tmp_path / "j.jsonl"))
    assert type(shadow._venue) is SimVenue and not isinstance(shadow._venue, GuardedVenue)
    assert shadow._venue.name == "sim" and shadow._venue.managed_accounts() == (), "no broker, no account"
    shadow.close()

    made = []
    for rel, tree in shadow_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
                continue
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                names = [n.attr for n in ast.walk(target) if isinstance(n, ast.Attribute)]
                if "_venue" in names:
                    made.append((rel, ast.unparse(node.value).split("(")[0] if node.value else ""))
    assert made == [("src/hftlab/shadow/runner.py", "SimVenue")], "assigned once, from the SimVenue constructor"


def test_every_order_call_in_the_package_is_on_that_venue():
    calls = []
    for rel, tree in shadow_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and called_name(node) in ORDER_METHODS:
                calls.append((rel.removeprefix(SHADOW), ast.unparse(node.func)))
    assert sorted(calls) == [
        ("runner.py", "self._venue.cancel"),
        ("runner.py", "self._venue.mark_reconciled"),
        ("runner.py", "self._venue.place"),
    ]
    for _, tree in shadow_sources():  # and nobody reaches one by name
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and called_name(node) in BY_NAME:
                raise AssertionError(f"line {node.lineno}: a lookup by name in the shadow package")


def test_there_is_no_way_to_hand_the_runner_a_venue():
    parameters = inspect.signature(Shadow.__init__).parameters
    assert list(parameters) == ["self", "instruments", "model", "sleeves", "journal", "calendar", "seed", "source"]
    assert parameters["source"].annotation == "str", "a label for the journal, not an object"
    for name in ("Venue", "GuardedVenue", "FakeBroker", "IbkrVenue"):
        assert not hasattr(runner_module, name), f"the runner module does not even import {name}"
    live = inspect.signature(runner_module.live_events).parameters["source"].annotation
    assert live == "QuoteSource" and inspect.signature(runner_module.open_live).parameters["source"].annotation == live


def test_the_live_quote_source_has_no_order_methods_at_all():
    public = {name for name in dir(QuoteSource) if not name.startswith("_")}
    assert public == {
        "name", "state", "connect", "disconnect", "poll", "instrument", "subscribe_quotes", "unsubscribe_quotes",
        "server_time_ns",
    }  # fmt: skip
    assert not ORDER_METHODS & set(dir(MarketDataView)) and not ORDER_METHODS & public
    assert inspect.signature(feed.market_data).return_annotation == "MarketDataView"


def test_the_shadow_package_imports_one_thing_from_the_broker_side_the_order_free_feed():
    reached = set()
    for rel, tree in shadow_sources():
        for _, name in imports(tree, rel):
            if name.startswith("hftlab.venues"):
                reached.add((rel.removeprefix(SHADOW), name))
            assert name.split(".")[0] not in WIRES, (rel, name)
    harmless = ("hftlab.venues.port", "hftlab.venues.sim")
    assert {(rel, name) for rel, name in reached if not name.startswith(harmless)} == {
        ("__main__.py", "hftlab.venues.ibkr.feed.market_data"),
        ("runner.py", "hftlab.venues.view.QuoteSource"),
    }
