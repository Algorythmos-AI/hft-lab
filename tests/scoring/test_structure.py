"""The scorer's child holds no venue and reaches no network. Not a promise: a scan of what it imports, and a look.

Three layers, in the spirit of the shadow runner's structure test. The source of the package is scanned for
what it imports and calls. The modules the child imports are followed, by their own imports, to everything
they can reach. And a real child is started and asked what it has loaded.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys

from tests.learn.test_structure import uses_a_float
from tests.safety.test_isolation import is_network, violations
from tests.support import ROOT, called_name, imports, sources

PACKAGE = "src/hftlab/scoring/"
FILES = ["__init__.py", "child.py", "client.py", "record.py", "wire.py"]
CHILD = ("src/hftlab/scoring/child.py", "src/hftlab/scoring/wire.py")
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "select", "selectors", "multiprocessing"}
VENUE_CALLS = {"place", "cancel", "mark_reconciled", "connect", "disconnect", "subscribe_quotes", "poll", "feed"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
NOT_BYTES_OF_OURS = {"pickle", "marshal", "shelve", "dill", "copyreg"}  # nothing that arrives is ever run
PROCESSES = {"subprocess", "threading", "queue"}
# What a child may load of the platform: the model file, the scorers, and what those are written with.
CHILD_MAY_LOAD = {
    "hftlab", "hftlab.journal", "hftlab.scoring", "hftlab.scoring.child", "hftlab.scoring.wire",
    "hftlab.models", "hftlab.models.baseline", "hftlab.models.file", "hftlab.models.ridge", "hftlab.models.scorer",
    "hftlab.models.trees", "hftlab.features", "hftlab.features.builder", "hftlab.features.spec",
    "hftlab.calendar", "hftlab.calendar.events", "hftlab.calendar.week", "hftlab.record", "hftlab.record.format",
    "hftlab.core", "hftlab.core._checks", "hftlab.core.clock", "hftlab.core.enums", "hftlab.core.ids",
    "hftlab.core.instrument", "hftlab.core.market", "hftlab.core.money", "hftlab.core.orders",
    "hftlab.venues", "hftlab.venues.port",  # the port's event types, which a recorded stream is made of
}  # fmt: skip


def scoring_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(PACKAGE)]
    assert [rel.removeprefix(PACKAGE) for rel, _ in found] == FILES
    return found


def test_the_package_imports_no_network_and_only_the_traders_side_starts_a_process():
    starts = set()
    for rel, tree in scoring_sources():
        assert violations(rel, tree) == [], "and it gets no exemption from the isolation scan"
        for how, name in imports(tree, rel):
            top = name.split(".")[0]
            assert how in ("import", "from"), f"{rel}: an import by a computed or dynamic name"
            assert top not in WIRES and not is_network(name), (rel, name)
            assert top not in NOT_BYTES_OF_OURS, f"{rel} imports {name}: a message is text, and is never run"
            assert not name.startswith("hftlab.venues"), f"{rel} imports {name}: a scorer sees rows, not venues"
            if top in PROCESSES:
                starts.add((rel.removeprefix(PACKAGE), top))
    assert starts == {("client.py", "subprocess"), ("client.py", "threading"), ("client.py", "queue")}
    users = {rel for rel, tree in sources() for _, name in imports(tree, rel) if name.split(".")[0] == "subprocess"}
    assert users == {PACKAGE + "client.py", "src/hftlab/ops/backup.py", "src/hftlab/registry/records.py"}


def test_nothing_in_the_package_calls_a_venue_reaches_one_by_name_or_holds_a_float():
    for rel, tree in scoring_sources():
        assert uses_a_float(tree) == [], f"{rel}: whole numbers on the wire and in the code that reads them"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in VENUE_CALLS | BY_NAME, f"{rel} line {node.lineno}"
                assert called_name(node) not in ("loads", "load") or ast.unparse(node.func) in (
                    "json.loads", "wire.load", "load",
                ), f"{rel} line {node.lineno}: something is read that is not JSON"  # fmt: skip
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert "Venue" not in name and name not in ("QuoteSource", "MarketDataView"), f"{rel}: {name}"


def test_the_child_imports_the_model_file_the_scorers_and_the_wire_format_and_nothing_else_of_the_platform():
    trees = dict(scoring_sources())
    assert trees[PACKAGE + "__init__.py"].body[1:] == [], "the package imports nothing: the child loads what it names"
    reached = {name for rel in CHILD for _, name in imports(trees[rel], rel) if name.startswith("hftlab")}
    assert reached == {
        "hftlab.journal.canonical", "hftlab.scoring.wire",
        "hftlab.models.file.ModelError", "hftlab.models.file.parse",
        "hftlab.models.scorer.ScoreError", "hftlab.models.scorer.Scorer", "hftlab.models.scorer.answer",
        "hftlab.models.scorer.scorer_for",
    }  # fmt: skip
    every = {name for rel in CHILD for _, name in imports(trees[rel], rel)}
    plain = {name.split(".")[0] for name in every if not name.startswith("hftlab")}
    assert plain == {"__future__", "argparse", "collections", "json", "pathlib", "re", "sys", "typing"}


def modules_of_the_platform() -> dict[str, ast.Module]:
    out = {}
    for rel, tree in sources():
        name = rel.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        out[name] = tree
    return out


def test_followed_through_every_import_the_child_can_reach_no_adapter_no_order_path_and_no_socket():
    known = modules_of_the_platform()
    rels = {name: rel for name, rel in zip(known, [rel for rel, _ in sources()], strict=True)}
    seen, waiting, outside = set(), ["hftlab.scoring.child"], set()
    while waiting:
        module = waiting.pop()
        if module in seen:
            continue
        seen.add(module)
        parts = module.split(".")
        waiting += [".".join(parts[:n]) for n in range(1, len(parts))]  # importing a module imports its packages
        for _, name in imports(known[module], rels[module]):
            target = name if name in known else name.rsplit(".", 1)[0]
            if target in known:
                waiting.append(target)
            elif not name.startswith("hftlab"):
                outside.add(name.split(".")[0])
    assert seen == CHILD_MAY_LOAD
    assert not outside & (WIRES | PROCESSES | NOT_BYTES_OF_OURS | {"ib_insync", "ibapi", "requests", "httpx"})
    for banned in ("hftlab.venues.ibkr", "hftlab.venues.fake", "hftlab.venues.guard", "hftlab.venues.sim",
                   "hftlab.exec", "hftlab.risk", "hftlab.trader", "hftlab.shadow", "hftlab.ops", "hftlab.publish",
                   "hftlab.safety", "hftlab.learn", "hftlab.labels", "hftlab.strategy", "hftlab.book"):  # fmt: skip
        assert not [name for name in seen if name.startswith(banned)], banned


def test_a_started_child_has_loaded_what_the_scan_says_and_nothing_that_opens_a_socket():
    script = "import hftlab.scoring.child, json, sys; print(json.dumps(sorted(sys.modules)))"
    environment = {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=True)
    loaded = set(json.loads(done.stdout))
    assert {name for name in loaded if name.split(".")[0] == "hftlab"} == CHILD_MAY_LOAD
    tops = {name.split(".")[0] for name in loaded}
    assert not tops & {"socket", "_socket", "ssl", "_ssl", "asyncio", "http", "select", "selectors", "subprocess",
                       "multiprocessing", "pickle", "_pickle", "ib_async", "numpy", "lightgbm"}  # fmt: skip
    assert not [name for name in loaded if is_network(name)]
