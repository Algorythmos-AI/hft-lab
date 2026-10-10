"""The hash-chained journal: nothing in it can be changed, dropped, inserted or moved without being caught."""
from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.shadow.journal import GENESIS, Journal, JournalError, JournalState, canonical, read, verify


def written(path, count: int = 5) -> list[bytes]:
    journal = Journal(path)
    journal.open()
    for number in range(count):
        journal.write("fill" if number % 2 else "order", n=number + 10, pair="EUR.USD", price=108_500 + number)
    journal.close()
    return path.read_bytes().splitlines(keepends=True)


def test_each_line_carries_its_own_hash_and_the_hash_of_the_line_before(tmp_path):
    path = tmp_path / "j.jsonl"
    lines = written(path)
    state, entries = read(path)
    assert state.entries == 5 and not state.torn and state.good_bytes == path.stat().st_size
    assert [entry["i"] for entry in entries] == [0, 1, 2, 3, 4] and state.last_event == 14
    assert entries[0]["prev"] == GENESIS
    hashes = [json.loads(line)["hash"] for line in lines]
    assert [entry["prev"] for entry in entries[1:]] == hashes[:-1] and state.last_hash == hashes[-1]
    assert all(line.startswith(b'{"hash":"') and line.endswith(b"}\n") for line in lines)


def test_a_journal_that_does_not_exist_yet_is_an_empty_chain(tmp_path):
    assert verify(tmp_path / "none.jsonl") == JournalState()
    journal = Journal(tmp_path / "deep" / "er" / "j.jsonl")
    assert journal.open() == JournalState() and journal.entries == 0 and journal.last_hash == GENESIS
    journal.close()
    journal.close()
    with pytest.raises(JournalError, match="not open"):
        journal.write("order")


def test_the_same_entries_always_make_the_same_bytes(tmp_path):
    assert written(tmp_path / "a.jsonl") == written(tmp_path / "b.jsonl")
    assert written(tmp_path / "c.jsonl", 4) == written(tmp_path / "a2.jsonl")[:4]


def change(line: bytes, **fields) -> bytes:
    return canonical({**json.loads(line), **fields}).encode() + b"\n"


@pytest.mark.parametrize(
    ("damage", "complaint"),
    [
        (lambda lines: [lines[0], change(lines[1], price=1), *lines[2:]], "line 2: the line does not match its own"),
        (lambda lines: [lines[0], *lines[2:]], "line 2: the line does not follow the one before it"),
        (lambda lines: [lines[0], lines[2], lines[1], *lines[3:]], "line 2: the line does not follow"),
        (lambda lines: [*lines[:2], lines[1], *lines[2:]], "line 3: the line does not follow"),
        (lambda lines: lines[1:], "line 1: the line does not follow"),
        (lambda lines: [*lines[:4], change(lines[4], n=99)], "line 5: the line does not match its own hash"),
        (lambda lines: [*lines[:2], b"not json\n", *lines[2:]], "line 3: not JSON"),
        (lambda lines: [*lines[:2], b"[1]\n", *lines[2:]], "line 3: not a mapping"),
        (lambda lines: [*lines, lines[0]], "line 6: the line does not follow"),
    ],
)
def test_any_change_to_a_whole_line_is_caught_at_that_line(tmp_path, damage, complaint):
    path = tmp_path / "j.jsonl"
    path.write_bytes(b"".join(damage(written(path))))
    with pytest.raises(JournalError, match=complaint):
        verify(path)
    with pytest.raises(JournalError):
        Journal(path).open()


def test_a_line_rehashed_after_a_change_still_breaks_the_chain_behind_it(tmp_path):
    path = tmp_path / "j.jsonl"
    lines = written(path)
    forged = Journal(tmp_path / "forged.jsonl")
    forged.open()
    forged.write("order", n=10, pair="EUR.USD", price=1)  # the first entry, altered and correctly hashed
    forged.close()
    path.write_bytes((tmp_path / "forged.jsonl").read_bytes() + b"".join(lines[1:]))
    with pytest.raises(JournalError, match="line 2: the line does not follow"):
        verify(path)


def test_a_caller_cannot_choose_a_lines_number_or_its_place_in_the_chain(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    journal.open()
    journal.write("order", i=7, prev="f" * 64)
    journal.close()
    (entry,) = read(tmp_path / "j.jsonl")[1]
    assert (entry["i"], entry["prev"]) == (0, GENESIS)
    lines = written(tmp_path / "k.jsonl")
    (tmp_path / "k.jsonl").write_bytes(b"".join([*lines[:2], change(lines[2], i=9), *lines[3:]]))
    with pytest.raises(JournalError, match="line 3"):
        verify(tmp_path / "k.jsonl")


def test_a_killed_writer_leaves_a_torn_last_line_that_is_cut_off_at_every_possible_length(tmp_path):
    whole = b"".join(written(tmp_path / "whole.jsonl"))
    lines = whole.splitlines(keepends=True)
    kept = len(whole) - len(lines[-1])
    for cut in range(kept, len(whole)):
        path = tmp_path / f"cut{cut}.jsonl"
        path.write_bytes(whole[:cut])
        state = verify(path)
        assert state.entries == 4 and state.good_bytes == kept and state.torn == (cut > kept), cut
        journal = Journal(path)
        assert journal.open().entries == 4
        journal.write("order", n=14, pair="EUR.USD", price=108_504)  # the entry that was lost, written again
        journal.close()
        assert path.read_bytes() == whole, cut


def test_reopening_continues_the_chain(tmp_path):
    path = tmp_path / "j.jsonl"
    written(path, 3)
    journal = Journal(path, fsync=True)
    assert journal.open().last_event == 12
    journal.write("start", session=2)
    journal.flush()
    journal.flush()
    assert verify(path).entries == 4, "flushed entries are in the file before it is closed"
    journal.close()
    state, entries = read(path)
    third = json.loads(written(tmp_path / "x", 3)[2])
    assert (state.entries, state.sessions) == (4, 1) and entries[3]["prev"] == third["hash"]


@pytest.mark.parametrize("value", [1.5, 1e9, b"bytes", {1: "key"}, {"deep": [1, {"x": 0.1}]}, object()])
def test_only_integers_and_text_are_kept(tmp_path, value):
    journal = Journal(tmp_path / "j.jsonl")
    journal.open()
    with pytest.raises(JournalError):
        journal.write("order", value=value)
    journal.close()
    assert verify(tmp_path / "j.jsonl").entries == 0


def test_a_float_written_into_the_file_by_other_means_does_not_verify(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"hash":"x","i":0,"prev":"' + GENESIS + '","price":1.5,"type":"fill"}\n')
    with pytest.raises(JournalError, match="integers and text only"):
        verify(path)


PLAIN = st.recursive(
    st.none() | st.integers(-(10**30), 10**30) | st.text(max_size=12) | st.booleans(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=8,
)


@given(entries=st.lists(st.dictionaries(st.sampled_from(["a", "b", "n", "pair"]), PLAIN, max_size=4), max_size=8))
def test_whatever_is_written_reads_back_equal_and_verifies(tmp_path_factory, entries):
    path = tmp_path_factory.mktemp("journal") / "j.jsonl"
    journal = Journal(path)
    journal.open()
    for fields in entries:
        journal.write("note", **fields)
    journal.close()
    state, found = read(path)
    assert state.entries == len(entries)
    assert [{k: v for k, v in e.items() if k not in ("i", "prev", "type")} for e in found] == entries
