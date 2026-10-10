"""The commit-message rule (AGENTS.md): no co-author trailer and no assistant or tool name."""
from __future__ import annotations

import hashlib
import re

import pytest

from tests.support import ROOT
from tools import commitmsg

DENIED = frozenset({hashlib.sha256(b"wombat").hexdigest()})


@pytest.mark.parametrize(
    "message",
    [
        "Add the lock\n\nCo-Authored-By: Someone <someone@example.invalid>\n",
        "Add the lock\n\nco-authored-by: someone\n",
        "Add the lock\n\n  CO-AUTHORED-BY:someone\n",
    ],
)
def test_a_co_author_trailer_is_refused(message):
    assert commitmsg.violations(message, DENIED) == ["it carries a Co-Authored-By trailer"]


@pytest.mark.parametrize(
    "message", ["Written with Wombat", "wombat: add the lock", "Add the lock (WOMBAT-4)", "x\n\nvia wombat."]
)
def test_a_refused_name_is_found_whatever_its_case_or_punctuation(message):
    assert commitmsg.violations(message, DENIED) == ["it names 'wombat'"]


@pytest.mark.parametrize(
    "message", ["Add the paper-only lock", "wombats are fine, so is awombat", "Co-author rules documented", ""]
)
def test_an_ordinary_message_passes(message):
    assert commitmsg.violations(message, DENIED) == []


def test_git_comment_lines_and_the_diff_below_the_scissors_are_not_the_message():
    message = (
        "Add the lock\n"
        "# Please enter the commit message. wombat\n"
        "# ------------------------ >8 ------------------------\n"
        "+Co-Authored-By: a line of the diff shown by `git commit -v`\n"
    )
    assert commitmsg.violations(message, DENIED) == []
    assert commitmsg.body(message) == "Add the lock"


def test_each_refused_name_is_reported_once_in_order():
    assert commitmsg.denied_words("Wombat and wombat, then emu", DENIED | {hashlib.sha256(b"emu").hexdigest()}) == [
        "wombat",
        "emu",
    ]


def test_the_shipped_list_is_digests_only_and_refuses_a_real_name():
    assert commitmsg.DENIED_SHA256 and all(re.fullmatch(r"[0-9a-f]{64}", d) for d in commitmsg.DENIED_SHA256)
    # The one such name this repository cannot avoid is the stem of the file that points at AGENTS.md.
    pointer = next(p for p in ROOT.glob("*.md") if p.read_text().strip() == "@AGENTS.md")
    assert commitmsg.violations(f"Update {pointer.name}") == [f"it names {pointer.stem.lower()!r}"]
    assert commitmsg.violations("Add the paper-only lock and its isolation tests") == []


def test_the_command_exits_non_zero_on_a_refused_message(tmp_path, capsys):
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_text("Add the lock\n")
    bad.write_text("Add the lock\n\nCo-Authored-By: x\n")
    assert commitmsg.main([str(good)]) == 0
    assert commitmsg.main([str(bad)]) == 1
    assert "refused, it carries a Co-Authored-By trailer" in capsys.readouterr().err
    assert commitmsg.main([]) == 2
