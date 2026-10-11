"""The host layer holds no venue and no order path: it reads files, renders text and sends one kind of request.

In the spirit of the shadow and feasibility structure tests: not a promise that it will not trade, but a scan
showing the objects that could are not there.
"""
from __future__ import annotations

import ast

from tests.support import called_name, imports, sources

PACKAGES = ("src/hftlab/ops/", "src/hftlab/publish/")
ORDER_CALLS = {"place", "cancel", "mark_reconciled", "connect", "subscribe_quotes", "poll", "feed"}
BY_NAME = {"getattr", "setattr", "__import__", "import_module"}
NOT_ITS_BUSINESS = (
    "hftlab.venues", "hftlab.safety", "hftlab.strategy", "hftlab.book", "hftlab.risk", "hftlab.exec", "hftlab.replay",
    "hftlab.feasibility", "hftlab.registry",
)  # fmt: skip


def layer() -> list[tuple[str, ast.Module]]:
    found = [(rel, tree) for rel, tree in sources() if rel.startswith(PACKAGES)]
    assert [rel.removeprefix("src/hftlab/") for rel, _ in found] == [
        "ops/__init__.py", "ops/alerts.py", "ops/backup.py", "ops/deploy.py", "ops/host.py", "ops/post.py",
        "ops/preflight.py",
        "ops/report.py", "ops/status.py", "ops/units.py", "publish/__init__.py", "publish/__main__.py",
        "publish/contract.py", "publish/send.py", "publish/snapshot.py",
    ]  # fmt: skip
    return found


def test_it_imports_no_venue_no_lock_no_strategy_and_no_book():
    for rel, tree in layer():
        for _, name in imports(tree, rel):
            assert not name.startswith(NOT_ITS_BUSINESS), f"{rel} imports {name}"
            assert name.split(".")[0] not in {"ib_async", "socket", "ssl", "asyncio", "http", "requests"}, (rel, name)


def test_of_the_trading_side_it_reads_only_the_shadow_journal():
    every = [(rel, name) for rel, tree in layer() for _, name in imports(tree, rel)]
    reached = {(rel, name) for rel, name in every if name.startswith("hftlab.shadow")}
    assert reached == {
        ("src/hftlab/ops/status.py", "hftlab.shadow.journal.JournalError"),
        ("src/hftlab/ops/status.py", "hftlab.shadow.journal.read"),
    }


def test_nothing_in_it_calls_a_venue_or_reaches_anything_by_name():
    for rel, tree in layer():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in ORDER_CALLS | BY_NAME, f"{rel} line {node.lineno}: {called_name(node)}"
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert "Venue" not in name and name not in ("QuoteSource", "MarketDataView"), f"{rel}: {name}"


def test_only_the_post_module_touches_the_http_client_and_only_the_backup_and_the_deploy_start_a_program():
    for rel, tree in layer():
        names = {name for _, name in imports(tree, rel)}
        http = {name for name in names if name.startswith(("urllib.request", "urllib.error"))}
        assert bool(http) == (rel == "src/hftlab/ops/post.py"), f"{rel} imports {sorted(http)}"
        starts_programs = ("src/hftlab/ops/backup.py", "src/hftlab/ops/deploy.py")  # the tool; git, uv, systemctl, gh
        assert ("subprocess" in names) == (rel in starts_programs), rel


def test_the_pre_flight_gate_imports_the_standard_library_and_the_clock_and_nothing_else_of_ours():
    tree = dict(layer())["src/hftlab/ops/preflight.py"]
    ours = sorted(name for _, name in imports(tree, "src/hftlab/ops/preflight.py") if name.startswith("hftlab"))
    assert ours == ["hftlab.core.clock.WallClock"]
    assert "yaml" not in {name.split(".")[0] for _, name in imports(tree, "src/hftlab/ops/preflight.py")}


def test_no_module_of_the_layer_writes_into_the_recording():
    """It measures the store and counts its records. Writing there is the recorder's alone."""
    for rel, tree in layer():
        for _, name in imports(tree, rel):
            if name.startswith("hftlab.record"):
                assert name.rsplit(".", 1)[1] in {
                    "CorruptSegment", "UNEXPLAINED", "count_records", "day_of", "days", "measure", "read_journal",
                    "segments", "JOURNAL",
                }, f"{rel} imports {name}"  # fmt: skip
