"""The feasibility map reads recordings and computes. It holds no venue, so there is nothing it could send."""
from __future__ import annotations

import ast

from tests.support import called_name, imports, sources

PACKAGE = "src/hftlab/feasibility/"
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess"}
VENUE_CALLS = {"place", "cancel", "mark_reconciled", "connect", "subscribe_quotes", "poll", "feed"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
NOT_ITS_BUSINESS = ("hftlab.shadow", "hftlab.strategy", "hftlab.book", "hftlab.safety", "hftlab.registry")


def feasibility_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(PACKAGE)]
    assert [rel.removeprefix(PACKAGE) for rel, _ in found] == ["__init__.py", "__main__.py", "map.py"]
    return found


def test_of_the_venues_the_package_imports_the_cost_models_arithmetic_and_nothing_else():
    reached = set()
    for rel, tree in feasibility_sources():
        for _, name in imports(tree, rel):
            assert name.split(".")[0] not in WIRES, (rel, name)
            assert not name.startswith(NOT_ITS_BUSINESS), (rel, name)
            if name.startswith("hftlab.venues"):
                reached.add(name)
    assert reached == {
        "hftlab.venues.sim.cost.CostModel", "hftlab.venues.sim.cost.CostModelError",
        "hftlab.venues.sim.cost.load_cost_model", "hftlab.venues.sim.cost.NO_FILL", "hftlab.venues.sim.cost.WIDENED",
    }, "no venue class, no port, no quote source: a cost model is a set of numbers"  # fmt: skip


def test_nothing_in_the_package_calls_a_venue_or_reaches_one_by_name():
    for rel, tree in feasibility_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in VENUE_CALLS | BY_NAME, f"{rel} line {node.lineno}"
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert "Venue" not in name and name not in ("QuoteSource", "MarketDataView"), f"{rel}: {name}"
