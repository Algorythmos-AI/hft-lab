"""Determinism: a recorded day of the trader replays to the same decision journal, byte for byte (ADR 0005).

The day is synthetic and runs against the fake broker, with the real risk gate, order manager and reconciler.
The replay has no broker at all: the tape stands in for it.
"""
from __future__ import annotations

import pytest

from hftlab.calendar import Calendar
from hftlab.journal import JournalError, verify
from hftlab.trader import TapeError
from hftlab.trader.replay import replay_day
from hftlab.trader.tape import read_tape
from hftlab.venues.fake import FakeConfig
from tests.trader.kit import DIGEST, MS, SECOND, T0, Desk, put_stage

CHUNKS = {"fill_chunks": 2, "chunk_gap_ns": 400 * MS}  # every order fills in two pieces, 400 ms apart


def a_day_with_a_disconnect_in_a_partly_filled_order(folder, probe):
    """A minute of paper trading in which the link drops between the two fills of one order, for five seconds."""
    put_stage(probe, "control", "min_size")
    first = Desk(probe, FakeConfig(**CHUNKS))
    first.run(30)
    partial = [e for e in first.outbox() if e.get("to") == "PARTIALLY_FILLED" and e["at_ns"] > T0 + 10 * SECOND]
    down = partial[0]["at_ns"] + 100 * MS
    put_stage(folder, "control", "min_size")
    desk = Desk(folder, FakeConfig(outages=((down, down + 5 * SECOND),), **CHUNKS))
    desk.run(60)
    return desk


def test_a_day_with_a_disconnect_and_partial_fills_replays_to_the_same_decisions_byte_for_byte(tmp_path):
    desk = a_day_with_a_disconnect_in_a_partly_filled_order(tmp_path / "live", tmp_path / "probe")
    desk.stop()
    stages = [(e["before"], e["to"], e["cause"]) for e in desk.outbox() if e["type"] == "stage"]
    assert ("PARTIALLY_FILLED", "UNKNOWN", "CONNECTION_LOST") in stages, "the link dropped between an order's two fills"
    assert ("UNKNOWN", "FILLED", "FILL") in stages, "and the second was found by asking the venue afterwards"
    assert [e["state"] for e in desk.of("connection")].count("DISCONNECTED") >= 1
    assert len(desk.of("broker_fill")) > 40 and len(desk.of("verdict")) > 20
    live = (desk.day() / "decisions.jsonl").read_bytes()

    (replayed,) = desk.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped and replayed.live_bytes == len(live) > 100_000
    again = (tmp_path / "scratch" / "session-001" / "days" / desk.trader.label / "decisions.jsonl").read_bytes()
    assert again == live, "every intent, verdict, order and fill, with its hash"
    for name in ("outbox", "risk", "reconcile", "stages", "shadow"):  # and every journal a decision rests on
        there, here = tmp_path / "scratch" / "session-001" / f"{name}.jsonl", tmp_path / "live" / f"{name}.jsonl"
        assert there.read_bytes() == here.read_bytes(), name


def test_the_replay_needs_no_broker_and_touches_none_of_the_days_own_files(tmp_path):
    desk = a_day_with_a_disconnect_in_a_partly_filled_order(tmp_path / "live", tmp_path / "probe")
    desk.stop()
    before = {path: path.read_bytes() for path in (tmp_path / "live").rglob("*") if path.is_file()}
    sent = len(desk.market._orders)
    desk.market.submit = desk.market.request_cancel = None  # the broker is not there any more
    assert all(one.identical for one in desk.replay(tmp_path / "scratch"))
    assert {path: path.read_bytes() for path in (tmp_path / "live").rglob("*") if path.is_file()} == before
    assert len(desk.market._orders) == sent and desk.pages.fired == [], "nothing was sent, and nobody was paged"


def test_every_session_of_a_day_is_replayed_from_where_it_began(tmp_path):
    put_stage(tmp_path / "live", "control", "min_size")
    desk = Desk(tmp_path / "live")
    desk.run(20)
    desk.stop()
    desk.start()
    desk.run(20)
    desk.stop()
    found = desk.replay(tmp_path / "scratch")
    assert [one.tape for one in found] == ["session-001.tape.jsonl", "session-002.tape.jsonl"]
    assert all(one.identical and one.stopped for one in found)
    assert sum(one.live_bytes for one in found) == verify(desk.day() / "decisions.jsonl").good_bytes


def test_a_session_that_was_killed_is_reproduced_as_far_as_it_got(tmp_path):
    put_stage(tmp_path / "live", "control", "min_size")
    desk = Desk(tmp_path / "live")
    desk.run(20)
    desk.restart()  # killed: no stop, no result
    desk.run(20)
    desk.stop()
    killed, stopped = desk.replay(tmp_path / "scratch")
    assert (killed.stopped, killed.identical) == (False, True) and killed.replayed_bytes >= killed.live_bytes > 0
    assert (stopped.stopped, stopped.identical) == (True, True)


def test_a_tape_cut_off_in_the_middle_of_a_turn_ends_the_replay_there_and_not_in_an_error(tmp_path):
    put_stage(tmp_path / "live", "control", "min_size")
    desk = Desk(tmp_path / "live")
    desk.run(20)
    desk.trader.abandon()
    tape = desk.day() / "session-001.tape.jsonl"
    rows = tape.read_bytes().split(b"\n")
    last_ask = max(n for n, row in enumerate(rows) if row.startswith(b'["sent"'))
    tape.write_bytes(b"\n".join(rows[:last_ask]) + b'\n["sen')  # the process died before it could say how a send went
    assert read_tape(tape)[-1][0] != "sent"
    (found,) = desk.replay(tmp_path / "scratch")
    assert not found.stopped and found.replayed_bytes > 0


def test_the_same_day_twice_writes_the_same_bytes(tmp_path):
    days = []
    for name in ("one", "two"):
        put_stage(tmp_path / name, "control", "min_size")
        desk = Desk(tmp_path / name, FakeConfig(**CHUNKS))
        desk.run(40)
        desk.stop()
        days.append((desk.day() / "decisions.jsonl").read_bytes())
    assert days[0] == days[1] and len(days[0]) > 50_000


def test_a_day_is_not_compared_under_other_settings_than_it_ran_with(tmp_path):
    desk = Desk(tmp_path / "live", paper=False)
    desk.run(10)
    desk.stop()
    with pytest.raises(TapeError, match="other settings"):
        replay_day(
            desk.day(), tmp_path / "scratch", desk.settings, model=desk.costs, calendar=Calendar(), risk=None,
            config_digest="another",
        )
    assert replay_day(
        desk.day(), tmp_path / "scratch", desk.settings, model=desk.costs, calendar=Calendar(), risk=None,
        config_digest=DIGEST,
    )[0].identical


def test_a_journal_that_was_changed_since_the_session_began_is_refused(tmp_path):
    put_stage(tmp_path / "live", "control", "min_size")
    desk = Desk(tmp_path / "live")
    desk.run(10)
    desk.stop()
    stages = tmp_path / "live" / "stages.jsonl"
    stages.write_bytes(stages.read_bytes().replace(b"HFT-DEC-9999", b"HFT-DEC-0000", 1))
    with pytest.raises(JournalError, match="does not match its own hash"):
        desk.replay(tmp_path / "scratch")
