"""Features, labels, learning and models compute. They hold no venue, so there is nothing they could send.

And three things about what they compute with: the future stays in the label package, floats stay in the module
that reports statistics and the two modules that fit in floats, and randomness is a hash of a seed, never a
generator. And two about what the fits import: one function of one module imports the optional ML set (ADR 0025),
and the array library is imported by the two fitting modules alone, inside their functions (ADR 0029).
"""
from __future__ import annotations

import ast

from tests.support import called_name, imports, sources

PACKAGES = {
    "src/hftlab/features/": ["__init__.py", "builder.py", "spec.py", "window.py"],
    "src/hftlab/labels/": ["__init__.py", "outcome.py"],
    "src/hftlab/learn/": [
        "__init__.py", "boost.py", "config.py", "dataset.py", "drift.py", "promote.py", "sequence.py", "train.py",
        "validate.py",
    ],
    "src/hftlab/models/": ["__init__.py", "baseline.py", "file.py", "net.py", "ridge.py", "scorer.py", "trees.py"],
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
BOOST = "src/hftlab/learn/boost.py"  # the one module that fits with a library, in the library's floats
ML_SET = {"lightgbm", "scipy", "narwhals"}  # what requirements-ml.in adds to an environment
SEQUENCE = "src/hftlab/learn/sequence.py"  # the one module that fits a network, in floats of its own
FITS = {BOOST, SEQUENCE}
ARRAYS = "numpy"  # what the two fits compute with. It comes with the broker's client library, and nothing else uses it
# Everything of the array library the network's fit may touch: arrays, and arithmetic element by element.
ELEMENTWISE = {"array", "zeros", "zeros_like", "float64", "concatenate", "stack", "minimum", "maximum", "sqrt",
               "__version__"}  # fmt: skip


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
    assert len(blind) == 4 + 7 + 8
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


def test_floats_are_confined_to_the_statistics_of_one_module_and_the_two_modules_that_fit():
    """What scores a row (`models/`), what builds one and what decides a promotion hold no float at all."""
    found = {rel: uses_a_float(tree) for rel, tree in [*learning_sources(), (ML_SLEEVE, dict(sources())[ML_SLEEVE])]}
    assert {rel for rel, lines in found.items() if lines} == {STATISTICS, *FITS}
    assert not any(rel.startswith("src/hftlab/models/") for rel, lines in found.items() if lines)
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
        assert not tops & {"random", "secrets", "uuid"}, rel
        assert ARRAYS not in tops or rel in FITS, f"{rel}: arrays are for the two fits and for nothing else"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in ("hash", "urandom", "shuffle", "sample"), f"{rel} line {node.lineno}"
            if rel in FITS and isinstance(node, ast.Attribute):
                assert node.attr != "random", f"{rel} line {node.lineno}: the array library's generator"


def test_the_optional_ml_set_is_imported_by_one_module_inside_a_function_and_by_nothing_else():
    """Importing any module of the platform, the two that fit among them, needs neither the set nor the arrays."""
    users, at_the_top = {}, []
    for rel, tree in sources():
        heavy = {name.split(".")[0] for _, name in imports(tree, rel)} & (ML_SET | {ARRAYS})
        if heavy:
            users[rel] = heavy
        if rel in FITS:
            at_the_top += [name.split(".")[0] for node in tree.body for _, name in imports(node, rel)
                           if isinstance(node, ast.Import | ast.ImportFrom)]  # fmt: skip
    assert users == {BOOST: {"lightgbm", ARRAYS}, SEQUENCE: {ARRAYS}}, "the network's fit adds no library at all"
    assert not {*at_the_top} & (ML_SET | {ARRAYS}), "imported where it is used, not on import"
    blind = [rel for rel, tree in sources() if rel.startswith("src/hftlab/models/")
             for _, name in imports(tree, rel) if name.startswith("hftlab.learn")]  # fmt: skip
    assert blind == [], "what scores a model cannot reach what fits one"


def test_the_networks_fit_is_arithmetic_element_by_element_and_leaves_no_sum_to_a_library():
    """What makes two fits the same file on any machine (ADR 0029): nothing whose order or rounding a build chooses.

    No matrix product, no library sum or mean, no exponential, logarithm or power of a float: only what
    `ELEMENTWISE` lists, the four operations, a square root and comparisons.
    """
    tree = dict(sources())[SEQUENCE]
    library = {node.targets[0].id for node in ast.walk(tree) if isinstance(node, ast.Assign)
               and isinstance(node.value, ast.Call) and called_name(node.value) == "library"}  # fmt: skip
    assert library == {"numpy"}, "the array library goes by one name in the module"
    touched = set()
    for node in ast.walk(tree):
        assert not isinstance(node, ast.MatMult), f"line {node.lineno}: a matrix product"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "numpy":
            touched.add(node.attr)
        if isinstance(node, ast.Call):
            assert called_name(node) not in {
                "dot", "matmul", "einsum", "tensordot", "inner", "sum", "mean", "prod", "cumsum", "exp", "log", "tanh",
                "power", "pow", "std", "var", "average", "reduce", "accumulate",
            } or ast.unparse(node.func) == "sum", f"line {node.lineno}: {ast.unparse(node.func)}"  # fmt: skip
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            assert ast.unparse(node.left) == "2", f"line {node.lineno}: a power that is not a whole power of two"
    assert touched <= ELEMENTWISE and {"minimum", "maximum", "sqrt", "array"} <= touched, sorted(touched - ELEMENTWISE)
    methods = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute)}  # fmt: skip
    assert not methods & {"sum", "mean", "dot", "std", "var", "prod", "cumsum"}, "nor as a method of an array"


def test_nothing_is_iterated_in_an_order_that_depends_on_a_hash():
    """Wherever a set is walked for a result, it is sorted first; otherwise a set is only counted or compared."""
    for rel, tree in learning_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.For | ast.comprehension):
                walked = node.iter
                assert not isinstance(walked, ast.Set | ast.SetComp), f"{rel} line {walked.lineno}: iterates a set"
                if isinstance(walked, ast.Call):
                    assert called_name(walked) not in ("set", "frozenset"), f"{rel} line {walked.lineno}"
