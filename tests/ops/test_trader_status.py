"""The trader's parts of the status screen and of the snapshot: there when a trader has run, absent before.

The status file is the one a real trader writes, on the fake broker (tests/trader/kit.py). The snapshot is held
to the contract file as it stands; the contract is not changed here.
"""
from __future__ import annotations

import json

import pytest

from hftlab.ops import status
from hftlab.ops.status import read, render
from hftlab.publish import contract
from hftlab.publish.snapshot import build, trader_parts
from hftlab.trader.tape import housekeep, tape_bytes
from tests.ops.kit import sources
from tests.support import ROOT
from tests.trader.kit import SECOND, Desk, put_stage

SCHEMA = contract.load(ROOT / contract.CONTRACT)
PHASES = {f"F{n}": "open" for n in range(8)}


def after_a_run(tmp_path, *, paper: bool = True, seconds: int = 60):
    """A trader that ran for a minute into tmp_path/var/trader, and the reading of that host a second later."""
    folder = tmp_path / "var" / "trader"
    if paper:
        put_stage(folder, "control", "min_size")
    desk = Desk(folder, paper=paper)
    desk.run(seconds)
    places = sources(tmp_path, trader_status=folder / "status.json")
    return desk, places, read(places, desk.clock.now_ns() + SECOND)


def snap(reading) -> dict:
    return build(reading, phases=PHASES, version="0123456789ab")


def test_before_a_trader_has_run_there_is_no_trader_reading_and_no_sleeves_or_books_part(tmp_path):
    for places in (sources(tmp_path), sources(tmp_path, trader_status=tmp_path / "var" / "trader" / "status.json")):
        reading = read(places, 1_791_342_000 * SECOND)
        assert reading.trader is None and reading.problems == ()
        found = snap(reading)
        assert "sleeves" not in found and "books" not in found, "absent, never zeros"
        assert contract.problems(found, SCHEMA) == [] and "trader     never run" in render(reading)


def test_a_trader_on_paper_is_read_from_its_own_status_and_published_as_the_contract_defines_it(tmp_path):
    desk, _, reading = after_a_run(tmp_path)
    trader = reading.trader
    assert (trader.state, trader.mode, trader.connection) == ("running", "paper", "READY")
    assert trader.day == desk.trader.label
    control = next(sleeve for sleeve in trader.sleeves if sleeve.name == "control")
    assert control.stage == "min_size" and control.trades_today == control.trades_total > 10
    found = snap(reading)
    assert contract.problems(found, SCHEMA) == []
    assert found["desk"]["stage"] == "paper"
    (sleeve,) = found["sleeves"]
    assert sleeve["name"] == "control", "the idle sleeve is not a name the contract knows, so it is left out"
    assert sleeve["stage"] == "min_size" and sleeve["trades_total"] == control.trades_total
    assert sleeve["currency"] == "USD" and sleeve["net_total"] == round(control.net_total_micro["USD"] / 1e6, 2) < 0
    books = found["books"]
    assert books["currency"] == "USD" and books["reconciliation_breaks_today"] == 0
    assert books["shadow"]["net_total"] == sleeve["net_total"], "the shadow book is the result of record"
    assert books["broker"]["net_total"] is not None and books["broker"]["net_total"] < 0
    assert books["open_positions"] == trader.open_positions
    screen = render(reading)
    assert "trader     running; paper;" in screen and "sleeve control: min_size;" in screen and " USD" in screen


def test_a_shadow_only_trader_publishes_its_shadow_book_and_no_number_for_a_broker_book_it_does_not_have(tmp_path):
    _, _, reading = after_a_run(tmp_path, paper=False)
    assert reading.trader.mode == "shadow_only" and reading.trader.broker is None
    found = snap(reading)
    assert contract.problems(found, SCHEMA) == [] and found["desk"]["stage"] == "shadow"
    assert found["books"]["broker"] == {"net_today": None, "net_total": None}, "null, not zero"
    assert found["sleeves"][0]["stage"] == "shadow" and found["sleeves"][0]["trades_total"] > 10
    moved = {code for code, amount in reading.trader.shadow.net_total_micro.items() if amount}
    assert moved == {"USD", "JPY"} and found["books"]["shadow"]["net_total"] is None, "two currencies are not added"
    assert "shadow-only" in render(reading)


def test_a_trader_that_stopped_is_still_published_from_its_last_word_and_says_stopped(tmp_path):
    desk, places, _ = after_a_run(tmp_path)
    later = read(places, desk.clock.now_ns() + 3_600 * SECOND)
    assert later.trader.state == "stopped" and later.trader.age_ns >= 3_600 * SECOND
    assert contract.problems(snap(later), SCHEMA) == [] and "sleeves" in snap(later)


def test_a_result_in_more_than_one_currency_is_not_added_up_it_is_published_as_not_worked_out(tmp_path):
    _, places, reading = after_a_run(tmp_path)
    said = json.loads(places.trader_status.read_text())
    said["sleeves"][1]["net_total_micro"]["JPY"] = -5_000_000
    said["books"]["shadow"]["net_total_micro"]["JPY"] = -5_000_000
    places.trader_status.write_text(json.dumps(said))
    parts = trader_parts(read(places, reading.at_ns).trader)
    assert parts["sleeves"][0]["net_total"] is None and parts["books"]["shadow"]["net_total"] is None
    assert contract.problems(snap(read(places, reading.at_ns)), SCHEMA) == []


def test_latches_and_breaks_are_warnings_here_with_codes_the_contract_accepts(tmp_path):
    desk, places, _ = after_a_run(tmp_path, seconds=10)
    desk.trader.gate.halt()
    desk.run(5)
    reading = read(places, desk.clock.now_ns())
    assert [problem.code for problem in reading.problems] == ["trader_latched"] and reading.level == "warn"
    assert reading.trader.latched == ("ORDER_FAULT",) and "latched: ORDER_FAULT" in render(reading)
    assert next(s for s in reading.trader.sleeves if s.name == "control").stage == "stepped_down"
    found = snap(reading)
    assert contract.problems(found, SCHEMA) == [] and found["health"]["reasons"] == ["trader_latched"]
    assert found["sleeves"][0]["stage"] == "stepped_down"


@pytest.mark.parametrize("damage", ["{", "[]", '{"at_ns": 1}', '{"at_ns": "x", "books": {}, "sleeves": []}'])
def test_a_status_file_that_cannot_be_read_is_a_warning_and_publishes_no_trader_part(tmp_path, damage):
    _, places, reading = after_a_run(tmp_path, seconds=5)
    places.trader_status.write_text(damage)
    again = read(places, reading.at_ns)
    assert again.trader is None and [problem.code for problem in again.problems] == ["trader_status_unreadable"]
    assert "sleeves" not in snap(again) and contract.problems(snap(again), SCHEMA) == []


def test_the_shipped_settings_say_where_the_traders_status_is(tmp_path):
    places = status.load_sources(
        ROOT / "config" / "ops.yaml", ROOT / "config" / "recorder.yaml", ROOT / "config" / "shadow.yaml",
        ROOT / "config" / "calendar", state=tmp_path, trader_config=ROOT / "config" / "trader.yaml",
    )  # fmt: skip
    assert places.trader_status.as_posix() == "var/trader/status.json"
    without = status.load_sources(
        ROOT / "config" / "ops.yaml", ROOT / "config" / "recorder.yaml", ROOT / "config" / "shadow.yaml",
        ROOT / "config" / "calendar", state=tmp_path, trader_config=tmp_path / "none.yaml",
    )  # fmt: skip
    assert without.trader_status is None


# ---- the tapes against their limits (ADR 0027, addendum) -------------------------------------------------------------

LIMITS = {"part_bytes": 3_000, "quota_bytes": 40_000, "soft_bytes": 8_000, "reserve_bytes": 28_000}  # hard at 12,000


def a_bounded_run(tmp_path, seconds: int):
    folder = tmp_path / "var" / "trader"
    put_stage(folder, "control", "min_size")
    desk = Desk(folder, edge=LIMITS)
    desk.run(seconds)
    return desk, sources(tmp_path, trader_status=folder / "status.json"), folder / "days"


def test_the_screen_shows_the_tapes_against_their_soft_limit_their_hard_limit_and_their_quota(tmp_path):
    desk, places, days = a_bounded_run(tmp_path, 2)
    reading = read(places, desk.clock.now_ns())
    tape, used = reading.trader.tape, tape_bytes(days)
    assert (tape.used_bytes, tape.soft_bytes, tape.hard_bytes, tape.quota_bytes) == (used, 8_000, 12_000, 40_000)
    assert (tape.level, tape.dark) == ("room", False) and reading.problems == ()
    line = f"             tapes: {used:,} bytes; soft limit 8,000, hard limit 12,000, quota 40,000"
    assert f"{line} ({used * 100 // 40_000}%); under both limits" in render(reading).splitlines()
    assert contract.problems(snap(reading), SCHEMA) == [], "the limits are on the screen, not in the snapshot"
    assert "tape" not in json.dumps(snap(reading))


def test_the_soft_limit_is_a_warning_and_the_hard_limit_is_bad_each_under_a_code_the_contract_accepts(tmp_path):
    desk, places, days = a_bounded_run(tmp_path, 2)
    while desk.edge.level == 0:
        desk.run(1)
    reading = read(places, desk.clock.now_ns())
    assert reading.trader.tape.level == "soft" and reading.level == "warn"
    assert [problem.code for problem in reading.problems] == ["trader_tape_soft_limit"]
    assert "AT THE SOFT LIMIT: exits only" in render(reading)
    while not desk.trader.flat_at_hard_limit:
        desk.run(1)
    desk.stop()
    reading = read(places, desk.clock.now_ns())
    assert reading.trader.tape.level == "hard" and reading.level == "bad"
    assert status.exit_code(reading) == status.EXIT_BAD
    assert [problem.code for problem in reading.problems] == ["trader_tape_hard_limit"]
    assert "AT THE HARD LIMIT: closing everything" in render(reading)
    found = snap(reading)
    assert contract.problems(found, SCHEMA) == [] and found["health"]["reasons"] == ["trader_tape_hard_limit"]
    assert "trader_tape_hard_limit" in status.PAGED, "what this command pages it also takes back"

    housekeep(days)  # no trader says anything now: the bytes are counted on the disk, as they are
    later = read(places, desk.clock.now_ns() + SECOND)
    assert later.trader.tape.used_bytes == tape_bytes(days) < 8_000 and later.trader.tape.level == "room"
    assert later.problems == () and "under both limits" in render(later)


def test_a_session_that_is_no_longer_recorded_says_so_on_the_screen(tmp_path):
    desk, places, _ = a_bounded_run(tmp_path, 2)
    while not desk.trader.flat_at_hard_limit:
        desk.run(1)
    desk.stop()
    said = json.loads(places.trader_status.read_text())
    assert said["tape"]["dark"] == 0 and said["tape"]["level"] == 2
    said["tape"]["dark"] = 1
    places.trader_status.write_text(json.dumps(said))
    reading = read(places, desk.clock.now_ns())
    assert reading.trader.tape.dark and "RESERVE USED UP: the session is not recorded" in render(reading)
    (problem,) = reading.problems
    assert (problem.code, problem.level) == ("trader_tape_hard_limit", "bad") and "no longer recorded" in problem.text


def test_a_trader_under_no_quota_has_no_tape_line_and_one_whose_limits_cannot_be_read_is_unreadable(tmp_path):
    _, places, reading = after_a_run(tmp_path, seconds=5)
    assert reading.trader.tape is None and "tapes:" not in render(reading)
    said = json.loads(places.trader_status.read_text())
    said["tape"] = {"used_bytes": 1, "soft_bytes": 5, "hard_bytes": "many", "quota_bytes": 10}
    places.trader_status.write_text(json.dumps(said))
    again = read(places, reading.at_ns)
    assert again.trader is None and [problem.code for problem in again.problems] == ["trader_status_unreadable"]
