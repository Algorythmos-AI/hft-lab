"""Sleeve state across a restart of the trader: restored when it can be believed, cold and reported when not.

The trader is the real one on the fake broker's market. A "restart" is a new process start on the same files; a
"kill" is a start after a process that ended without a word. The market is scripted once for the whole of a
test, so a run that was cut and a run that was not are shown the same quotes.
"""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest

from hftlab.calendar import rollover_near
from hftlab.journal import Journal, read, verify
from hftlab.lineup import Lineup
from hftlab.trader import resume, tape
from hftlab.trader.session import mark
from hftlab.trader.tape import ending, housekeep, last_head, parts, read_tape, tape_bytes
from tests.lineup.kit import CHALLENGER, ML, effect, put, register
from tests.lineup.kit import desk as lineup_desk
from tests.trader.kit import NEVER, SECOND, T0, Desk, put_stage, raw_settings

HOUR = 3_600 * SECOND
WHOLE = 120  # seconds of market in a test that compares a cut run with an uncut one
HEAD = "session-001.tape.jsonl"
SMALL = {"part_bytes": 3_000}  # a part every two or three seconds of this market


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
    head = made.day() / HEAD
    (part,) = parts(head)
    whole = head.read_bytes() + part.read_bytes()  # the tape as one file would have held it
    assert json.loads(whole.split(b"\n")[-2]) == ["stop", entry["at_ns"]], "its last word, the state's last moment"
    assert (entry["day"], entry["session"], entry["tape"]) == (made.trader.label, 1, head.name)
    assert entry["tape_end"] == [1, len(whole), hashlib.sha256(whole).hexdigest()] == ending(head)
    assert entry["version"] == resume.VERSION == 2, "an entry tied to a tape of one file is of another version"
    names = ("decisions", "outbox", "risk", "reconcile", "stages", "shadow", "inventory")
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


@pytest.mark.parametrize("paper", [False, True], ids=["shadow only", "limits in force"])
def test_cut_on_either_side_of_a_part_of_the_tape_and_restored_it_decides_as_one_never_stopped(tmp_path, uncut, paper):
    cutting = {"part_bytes": 60_000}  # two cuts of the tape in the two minutes
    whole = a_desk(tmp_path / "whole", paper=paper, edge=cutting)
    whole.run(WHOLE, quotes=False)
    whole.stop()
    cut_into = parts(whole.day() / HEAD)
    assert len(cut_into) == 3 and decided(whole) == uncut[paper], "where the tape is cut decides nothing"
    turned = (json.loads(cut_into[1].read_bytes().split(b"\n")[0])[1] - T0) // SECOND  # when the second part began
    assert 20 < turned < 100
    counts = {}
    for cut in (7, turned - 1, turned, turned + 1, turned + 2, 110):
        for packed in (False, True):
            folder = tmp_path / f"{cut}-{packed}"
            made = a_desk(folder, paper=paper, edge=cutting)
            made.run(cut, quotes=False)
            before = made.trader._states["dc"]
            made.stop()
            head = made.day() / HEAD
            counts[cut] = len(parts(head))
            if packed:  # the housekeeping job came by between the stop and the start: every part is closed
                assert housekeep(folder / "days") == counts[cut] and parts(head)[-1].suffix == ".gz"
            made.start()
            assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}, (cut, packed)
            assert made.trader._states["dc"] == before
            made.run(WHOLE - cut, quotes=False)
            made.stop()
            assert decided(made) == uncut[paper], (cut, packed)
            assert made.of("inherited") == []
            assert all(one.identical and one.stopped for one in made.replay(folder / "scratch")), (cut, packed)
    assert counts[turned - 1] == 1 and counts[turned + 1] == 2, "a state saved before the tape was cut, and one after"
    assert counts[7] == 1 and counts[110] == 3


def test_a_restart_after_the_tape_went_on_to_new_parts_restores_and_so_does_one_after_all_were_compressed(tmp_path):
    made = a_desk(tmp_path, edge=SMALL)
    made.run(23, quotes=False)
    made.stop()
    days, head = tmp_path / "days", made.day() / HEAD
    count, saved = len(parts(head)), entries(tmp_path)[-1]["tape_end"]
    assert count > 5 and saved[0] == count and saved == ending(head)
    made.start()
    assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}, "the last word is in the last part"
    made.run(23, quotes=False)
    made.stop()
    second = made.day() / "session-002.tape.jsonl"
    saved = entries(tmp_path)[-1]["tape_end"]
    assert housekeep(days) == count + len(parts(second)), "both sessions said they stopped: every part is closed"
    assert parts(second)[-1].suffix == ".gz" and ending(second) == saved, "the same tape, to the byte, once unpacked"
    made.start()
    assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}, "compressed since, and nothing was written"
    made.run(5, quotes=False)
    made.stop()
    assert [one.identical for one in made.replay(tmp_path / "scratch")] == [True, True, True]


def test_a_session_the_hard_limit_ended_saves_its_state_and_every_start_after_it_restores_it(tmp_path):
    limits = {**SMALL, "quota_bytes": 60_000, "soft_bytes": 10_000, "reserve_bytes": 40_000}  # hard at 20,000
    made = a_desk(tmp_path, edge=limits)
    for _ in range(WHOLE):
        if made.trader.flat_at_hard_limit:
            break
        made.run(1, quotes=False)
    assert made.trader.flat_at_hard_limit, "a run that sends nothing has nothing to close: it ends at the hard limit"
    made.stop()  # it ends as a session told to stop does: its last word, then its entry
    days, head = tmp_path / "days", made.day() / HEAD
    assert read_tape(head)[-1][0] == "stop" and entries(tmp_path)[-1]["tape_end"] == ending(head)
    assert tape_bytes(days) >= 20_000
    made.start()  # a new process with the tapes still at their hard limit: it is not refused
    assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}, "nothing moved, so the state is believed"
    made.run(2, quotes=False)
    assert made.trader.flat_at_hard_limit and made.trader.summary()["turns"] == 1
    made.stop()
    housekeep(days)  # room is made by compressing; the tapes are the same tapes
    assert tape_bytes(days) < 10_000
    made.start()
    assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}
    made.run(1, quotes=False)
    made.stop()
    assert [e["session"] for e in entries(tmp_path)] == [1, 2, 3]
    assert all(one.identical and one.stopped for one in made.replay(tmp_path / "scratch"))


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


def stopped(tmp_path, **options) -> Desk:
    made = a_desk(tmp_path, **options)
    made.run(23, quotes=False)
    made.stop()
    return made


def add(path, more: bytes) -> None:
    path.write_bytes(path.read_bytes() + more)


def pack(path, *, keep: bool = False, damage: bool = False) -> None:
    """Compress one part as the housekeeping job does; `keep` leaves the plain one beside it, as a stopped job does."""
    packed = gzip.compress(path.read_bytes())
    path.with_name(path.name + ".gz").write_bytes(packed[: len(packed) // 2] if damage else packed)
    if not keep:
        path.unlink()


def flip(path) -> None:
    """Write a part again: the same length, the same last line, one character of one earlier line another."""
    whole = bytearray(path.read_bytes())
    whole[10] ^= 1
    path.write_bytes(bytes(whole))


OPENED = b'["open",1,"x",[]]\n'
NOT_BELIEVED = {
    "a later session's head": lambda day, head, found: (day / "session-002.tape.jsonl").write_bytes(OPENED),
    "a head in a later day's folder": lambda day, head, found: (
        (day.parent / "2099-01-01").mkdir() or (day.parent / "2099-01-01" / HEAD).write_bytes(OPENED)
    ),
    "a line more in the last part": lambda day, head, found: add(found[-1], b'["step",1,"READY",[],null]\n'),
    "a part more: the tape went on": lambda day, head, found: (
        day / f"session-001.part-{len(found) + 1:04d}.2025-10-09.jsonl"
    ).write_bytes(b'["step",1,"READY",[],null]\n'),
    "a part more, and it is empty": lambda day, head, found: (
        day / f"session-001.part-{len(found) + 1:04d}.2025-10-09.jsonl"
    ).write_bytes(b""),
    "the last part cut short": lambda day, head, found: found[-1].write_bytes(found[-1].read_bytes()[:-12]),
    "the last part without its last line": lambda day, head, found: found[-1].write_bytes(
        found[-1].read_bytes().rsplit(b"\n", 2)[0] + b"\n"
    ),
    "the last part gone": lambda day, head, found: found[-1].unlink(),
    "an older part gone": lambda day, head, found: found[1].unlink(),
    "the first part gone": lambda day, head, found: found[0].unlink(),
    "an older part written again": lambda day, head, found: flip(found[1]),
    "an older part written again, then compressed": lambda day, head, found: (flip(found[1]), pack(found[1])),
    "an older part cut short": lambda day, head, found: found[1].write_bytes(found[1].read_bytes()[:-5]),
    "a compressed part that does not unpack": lambda day, head, found: pack(found[1], damage=True),
    "two parts changed places": lambda day, head, found: (
        found[1].rename(day / "x"), found[2].rename(found[1]), (day / "x").rename(found[2])
    ),
    "the head written again": lambda day, head, found: head.write_bytes(head.read_bytes().replace(b"open", b"OPEN", 1)),
    "no head": lambda day, head, found: head.unlink(),
    "no tape at all": lambda day, head, found: [path.unlink() for path in day.glob("session-*")],
}  # fmt: skip
BELIEVED = {
    "nothing done to it": lambda days, found: None,
    "every part compressed, the last among them": lambda days, found: housekeep(days),
    "only the last part compressed": lambda days, found: pack(found[-1]),
    "only the older parts compressed": lambda days, found: [pack(path) for path in found[:-1]],
    "a part there both plain and compressed": lambda days, found: pack(found[1], keep=True),
    "a compressed copy half written beside its part": lambda days, found: (
        found[-1].with_name(found[-1].name + ".gz.partial").write_bytes(b"half")
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(NOT_BELIEVED))
def test_a_state_whose_tape_went_on_lost_a_part_or_was_written_again_is_not_restored(tmp_path, case):
    made = stopped(tmp_path, edge=SMALL)
    head = made.day() / HEAD
    assert len(parts(head)) > 5
    NOT_BELIEVED[case](made.day(), head, parts(head))
    made.start()
    assert state_of(made) == {"restored": [], "cold": {"control": "TAPE", "dc": "TAPE"}}, case
    assert made.pages.fired[-1] == "sleeve-state", "and the owner is told"
    assert made.trader._states["dc"] == made.trader._sleeves[2].initial()


@pytest.mark.parametrize("case", sorted(BELIEVED))
def test_a_state_whose_tape_only_changed_how_it_is_kept_is_restored(tmp_path, case):
    made = stopped(tmp_path, edge=SMALL)
    head = made.day() / HEAD
    saved = ending(head)
    BELIEVED[case](tmp_path / "days", parts(head))
    assert ending(head) == saved == entries(tmp_path)[-1]["tape_end"], "the same bytes, however they are kept"
    made.start()
    assert state_of(made) == {"restored": ["control", "dc"], "cold": {}}, case
    made.stop()
    assert all(one.identical for one in made.replay(tmp_path / "scratch"))


def test_a_state_of_a_tape_in_one_part_is_judged_the_same_way(tmp_path):
    for case, damage in (
        ("a later tape", lambda day, part: (day / "session-002.tape.jsonl").write_bytes(b'["open",1,"x",[]]\n')),
        ("a tape with more on it", lambda day, part: add(part, b'["step",1,"READY"]\n')),
        ("a tape cut short", lambda day, part: part.write_bytes(part.read_bytes()[:-12])),
        ("no part", lambda day, part: part.unlink()),
    ):  # fmt: skip
        made = stopped(tmp_path / case.replace(" ", "-"))
        damage(made.day(), parts(made.day() / HEAD)[0])
        made.start()
        assert state_of(made) == {"restored": [], "cold": {"control": "TAPE", "dc": "TAPE"}}, case
        assert "sleeve-state" in made.pages.fired
    untouched = stopped(tmp_path / "untouched")
    untouched.start()
    assert state_of(untouched)["restored"] == ["control", "dc"], "the same day, with nothing done to it"


def test_an_entry_of_the_version_that_knew_one_file_is_no_state_of_this_one(tmp_path):
    made = stopped(tmp_path)
    (entry,) = entries(tmp_path)
    old = {key: value for key, value in entry.items() if key not in ("type", "i", "prev")} | {"version": 1}
    journal = Journal(tmp_path / resume.FILE)
    journal.open()
    journal.write(resume.KIND, **old)
    journal.close()
    made.start()
    assert state_of(made)["cold"] == {"control": "NOT_A_STATE", "dc": "NOT_A_STATE"}


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


def test_a_state_file_that_cannot_be_read_or_written_stops_neither_a_start_nor_a_clean_stop(tmp_path):
    made = a_desk(tmp_path)
    made.run(10, quotes=False)
    (tmp_path / resume.FILE).mkdir()  # not a file at all: it can be neither read nor appended to
    result = made.stop()
    assert result["turns"] > 0 and made.pages.fired[-1] == "sleeve-state-save", "the session ended, and said so"
    made.start()
    assert state_of(made) == {"restored": [], "cold": {"control": "UNVERIFIED", "dc": "UNVERIFIED"}}
    made.run(10, quotes=False)
    made.stop()
    assert all(one.identical for one in made.replay(tmp_path / "scratch"))


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


# ---- where a tape ends, read as bytes --------------------------------------------------------------------------------


def test_where_a_tape_ends_is_its_parts_its_bytes_and_their_hash_however_they_are_kept_and_nothing_is_parsed(tmp_path):
    head = tmp_path / HEAD
    assert ending(head) == [0, 0, ""], "no tape"
    head.write_bytes(b"")
    assert ending(head) == [0, 0, ""], "an empty head ends nowhere"
    head.write_bytes(b"not json\n")
    assert ending(head) == [0, 9, hashlib.sha256(b"not json\n").hexdigest()], "a head and no part yet"
    one, two = tmp_path / "session-001.part-0001.2026-10-14.jsonl", tmp_path / "session-001.part-0002.2026-10-15.jsonl"
    one.write_bytes(b"nor this\n" * 3)
    two.write_bytes(b"x" * 3_000_000 + b"\nend\n")
    whole = head.read_bytes() + one.read_bytes() + two.read_bytes()
    as_saved = ending(head)
    assert as_saved == [2, len(whole), hashlib.sha256(whole).hexdigest()], "more than one read's worth, no line read"
    (tmp_path / "session-002.part-0001.2026-10-14.jsonl").write_bytes(b"another session's\n")
    (tmp_path / "decisions.jsonl").write_bytes(b"not a tape\n")
    assert ending(head) == as_saved, "its own files and no others"
    pack(one)
    assert ending(head) == as_saved, "a compressed part is the same part"
    pack(two, keep=True)
    assert ending(head) == as_saved, "and so is one that is there twice"
    two.unlink()
    assert ending(head) == as_saved
    (tmp_path / "session-001.part-0003.2026-10-15.jsonl").write_bytes(b"one\ntorn")
    assert ending(head) == [3, len(whole) + 8, ""], "a last line that was being written is no end"
    (tmp_path / "session-001.part-0003.2026-10-15.jsonl").write_bytes(b"")
    assert ending(head) == [3, len(whole), ""], "nor is a part a kill left empty"
    (tmp_path / "session-001.part-0003.2026-10-15.jsonl").rename(tmp_path / "session-001.part-0004.2026-10-15.jsonl")
    assert ending(head)[2] == "", "a part missing from the run: a hole"


def test_the_housekeeping_job_taking_a_part_away_while_the_tape_is_read_is_looked_at_again(tmp_path, monkeypatch):
    made = stopped(tmp_path, edge=SMALL)
    head = made.day() / HEAD
    saved, listed = ending(head), tape.parts
    calls = []

    def and_then_compressed(of):
        found = listed(of)
        if not calls:  # between the listing and the reading, the part is replaced by its compressed copy
            pack(found[1])
        calls.append(1)
        return found

    monkeypatch.setattr(tape, "parts", and_then_compressed)
    assert ending(head) == saved and len(calls) == 2


def test_the_last_head_is_the_highest_number_of_the_latest_day_that_has_one(tmp_path):
    assert last_head(tmp_path / "days") is None
    for day, names in (("2026-10-13", ["session-001.tape.jsonl", "session-002.tape.jsonl"]), ("2026-10-14", [])):
        (tmp_path / "days" / day).mkdir(parents=True)
        for name in names:
            (tmp_path / "days" / day / name).write_bytes(b"")
    (tmp_path / "days" / "2026-10-13" / "session-003.part-0001.2026-10-13.jsonl").write_bytes(b"")
    (tmp_path / "days" / "2026-10-14" / "decisions.jsonl").write_bytes(b"")
    assert last_head(tmp_path / "days") == tmp_path / "days" / "2026-10-13" / "session-002.tape.jsonl"
    (tmp_path / "days" / "2026-10-14" / "session-001.tape.jsonl").write_bytes(b"")
    assert last_head(tmp_path / "days").parent.name == "2026-10-14"
