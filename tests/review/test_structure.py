"""The review commands read and compute. Neither holds a venue, and the one that runs a sleeve does it in shadow."""
from __future__ import annotations

import ast

from tests.safety.test_isolation import violations
from tests.support import called_name, imports, sources

PACKAGE = "src/hftlab/review/"
FILES = ["__init__.py", "dc_null.py", "trip_costs.py", "trips.py"]
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess"}
VENUE_CALLS = {"place", "cancel", "mark_reconciled", "connect", "disconnect", "subscribe_quotes", "poll", "feed"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
NOT_ITS_BUSINESS = ("hftlab.exec", "hftlab.trader", "hftlab.risk", "hftlab.safety", "hftlab.ops", "hftlab.publish",
                    "hftlab.learn", "hftlab.labels", "hftlab.models", "hftlab.scoring", "hftlab.lineup")  # fmt: skip


def review_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(PACKAGE)]
    assert [rel.removeprefix(PACKAGE) for rel, _ in found] == FILES
    return found


def test_of_the_venues_the_package_imports_the_cost_models_arithmetic_and_nothing_else():
    reached = set()
    for rel, tree in review_sources():
        assert violations(rel, tree) == []
        for how, name in imports(tree, rel):
            assert how in ("import", "from"), f"{rel}: an import by a computed or dynamic name"
            assert name.split(".")[0] not in WIRES, (rel, name)
            assert not name.startswith(NOT_ITS_BUSINESS), (rel, name)
            if name.startswith("hftlab.venues"):
                reached.add(name)
    assert reached == {
        "hftlab.venues.sim.cost.CostModel", "hftlab.venues.sim.cost.CostModelError", "hftlab.venues.sim.cost.WIDENED",
        "hftlab.venues.sim.cost.load_cost_model",
    }, "no venue class, no port, no quote source"  # fmt: skip


def test_nothing_in_the_package_calls_a_venue_or_reaches_one_by_name():
    for rel, tree in review_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in VENUE_CALLS | BY_NAME, f"{rel} line {node.lineno}"
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert "Venue" not in name and name not in ("QuoteSource", "MarketDataView"), f"{rel}: {name}"


def test_only_the_null_command_runs_a_sleeve_and_it_runs_it_in_the_shadow_runner():
    """The comparison of round trips reads a journal and a recording and nothing else: no runner, no sleeve, no file
    of settings. The null command builds its sleeves and its session the way `python -m hftlab.shadow` does."""
    by_file = {rel.removeprefix(PACKAGE): {name for _, name in imports(tree, rel)} for rel, tree in review_sources()}
    runs = ("hftlab.shadow", "hftlab.strategy")
    assert {name for name in by_file["dc_null.py"] if name.startswith(runs)} == {
        "hftlab.shadow.Journal", "hftlab.shadow.Shadow", "hftlab.shadow.ShadowError", "hftlab.shadow.run",
        "hftlab.shadow.__main__.DIRECTIONAL_CHANGE", "hftlab.shadow.__main__.ConfigError",
        "hftlab.shadow.__main__.load_config", "hftlab.shadow.__main__.make_sleeves",
        "hftlab.shadow.__main__.minimum_sizes", "hftlab.strategy.dc.DcSleeve", "hftlab.strategy.dc.null",
    }  # fmt: skip
    for quiet in ("trips.py", "trip_costs.py", "__init__.py"):
        assert not [name for name in by_file[quiet] if name.startswith(runs)], quiet
        assert not {"yaml", "tempfile"} & {name.split(".")[0] for name in by_file[quiet]}, quiet
    assert "hftlab.venues.sim.cost.load_cost_model" not in by_file["trip_costs.py"], "its cost model is the journal's"


def test_the_sleeves_still_cannot_import_what_runs_them_or_what_reads_them_back():
    for rel, tree in sources():
        if rel.startswith("src/hftlab/strategy/"):
            for _, name in imports(tree, rel):
                assert not name.startswith(("hftlab.shadow", "hftlab.review", "hftlab.replay")), (rel, name)
