"""The risk gate is independent: it imports no venue adapter and no sleeve, and it holds nothing it could send on."""
from __future__ import annotations

import ast
import subprocess
import sys

from tests.support import ROOT, called_name, imports, sources

PACKAGE = "src/hftlab/risk/"
WIRES = {"ib_async", "socket", "ssl", "asyncio", "http", "urllib", "subprocess"}
VENUE_CALLS = {"place", "cancel", "mark_reconciled", "connect", "disconnect", "poll", "subscribe_quotes", "feed"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
# Everything of the platform the gate may see: types, the book it rebuilds, the calendar, the journal it keeps
# its latches in, and the port's event types. No adapter, no guard, no sleeve, no runner and no order manager.
ALLOWED = ("hftlab.core", "hftlab.book", "hftlab.calendar", "hftlab.journal", "hftlab.risk", "hftlab.venues.port")
NEVER_LOADED = (
    "hftlab.strategy", "hftlab.venues.ibkr", "hftlab.venues.fake", "hftlab.venues.sim", "hftlab.venues.guard",
    "hftlab.venues.view", "hftlab.exec", "hftlab.shadow", "hftlab.record", "hftlab.replay", "ib_async",
)  # fmt: skip


def risk_sources():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(PACKAGE)]
    assert [rel.removeprefix(PACKAGE) for rel, _ in found] == [
        "__init__.py", "config.py", "gate.py", "room.py", "switches.py", "verdict.py",
    ]  # fmt: skip
    return found


def test_the_package_imports_no_venue_adapter_no_sleeve_and_nothing_that_sends():
    seen = set()
    for rel, tree in risk_sources():
        for how, name in imports(tree, rel):
            assert how != "computed" and name.split(".")[0] not in WIRES, (rel, name)
            if name.startswith("hftlab"):
                assert name.startswith(ALLOWED), f"{rel} imports {name}"
                seen.add(name)
    from_the_port = {name.rsplit(".", 1)[-1] for name in seen if name.startswith("hftlab.venues")}
    assert from_the_port == {"CommissionEvent", "ConnectionEvent", "ConnectionState"}, "events, never a venue"


def test_importing_the_gate_loads_no_adapter_no_sleeve_and_no_order_manager():
    script = (
        "import sys; import hftlab.risk; "
        f"print(sorted(m for m in sys.modules if m.startswith({NEVER_LOADED!r})))"
    )
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT / "src", capture_output=True, text=True, check=True
    )
    assert run.stdout.strip() == "[]", run.stdout


def test_nothing_in_the_package_calls_a_venue_or_reaches_anything_by_name():
    for rel, tree in risk_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in VENUE_CALLS | BY_NAME, f"{rel} line {node.lineno}"
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert "Venue" not in name and name not in ("QuoteSource", "MarketDataView"), f"{rel}: {name}"


def test_the_only_file_the_gate_touches_itself_is_its_journal_and_the_switches_come_from_outside():
    for rel, tree in risk_sources():
        opens = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and called_name(n) == "open"]
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        reads = [n.lineno for n in calls if called_name(n) in ("read_text", "exists")]
        if rel.endswith("gate.py"):
            assert len(opens) == 1 and reads == [], "journal.open(), once, when the gate is built"
        elif rel.endswith("switches.py"):
            assert opens == [] and len(reads) == 1, "one existence check per switch"
        elif rel.endswith("config.py"):
            assert opens == [] and len(reads) == 1, "the risk file, read once by the loader"
        else:
            assert opens == [] and reads == []
