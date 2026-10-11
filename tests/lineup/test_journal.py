"""The lineup journal: every change is a line that cannot be altered, and the lineup in force is the last one."""
from __future__ import annotations

import pytest

from hftlab import lineup
from hftlab.lineup import Lineup, LineupError, Named

A, B, C, D = (Named(letter * 64, digit * 64) for letter, digit in (("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")))


def test_an_absent_journal_is_the_empty_lineup(tmp_path):
    found = lineup.read(tmp_path / lineup.FILE)
    assert found == Lineup() and found.champion is None and not (tmp_path / lineup.FILE).exists()


def test_a_candidate_serves_nowhere_until_a_decision_makes_it_a_challenger():
    registered = Lineup().registered(A)
    assert registered.candidates == (A,) and registered.challengers == () and registered.champion is None
    assert registered.registered(A) is registered, "the same model is registered once"
    assert registered.kept() == Lineup(challengers=(A,)), "the decision that keeps moves it into the shadow places"
    assert Lineup(challengers=(A,), candidates=(B,)).kept() == Lineup(challengers=(B,)), "a week, and no more"


def test_a_promotion_keeps_the_champion_it_replaces_and_a_rollback_restores_it():
    week = Lineup(champion=A, challengers=(B,), candidates=(C,))
    promoted = week.promoted(B)
    assert promoted == Lineup(champion=B, previous=(A,), challengers=(C,))
    assert promoted.rolled_back() == Lineup(champion=A), "the champion before it, and the bad one is gone"
    twice = promoted.registered(D).promoted(C)
    assert twice.challengers == (D,)
    assert twice.previous == (A, B) and twice.rolled_back().champion == B and twice.rolled_back().previous == (A,)
    assert Lineup(champion=A).rolled_back() == Lineup(), "with none before it the place is empty: the sleeve is idle"
    with pytest.raises(LineupError, match="ran its week as a challenger"):
        week.promoted(C)
    with pytest.raises(LineupError, match="no champion"):
        Lineup().rolled_back()


def test_the_journal_holds_every_change_and_the_last_line_is_in_force(tmp_path):
    path = tmp_path / lineup.FILE
    first = lineup.append(path, Lineup().registered(A), action=lineup.REGISTER, at_ns=1, report=A.report)
    second = lineup.append(path, first.kept(), action=lineup.KEEP, at_ns=2, reasons=("rule_not_registered",))
    third = lineup.append(
        path, second.promoted(A), action=lineup.PROMOTE, at_ns=3, decision="HFT-DEC-0007", week="2025-10-17",
        report="9" * 64, evidence={"trades": 30},
    )  # fmt: skip
    assert lineup.read(path) == third and third.champion == A and third.entries == 3 and len(third.head) == 64
    lines = lineup.history(path)
    assert [line["action"] for line in lines] == ["register", "keep", "promote"]
    assert lines[2]["decision"] == "HFT-DEC-0007" and lines[2]["report"] == "9" * 64 and lines[2]["at_ns"] == 3
    assert lines[2]["champion"] == {"model": "a" * 64, "report": "1" * 64}
    assert lines[1]["reasons"] == ["rule_not_registered"]
    with pytest.raises(LineupError, match="changes by one of"):
        lineup.append(path, third, action="swap", at_ns=4)


def test_a_journal_that_was_edited_or_cut_short_is_refused_and_never_repaired(tmp_path):
    path = tmp_path / lineup.FILE
    lineup.append(path, Lineup().registered(A).kept(), action=lineup.KEEP, at_ns=1)
    whole = path.read_bytes()
    path.write_bytes(whole.replace(b"a" * 64, b"b" * 64))
    with pytest.raises(LineupError, match="does not verify"):
        lineup.read(path)
    path.write_bytes(whole + whole[: len(whole) // 2])
    with pytest.raises(LineupError, match="middle of a line"):
        lineup.read(path)
    path.write_bytes(whole)
    assert lineup.read(path).challengers == (A,)


def test_a_model_is_named_by_two_hashes_and_by_nothing_else():
    for model, report in (("latest", "1" * 64), ("a" * 64, "report.json"), ("A" * 64, "1" * 64), ("a" * 63, "1" * 64)):
        with pytest.raises(LineupError, match="SHA-256"):
            Named(model, report)
