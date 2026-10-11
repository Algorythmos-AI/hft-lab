"""Sleeve state across a restart of the trader: restored when it can be believed, cold and reported when not.

The trader is the real one on the fake broker's market. A "restart" is a new process start on the same files; a
"kill" is a start after a process that ended without a word. The market is scripted once for the whole of a
test, so a run that was cut and a run that was not are shown the same quotes.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from hftlab.calendar import rollover_near
from hftlab.journal import Journal, read, verify
from hftlab.lineup import Lineup
from hftlab.trader import resume
from hftlab.trader.session import mark
from tests.lineup.kit import CHALLENGER, ML, effect, put, register
from tests.lineup.kit import desk as lineup_desk
from tests.trader.kit import NEVER, SECOND, T0, Desk, put_stage, raw_settings

HOUR = 3_600 * SECOND
WHOLE = 120  # seconds of market in a test that compares a cut run with an uncut one


def settings(control: dict | None = None, **dc) -> dict:
    """A control sleeve and a Directional Change sleeve whose threshold the walking book crosses every few seconds."""
    raw = raw_settings()
    sleeve = {
        "name": "dc", "kind": "directional_change", "price": "mid", "variant": "trend", "threshold_percent": "0.02",
        "overshoot_multiple": None, "max_hold_seconds": 20, "min_spread_ratio": "2", "warm_up_events": 2,
        "gap_seconds": 10, "retry_seconds": 1, "max_attempts": 2, **dc,
    }  # fmt: skip
    raw["sleeves"] = [raw["sleeves"][0], {**raw["sleeves"][1], **(control or {})}, sleeve]
    raw["rollout"]["dc"] = {"start": "shadow", "promote": {"min_size": dict(NEVER), "normal": dict(NEVER)}}
    return raw


def a_desk(folder, *, paper: bool = False, seconds: int = WHOLE, **options) -> Desk:
    made = Desk(folder, paper=paper, settings=options.pop("settings", None) or settings(), **options)
    made.quotes(seconds)
    return made


def decided(made: Desk, sleeve: str = "dc") -> list[tuple]:
    """Every decision made for a sleeve, by anyone: when, by whom, and what."""
    return [
        (e["at_ns"], e["source"], e["pair"], e["side"], e["quantity"], e["leg"], e["attempt"])
        for e in made.of("decision")
        if e["sleeve"] == sleeve
    ]


def state_of(made: Desk) -> dict:
    """The last word of the trader about its sleeves' states: who was restored, and why each other one was not."""
    said = made.of("sleeve_state")[-1]
    return {"restored": said["restored"], "cold": said["cold"]}


def entries(folder) -> list[dict]:
    return read(folder / resume.FILE)[1]


# ---- what is written -------------------------------------------------------------------------------------------------


def test_a_session_that_ends_on_purpose_writes_each_sleeves_state_tied_to_its_journals_and_its_tape(tmp_path):
    made = a_desk(tmp_path)
    assert not (tmp_path / resume.FILE).exists() and state_of(made) == {
        "restored": [], "cold": {"control": "NONE", "dc": "NONE"},
    }, "the first start has nothing to restore, says so, and pages nobody"  # fmt: skip
    assert "sleeve-state" not in made.pages.fired
    made.run(30, quotes=False)
    made.stop()
    (entry,) = entries(tmp_path)
    tape = made.day() / "session-001.tape.jsonl"
    last_line = tape.read_bytes().split(b"\n")[-2]
    assert json.loads(last_line) == ["stop", entry["at_ns"]], "the tape's last word, and the state's last moment"
    assert (entry["day"], entry["session"], entry["tape"]) == (made.trader.label, 1, tape.name)
    assert entry["tape_end"] == [tape.stat().st_size, hashlib.sha256(last_line).hexdigest()]
    names = ("decisions", "outbox", "risk", "reconcile", "stages", "shadow")
    assert set(entry["marks"]) == set(names)
    for name in names:
        path = made.day() / "decisions.jsonl" if name == "decisions" else tmp_path / f"{name}.jsonl"
        assert entry["marks"][name] == mark(path), f"where {name} stood once the session had closed it"
    assert set(entry["states"]) | set(entry["in_flight"]) == {"control", "dc"}, "the idle sleeve keeps nothing"
    assert entry["stages"] == {"idle": "shadow", "control": "shadow", "dc": "shadow"} and entry["n"] > 0
    machine = entry["states"]["dc"]["state"][0]["detector"]["machines"][0]
    assert machine["events"] > 3, "the detector, as it writes itself"
    assert verify(tmp_path / resume.FILE).entries == 1

    made.start()
    made.run(5, quotes=False)
    made.stop()
    assert [e["session"] for e in entries(tmp_path)] == [1, 2], "one entry a session, each chained to the one before"


def test_a_session_that_ends_at_the_days_close_writes_it_too_and_the_next_day_starts_from_it(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    made = Desk(tmp_path, paper=False, settings=settings(), at_ns=rollover - 30 * SECOND)
    made.quoted = made.clock.now_ns()
    first = made.trader.label
    assert made.run(60) is True
    made.stop()
    assert [e["day"] for e in entries(tmp_path)] == [first]
    made.session()  # the same process, the next trading day: another day folder, another decision journal
    assert made.trader.label != first and state_of(made)["restored"] == ["control", "dc"]


# ---- restored: exactly the uncut run -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def uncut(tmp_path_factory):
    """What the Directional Change sleeve decides when nothing interrupts it, on paper settings and without."""
    found = {}
    for paper in (False, True):
        made = a_desk(tmp_path_factory.mktemp("uncut"), paper=paper)
        made.run(WHOLE, quotes=False)
        made.stop()
        found[paper] = decided(made)
        assert len(found[paper]) > 30 and {source for _, source, *_ in found[paper]} == {"sleeve"}
    return found


@pytest.mark.parametrize("paper", [False, True], ids=["shadow only", "limits in force"])
@pytest.mark.parametrize("cut", [7, 23, 40, 61, 88, 110])
def test_a_trader_stopped_and_started_again_decides_for_a_restored_sleeve_exactly_as_one_never_stopped(
    tmp_path, uncut, cut, paper
):
    made = a_desk(tmp_path, paper=paper)
    made.run(cut, quotes=False)
    before = made.trader._states["dc"]
    made.stop()
    made.start()
    assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}
    assert made.trader._states["dc"] == before, "the state it stopped with, and no other"
    made.run(WHOLE - cut, quotes=False)
    made.stop()
    assert decided(made) == uncut[paper], "every decision, to the nanosecond, the side and the attempt"
    assert made.of("inherited") == [], "what it held over the restart was its own, and it left it by its own rule"
    assert all(one.identical for one in made.replay(tmp_path / "scratch")), "and both sessions replay byte for byte"


@pytest.mark.parametrize("cut", [23, 61])
def test_a_trader_that_was_killed_starts_every_sleeve_cold_and_closes_what_they_held_as_before(tmp_path, uncut, cut):
    made = a_desk(tmp_path)
    made.run(cut, quotes=False)
    made.restart()  # gone without a word: no state was written
    assert state_of(made) == {"restored": [], "cold": {"control": "NONE", "dc": "NONE"}}
    assert made.trader._states["dc"] == made.trader._sleeves[2].initial(), "warm-up flags and all"
    made.run(WHOLE - cut, quotes=False)
    made.stop()
    assert decided(made) != uncut[False], "a cold sleeve is another sleeve: it warms up again"
    assert all(one.identical for one in made.replay(tmp_path / "scratch"))


def test_a_restored_sleeve_on_paper_leaves_its_position_itself_and_a_cold_one_has_it_closed_by_the_trader(tmp_path):
    held = {"entry_per_million": 1_000_000, "hold_seconds": 30, "retry_seconds": 1}
    outcomes = {}
    for how in ("stopped", "killed"):
        folder = tmp_path / how
        put_stage(folder, "control", "min_size")
        made = a_desk(folder, paper=True, settings=settings(held))
        made.run(10, quotes=False)
        assert made.held()[0] != 0 and made.held()[0] == made.held()[2], "a paper position, and the broker agrees"
        entered_at = made.of("broker_fill")[0]["at_ns"]
        if how == "stopped":
            made.stop()
            made.start()
        else:
            made.restart()
        made.run(50, quotes=False)
        exits = [e for e in made.of("decision") if e["sleeve"] == "control" and e["leg"] != "entry"]
        outcomes[how] = (state_of(made)["restored"], exits[0]["source"], exits[0]["at_ns"] - entered_at >= 30 * SECOND)
        made.stop()
    assert outcomes["stopped"] == (["control", "dc"], "sleeve", True), "held for its thirty seconds, then left by it"
    assert outcomes["killed"] == ([], "inherit", False), "nobody remembers why it was taken: the trader closes it"


# ---- never trusted ---------------------------------------------------------------------------------------------------


def test_a_state_left_behind_by_a_session_that_was_killed_later_is_stale_and_is_not_restored(tmp_path):
    made = a_desk(tmp_path)
    made.run(23, quotes=False)
    made.stop()
    made.start()
    assert state_of(made)["restored"] == ["control", "dc"] and made.pages.resolved[-1] == "sleeve-state"
    made.run(23, quotes=False)
    made.restart()  # the second session dies; the only entry is the first session's
    assert len(entries(tmp_path)) == 1
    assert state_of(made) == {"restored": [], "cold": {"control": "JOURNALS", "dc": "JOURNALS"}}
    assert made.trader._states["dc"] == made.trader._sleeves[2].initial()
    assert made.pages.fired[-1] == "sleeve-state", "and the owner is told that a state was not believed"
    made.run(23, quotes=False)
    made.stop()
    made.start()
    assert "dc" in state_of(made)["restored"], "the entry of the session that ended on purpose is believed again"
    made.stop()
    assert all(one.identical for one in made.replay(tmp_path / "scratch"))


def stopped(tmp_path) -> Desk:
    made = a_desk(tmp_path)
    made.run(23, quotes=False)
    made.stop()
    return made


def test_a_state_whose_tape_is_not_the_last_tape_or_does_not_end_as_it_did_is_not_restored(tmp_path):
    for case, damage in (
        ("a later tape", lambda day, tape: (day / "session-002.tape.jsonl").write_bytes(b'["open",1,"x",[]]\n')),
        ("a tape with more on it", lambda day, tape: tape.write_bytes(tape.read_bytes() + b'["step",1,"READY"]\n')),
        ("a tape cut short", lambda day, tape: tape.write_bytes(tape.read_bytes()[:-12])),
        ("no tape", lambda day, tape: tape.unlink()),
        ("a tape of a later day", lambda day, tape: tape.rename(
            (day.parent / "2099-01-01").mkdir() or day.parent / "2099-01-01" / tape.name)),
    ):  # fmt: skip
        made = stopped(tmp_path / case.replace(" ", "-"))
        damage(made.day(), made.day() / "session-001.tape.jsonl")
        made.start()
        assert state_of(made) == {"restored": [], "cold": {"control": "TAPE", "dc": "TAPE"}}, case
        assert "sleeve-state" in made.pages.fired
    untouched = stopped(tmp_path / "untouched")
    untouched.start()
    assert state_of(untouched)["restored"] == ["control", "dc"], "the same day, with nothing done to it"


def test_a_state_whose_journals_moved_on_is_not_restored_whichever_journal_it_was(tmp_path):
    for name in ("shadow", "stages", "outbox", "risk", "reconcile"):
        made = stopped(tmp_path / name)
        journal = Journal(tmp_path / name / f"{name}.jsonl")
        journal.open()
        journal.write("note", said="something was written here after the session ended")
        journal.close()
        made.start()
        assert state_of(made)["cold"] == {"control": "JOURNALS", "dc": "JOURNALS"}, name
    made = stopped(tmp_path / "decisions")
    journal = Journal(made.day() / "decisions.jsonl")
    journal.open()
    journal.write("note", n=0, at_ns=made.clock.now_ns())
    journal.close()
    made.start()
    assert state_of(made)["cold"] == {"control": "JOURNALS", "dc": "JOURNALS"}, "the day's decision journal too"


def test_a_corrupted_state_file_starts_every_sleeve_cold_is_reported_set_aside_and_never_repaired(tmp_path):
    made = stopped(tmp_path)
    path = tmp_path / resume.FILE
    whole = path.read_bytes()
    path.write_bytes(whole.replace(b'"warm_left":0', b'"warm_left":1', 1))  # one number of one machine
    assert path.read_bytes() != whole
    made.start()
    assert state_of(made) == {"restored": [], "cold": {"control": "UNVERIFIED", "dc": "UNVERIFIED"}}
    assert made.pages.fired[-1] == "sleeve-state"
    made.run(20, quotes=False)
    made.stop()
    assert (tmp_path / (resume.FILE + ".unverified")).read_bytes() != whole, "kept as it was found, under another name"
    assert [e["session"] for e in entries(tmp_path)] == [2], "and a new chain begins with this session's entry"
    made.start()
    assert state_of(made)["restored"] == ["control", "dc"]
    made.stop()
    replayed = made.replay(tmp_path / "scratch")
    assert [one.identical for one in replayed] == [True, True, True], "the session that found it broken among them"


def test_a_last_entry_that_is_not_a_state_or_is_torn_restores_nothing(tmp_path):
    made = stopped(tmp_path / "other")
    journal = Journal(tmp_path / "other" / resume.FILE)
    journal.open()
    journal.write("note", said="a line that verifies and is no state")
    journal.close()
    made.start()
    assert state_of(made)["cold"] == {"control": "NOT_A_STATE", "dc": "NOT_A_STATE"}

    torn = a_desk(tmp_path / "torn")
    torn.run(10, quotes=False)
    torn.stop()
    torn.start()
    torn.run(10, quotes=False)
    torn.stop()
    path = tmp_path / "torn" / resume.FILE
    path.write_bytes(path.read_bytes()[:-40])  # the process died while it wrote the second entry
    torn.start()
    assert state_of(torn)["cold"] == {"control": "JOURNALS", "dc": "JOURNALS"}, "the whole entry before it is stale"
    assert resume.last(tmp_path / "nothing" / resume.FILE) == (None, "NONE")


# ---- one sleeve at a time --------------------------------------------------------------------------------------------


def test_a_sleeve_whose_settings_changed_starts_cold_and_the_others_are_restored(tmp_path):
    made = stopped(tmp_path)
    other = settings(threshold_percent="0.03")
    made.settings = Desk(tmp_path / "other", paper=False, settings=other, start=False).settings
    made.start()
    assert state_of(made) == {"restored": ["control"], "cold": {"dc": "SETTINGS"}}
    assert made.trader._states["dc"] == made.trader._sleeves[2].initial() and "sleeve-state" in made.pages.fired
    made.stop()
    renamed = settings()
    renamed["sleeves"][2]["name"] = "dc-two"
    renamed["rollout"]["dc-two"] = renamed["rollout"].pop("dc")
    made.settings = Desk(tmp_path / "another", paper=False, settings=renamed, start=False).settings
    made.start()
    assert state_of(made) == {"restored": ["control"], "cold": {"dc-two": "ABSENT"}}, "a new sleeve has no state"


def test_a_sleeve_with_an_order_on_its_way_when_the_session_ended_has_no_state_and_starts_cold(tmp_path):
    busy = {"entry_per_million": 1_000_000, "hold_seconds": 0, "retry_seconds": 0}
    made = a_desk(tmp_path, settings=settings(busy), seconds=30)
    made.run(10, quotes=False)
    made.stop()
    (entry,) = entries(tmp_path)
    assert entry["in_flight"] == ["control"] and set(entry["states"]) == {"dc"}, "it trades on every quote"
    made.start()
    assert state_of(made) == {"restored": ["dc"], "cold": {"control": "IN_FLIGHT"}}
    made.run(10, quotes=False)
    made.stop()
    assert all(one.identical for one in made.replay(tmp_path / "scratch"))


def test_a_sleeve_that_was_moved_to_another_stage_starts_cold_its_state_was_reached_on_another_book(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    raw = settings()
    raw["clean_day_minutes"] = 1
    raw["rollout"]["control"]["promote"]["min_size"] = {"clean_days": 1, "decision": "HFT-DEC-0042"}
    made = Desk(tmp_path, settings=raw, at_ns=rollover - 90 * SECOND)
    made.quoted = made.clock.now_ns()
    assert made.run(120) is True
    made.stop()
    assert entries(tmp_path)[-1]["stages"]["control"] == "shadow", "the stage it ran under, not the one it earned"
    made.session()
    assert made.trader.stages.stage("control") == "min_size"
    assert state_of(made) == {"restored": ["dc"], "cold": {"control": "STAGE"}}


# ---- a model's sleeve ------------------------------------------------------------------------------------------------


def test_an_ml_place_is_restored_with_its_model_and_starts_cold_when_the_lineup_put_another_model_there(tmp_path):
    registry = tmp_path / "registry"
    a, b = register(registry, 1), register(registry, 3)
    folder = tmp_path / "state"
    before = put(folder, Lineup(champion=a, challengers=(b,)))
    made = lineup_desk(folder, registry)
    made.run(effect(made, 7, 5), quotes=False)
    history = made.trader._states[ML].features
    made.stop()
    made.start()
    assert {ML, CHALLENGER} <= set(state_of(made)["restored"])
    assert made.trader._states[ML].features == history and len(history.times) >= 2, "its feature history with it"
    made.run(effect(made, 8, 5), quotes=False)
    made.stop()
    put(folder, before.promoted(b), at_ns=2)  # the weekly decision: another model in the champion's place
    made.start()
    found = state_of(made)
    assert found["cold"][ML] == "SETTINGS" and ML not in found["restored"]
    assert CHALLENGER not in found["restored"] and CHALLENGER not in found["cold"], "its place is empty now: idle"
    made.stop()
    assert all(one.identical for one in made.replay(tmp_path / "scratch"))


# ---- the tape's end, read as bytes -----------------------------------------------------------------------------------


def test_how_a_tape_ends_is_its_length_and_the_hash_of_its_last_whole_line_and_nothing_is_parsed(tmp_path):
    tape = tmp_path / "session-001.tape.jsonl"
    assert resume.tape_end(tape) == [0, ""], "no tape"
    tape.write_bytes(b"")
    assert resume.tape_end(tape) == [0, ""]
    tape.write_bytes(b"not json\nnor this\n")
    assert resume.tape_end(tape) == [18, hashlib.sha256(b"nor this").hexdigest()]
    tape.write_bytes(b"one\ntorn")
    assert resume.tape_end(tape) == [8, ""], "a last line that was being written is no last word"
    tape.write_bytes(b"x" * 100_000 + b"\n")
    assert resume.tape_end(tape) == [100_001, ""], "nor is a line longer than any last word of a tape"
    tape.write_bytes(b"x" * 100_000 + b"\nend\n")
    assert resume.tape_end(tape) == [100_005, hashlib.sha256(b"end").hexdigest()]


def test_the_last_tape_is_the_highest_number_of_the_latest_day_that_has_one(tmp_path):
    assert resume.latest_tape(tmp_path / "days") is None
    for day, names in (("2026-10-13", ["session-001.tape.jsonl", "session-002.tape.jsonl"]), ("2026-10-14", [])):
        (tmp_path / "days" / day).mkdir(parents=True)
        for name in names:
            (tmp_path / "days" / day / name).write_bytes(b"")
    (tmp_path / "days" / "2026-10-14" / "decisions.jsonl").write_bytes(b"")
    assert resume.latest_tape(tmp_path / "days") == tmp_path / "days" / "2026-10-13" / "session-002.tape.jsonl"
    (tmp_path / "days" / "2026-10-14" / "session-001.tape.jsonl").write_bytes(b"")
    assert resume.latest_tape(tmp_path / "days").parent.name == "2026-10-14"
