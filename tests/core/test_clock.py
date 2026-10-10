"""The injected clock, and the scan that keeps system time out of every module but the wall clock."""
from __future__ import annotations

import ast
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import Clock, ClockWentBackwards, SimClock, WallClock
from tests.support import called_name, imports, runs_text, sources

WALL_CLOCK = "src/hftlab/core/clock.py"
CLOCK_READS = frozenset({"now", "utcnow", "today"})


# ---- SimClock ------------------------------------------------------------------------------------------------------


def test_a_sim_clock_starts_where_it_is_told_and_stands_still():
    clock = SimClock(1_760_000_000_000_000_000)
    assert clock.now_ns() == clock.now_ns() == 1_760_000_000_000_000_000
    assert SimClock().now_ns() == 0


@given(st.integers(0, 2**62), st.lists(st.integers(0, 10**12), max_size=50))
def test_a_sim_clock_is_the_running_sum_of_its_advances_and_never_decreases(start, deltas):
    clock = SimClock(start)
    seen = [clock.now_ns()]
    for delta in deltas:
        assert clock.advance(delta) == clock.now_ns()
        seen.append(clock.now_ns())
    assert seen[-1] == start + sum(deltas)
    assert seen == sorted(seen)
    assert all(type(t) is int for t in seen)


@given(st.integers(0, 2**62), st.integers(1, 10**12))
def test_a_sim_clock_refuses_to_go_backwards_and_stays_put(start, back):
    clock = SimClock(start + back)
    with pytest.raises(ClockWentBackwards):
        clock.advance(-back)
    with pytest.raises(ClockWentBackwards):
        clock.advance_to(start)
    assert clock.now_ns() == start + back


def test_a_sim_clock_can_be_moved_to_now_or_later():
    clock = SimClock(10)
    assert clock.advance_to(10) == 10 and clock.advance(0) == 10
    assert clock.advance_to(25) == 25


@pytest.mark.parametrize("wrong", [1.0, True, "5", None])
def test_a_sim_clock_takes_whole_nanoseconds_only(wrong):
    with pytest.raises(TypeError):
        SimClock(wrong)
    with pytest.raises(TypeError):
        SimClock().advance(wrong)
    with pytest.raises(TypeError):
        SimClock().advance_to(wrong)


def test_a_sim_clock_cannot_start_before_the_epoch():
    with pytest.raises(ValueError):
        SimClock(-1)


def test_the_wall_clock_reads_integer_nanoseconds_from_the_system():
    before = time.time_ns()
    now = WallClock().now_ns()
    assert type(now) is int and before <= now <= time.time_ns()


def test_both_clocks_satisfy_the_protocol():
    assert isinstance(SimClock(), Clock) and isinstance(WallClock(), Clock)
    assert not isinstance(object(), Clock)


# ---- only the wall clock reads system time -------------------------------------------------------------------------


def system_time_uses(rel: str, tree: ast.AST) -> list[str]:
    """Every way `rel` could learn the real time: the `time` module, or a now/utcnow/today on anything."""
    out = []
    for how, name in imports(tree, rel):
        if how == "computed":
            out.append(f"{rel}: imports a module by a computed name")
        elif name == "time" or name.startswith("time."):
            out.append(f"{rel}: imports the time module ({how})")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in CLOCK_READS:
            out.append(f"{rel}: reads the clock through .{node.attr}")
        elif isinstance(node, ast.ImportFrom) and any(alias.name in CLOCK_READS for alias in node.names):
            out.append(f"{rel}: imports a clock read by name")
        elif isinstance(node, ast.Call) and called_name(node) == "getattr" and len(node.args) >= 2:
            name_arg = node.args[1]
            if not isinstance(name_arg, ast.Constant) or name_arg.value in CLOCK_READS | {"time", "time_ns"}:
                out.append(f"{rel}: getattr could reach a clock read")
        elif runs_text(node):
            out.append(f"{rel}: runs code from text")
    return out


def test_no_module_but_the_wall_clock_reads_system_time():
    found = [v for rel, tree in sources() if rel != WALL_CLOCK for v in system_time_uses(rel, tree)]
    assert found == []


def test_the_wall_clock_is_exempt_for_a_reason():
    tree = dict(sources())[WALL_CLOCK]
    assert system_time_uses(WALL_CLOCK, tree), "the scan no longer sees the one place that reads the clock"


@pytest.mark.parametrize(
    "src",
    [
        "import time",
        "import time as t",
        "from time import time_ns",
        "from time import monotonic as tick",
        "import importlib; importlib.import_module('time')",
        "__import__('time').time_ns()",
        "import sys; sys.modules['time'].time()",
        "import importlib; importlib.import_module(name)",
        "from datetime import datetime; datetime.now()",
        "import datetime; datetime.datetime.now(datetime.timezone.utc)",
        "import datetime as dt; dt.datetime.utcnow()",
        "from datetime import date; date.today()",
        "from datetime import datetime; clock = datetime.now",
        "from datetime import datetime; getattr(datetime, 'now')()",
        "from datetime import datetime; getattr(datetime, which)()",
        "import pandas; pandas.Timestamp.now()",
        "eval('__import__(\"time\").time()')",
        "exec(code)",
    ],
)
def test_the_scan_catches_each_way_to_read_the_clock(src):
    assert system_time_uses("src/hftlab/strategy/sneaky.py", ast.parse(src))


@pytest.mark.parametrize(
    "src",
    [
        "def decide(clock): return clock.now_ns()",
        "from datetime import datetime, timezone as tz\ndef utc(s): return datetime.fromtimestamp(s, tz.utc)",
        "from hftlab.core.clock import Clock, SimClock",
        "timeout_ns = 5\ntime_in_force = 'IOC'",
        "import decimal, re, json",
        "kind = getattr(order, 'side')",
    ],
)
def test_the_scan_passes_code_that_takes_its_time_from_the_clock(src):
    assert system_time_uses("src/hftlab/strategy/honest.py", ast.parse(src)) == []
