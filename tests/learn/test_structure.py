"""Features, labels, learning and models compute. They hold no venue, so there is nothing they could send.

And three things about what they compute with: the future stays in the label package, floats stay in the one
module that reports statistics, and randomness is a hash of a seed, never a generator.
"""
from __future__ import annotations

import ast

from tests.support import called_name, imports, sources

PACKAGES = {
    "src/hftlab/features/": ["__init__.py", "builder.py", "spec.py"],
    "src/hftlab/labels/": ["__init__.py", "outcome.py"],
    "src/hftlab/learn/": [
        "__init__.py", "config.py", "dataset.py", "drift.py", "promote.py", "train.py", "validate.py",
    ],
    "src/hftlab/models/": ["__init__.py", "baseline.py", "file.py", "ridge.py", "scorer.py"],
}
ML_SLEEVE = "src/hftlab/strategy/ml.py"
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess"}
VENUE_CALLS = {"place", "cancel", "mark_reconciled", "connect", "disconnect", "subscribe_quotes", "poll", "feed"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
NOT_THEIR_BUSINESS = ("hftlab.shadow", "hftlab.exec", "hftlab.trader", "hftlab.risk", "hftlab.safety", "hftlab.ops",
                      "hftlab.publish", "hftlab.book", "hftlab.strategy")  # fmt: skip
FROM_THE_VENUES = {
    "hftlab.venues.port.ConnectionEvent",  # an event of the recorded stream
    "hftlab.venues.sim.cost.CostModel", "hftlab.venues.sim.cost.NO_FILL", "hftlab.venues.sim.cost.WIDENED",
    "hftlab.venues.sim.cost.CostModelError", "hftlab.venues.sim.cost.load_cost_model",  # the training run's file
}  # fmt: skip
STATISTICS = "src/hftlab/learn/validate.py"


def learning_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(tuple(PACKAGES))]
    for package, files in PACKAGES.items():
        assert [rel.removeprefix(package) for rel, _ in found if rel.startswith(package)] == files, package
    return found


def test_of_the_venues_they_import_an_event_type_and_the_cost_models_arithmetic_and_nothing_else():
    reached = set()
    for rel, tree in learning_sources():
        for how, name in imports(tree, rel):
            assert how in ("import", "from"), f"{rel}: an import by a computed or dynamic name"
            assert name.split(".")[0] not in WIRES, (rel, name)
            assert not name.startswith(NOT_THEIR_BUSINESS), (rel, name)
            if name.startswith("hftlab.venues"):
                reached.add(name)
    assert reached == FROM_THE_VENUES, "no venue class, no adapter, no fake broker, no guard, no quote source"


def test_nothing_in_them_calls_a_venue_or_reaches_one_by_name():
    for rel, tree in learning_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in VENUE_CALLS | BY_NAME, f"{rel} line {node.lineno}"
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert "Venue" not in name and name not in ("QuoteSource", "MarketDataView"), f"{rel}: {name}"


def test_the_future_is_known_to_the_label_package_and_to_nothing_a_decision_is_made_with():
    """A label is hindsight. What computes features, what scores them and what trades on the score cannot import one."""
    blind = [(rel, tree) for rel, tree in sources()
             if rel.startswith(("src/hftlab/features/", "src/hftlab/models/", "src/hftlab/strategy/"))]  # fmt: skip
    assert len(blind) == 3 + 5 + 4
    for rel, tree in blind:
        for _, name in imports(tree, rel):
            assert not name.startswith(("hftlab.labels", "hftlab.learn")), f"{rel} imports {name}"
    users = {rel for rel, tree in sources() for _, name in imports(tree, rel) if name.startswith("hftlab.labels")}
    assert users == {"src/hftlab/labels/__init__.py", "src/hftlab/learn/dataset.py"}, "the labeller and the one join"


def uses_a_float(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        named = isinstance(node, ast.Name) and node.id == "float"
        literal = isinstance(node, ast.Constant) and isinstance(node.value, float)
        if named or literal:
            lines.append(node.lineno)
    return lines


def test_floats_are_confined_to_the_statistics_of_one_module():
    found = {rel: uses_a_float(tree) for rel, tree in [*learning_sources(), (ML_SLEEVE, dict(sources())[ML_SLEEVE])]}
    assert {rel for rel, lines in found.items() if lines} == {STATISTICS}
    tree = dict(sources())[STATISTICS]
    floating = {"Sharpe", "Deflated", "sharpe", "_signed_root", "null_variance", "deflated_sharpe"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef) and uses_a_float(node):
            assert node.name in floating, f"{node.name} uses a float outside the Sharpe statistics"
    assert uses_a_float(tree) and uses_a_float(ast.parse("x = 0.5"))
    assert not uses_a_float(ast.parse("x = Fraction(1, 3) / 2"))
    reached = {name.split(".")[0] for rel, t in learning_sources() for _, name in imports(t, rel) if rel != STATISTICS}
    assert "statistics" not in reached and "decimal" not in reached


def test_randomness_is_a_hash_of_a_seed_and_never_a_generator():
    for rel, tree in [*learning_sources(), (ML_SLEEVE, dict(sources())[ML_SLEEVE])]:
        tops = {name.split(".")[0] for _, name in imports(tree, rel)}
        assert not tops & {"random", "secrets", "uuid", "numpy"}, rel
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in ("hash", "urandom", "shuffle", "sample"), f"{rel} line {node.lineno}"


def test_nothing_is_iterated_in_an_order_that_depends_on_a_hash():
    """Wherever a set is walked for a result, it is sorted first; otherwise a set is only counted or compared."""
    for rel, tree in learning_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.For | ast.comprehension):
                walked = node.iter
                assert not isinstance(walked, ast.Set | ast.SetComp), f"{rel} line {walked.lineno}: iterates a set"
                if isinstance(walked, ast.Call):
                    assert called_name(walked) not in ("set", "frozenset"), f"{rel} line {walked.lineno}"
