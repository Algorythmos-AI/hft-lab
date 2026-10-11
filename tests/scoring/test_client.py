"""The trader's side of the pipe: the same decisions as in process, and no failure of a child that reaches the caller.

Every child here is a real process. A hung one is a child that was sent SIGSTOP, a dead one a child that was
sent SIGKILL: nothing in the child is switched on for a test.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

import pytest

from hftlab.core import SimClock
from hftlab.features import FeatureRow
from hftlab.models import scorer_for
from hftlab.scoring import client
from hftlab.scoring.client import ProcessScorer, ScorerRefused, Times
from tests.scoring.kit import PATIENT, QUICK, Missed, ready, ridge, rows, stand_in, started, trees, until
from tests.support import ROOT
from tests.venues.kit import MS, T0

FIRST, LAST = QUICK.restart_first_ns, QUICK.restart_last_ns


def gone(pid: int) -> bool:
    """Has this process ended? (A killed child is reaped by its scorer; until then it is still listed.)"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# ---- the same decisions ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("make", [ridge, trees])
def test_a_scorer_process_gives_the_in_process_decision_on_every_one_of_thousands_of_rows(tmp_path, make):
    model = make()
    scorer, _, missed = started(tmp_path, model)
    inside = scorer_for(model)
    shown = rows(4_000)
    try:
        theirs = [scorer.score(row) for row in shown]
    finally:
        scorer.close()
    ours = [inside.score(row) for row in shown]
    assert theirs == ours and missed.rows == [], "row for row: the side, the whole millionths, the model's hash"
    sides = [found.side.value if found else None for found in ours]
    assert min(sides.count("BUY"), sides.count("SELL"), sides.count(None)) > 100, "each answer, hundreds of times"
    assert scorer.model is model and scorer.starts == 1 and scorer.failures == 0


def test_numbers_of_any_size_cross_the_pipe_exactly(tmp_path):
    model = ridge()
    scorer, _, missed = started(tmp_path, model)
    inside = scorer_for(model)
    wide = [
        FeatureRow("EUR.USD", n, T0, tuple((-1) ** k * 10 ** (n * 3) + k for k in range(10)))
        for n in range(1, 40)
    ]
    try:
        assert [scorer.score(row) for row in wide] == [inside.score(row) for row in wide] and missed.rows == []
    finally:
        scorer.close()
    assert max(abs(found.expected_micro) for row in wide if (found := inside.score(row))) > 10**60


# ---- a child that fails ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", [signal.SIGSTOP, signal.SIGKILL])
def test_a_hung_or_a_killed_child_costs_its_rows_and_a_new_one_answers_after_the_back_off(tmp_path, how):
    model = trees()
    scorer, clock, missed = started(tmp_path, model, QUICK)
    inside = scorer_for(model)
    shown = rows(60)
    try:
        assert [scorer.score(row) for row in shown[:10]] == [inside.score(row) for row in shown[:10]]
        first = scorer.pid
        os.kill(first, how)
        began = time.monotonic()
        assert scorer.score(shown[10]) is None, "the caller gets an abstention, never an exception"
        assert time.monotonic() - began < 5, "and gets it within the timeout, not when the child comes back"
        why = missed.rows[-1][1]
        assert why == "TIMEOUT" if how == signal.SIGSTOP else why in ("CLOSED", "NOT_SENT")
        assert scorer.health == "down" and scorer.pid is None and scorer.failures == 1 and scorer.last_failure == why
        assert until(lambda: scorer.tend() or gone(first)), "the child that failed was killed, hung or not"

        began = time.monotonic()
        for row in shown[11:30]:
            assert scorer.score(row) is None
        assert time.monotonic() - began < 1 and scorer.starts == 1, "while it is down nothing is waited for or started"
        assert missed.rows[1:] == [(row.n, "DOWN") for row in shown[11:30]], "each row asked once, and written down"

        clock.advance(FIRST - 1)
        scorer.tend()
        assert scorer.health == "down" and scorer.retry_at_ns == clock.now_ns() + 1
        clock.advance(1)
        assert scorer.score(shown[30]) is None and missed.rows[-1] == (shown[30].n, "STARTING")
        assert scorer.starts == 2 and scorer.pid not in (None, first), "started on the turn it was due, not waited for"
        assert until(lambda: ready(scorer))
        assert [scorer.score(row) for row in shown[31:]] == [inside.score(row) for row in shown[31:]], "and it is back"
        assert len(missed.rows) == 21 and scorer.failures == 1
    finally:
        scorer.close()


def test_the_wait_before_a_restart_doubles_to_its_limit_and_is_the_first_wait_again_after_a_row_was_scored(tmp_path):
    model = trees()
    row = rows(1)[0]
    scorer, clock, missed = started(tmp_path, model, QUICK, command=stand_in(tmp_path, "mute", model))
    waits = []
    try:
        for _ in range(6):
            assert until(lambda: ready(scorer))
            assert scorer.score(row) is None and missed.reasons[-1] == "TIMEOUT"
            waits.append(scorer.retry_at_ns - clock.now_ns())
            clock.advance(waits[-1])
    finally:
        scorer.close()
    assert waits == [FIRST, 2 * FIRST, 4 * FIRST, LAST, LAST, LAST] and scorer.starts == 6

    real, clock, missed = started(tmp_path / "real", model, QUICK)
    try:
        for expected in (FIRST, 2 * FIRST):
            os.kill(real.pid, signal.SIGKILL)
            assert real.score(row) is None and real.retry_at_ns - clock.now_ns() == expected
            clock.advance(expected)
            assert until(lambda: ready(real))
        assert real.score(row) == scorer_for(model).score(row)
        os.kill(real.pid, signal.SIGKILL)
        assert real.score(row) is None and real.retry_at_ns - clock.now_ns() == FIRST, "a scored row starts it over"
    finally:
        real.close()


@pytest.mark.parametrize(
    ("how", "why"),
    [
        ("garbage", "MALFORMED"), ("a_list", "MALFORMED"), ("a_float", "MALFORMED"), ("another_key", "MALFORMED"),
        ("no_side", "MALFORMED"), ("another_version", "MALFORMED"), ("too_long", "MALFORMED"),
        ("another_model", "OTHER_MODEL"), ("refuses", "ROW_REFUSED"), ("leaves", "CLOSED"), ("mute", "TIMEOUT"),
    ],
)  # fmt: skip
def test_a_reply_that_is_not_one_is_an_abstention_the_child_is_killed_and_nothing_is_raised(tmp_path, how, why):
    model = trees()
    scorer, clock, missed = started(tmp_path, model, QUICK, command=stand_in(tmp_path, how, model))
    try:
        pid = scorer.pid
        began = time.monotonic()
        assert scorer.score(rows(1)[0]) is None and missed.rows == [(1, why)]
        assert time.monotonic() - began < 5, "an over-long reply is refused at its head; nobody waits for the rest"
        assert scorer.health == "down" and scorer.failures == 1
        assert until(lambda: scorer.tend() or gone(pid)), "the child that answered so was killed"
        clock.advance(FIRST)
        assert until(lambda: ready(scorer)) and scorer.starts == 2, "and the next one is started like any other"
    finally:
        scorer.close()


def test_a_row_too_long_for_one_message_is_not_sent_and_the_child_is_not_blamed_for_it(tmp_path):
    model = ridge()
    scorer, _, missed = started(tmp_path, model)
    try:
        pid = scorer.pid
        long = FeatureRow("EUR.USD", 1, T0, tuple(10**4_000 + k for k in range(20)))  # 80,000 digits: too long
        vast = FeatureRow("EUR.USD", 2, T0, tuple(10**7_000 + k for k in range(10)))  # a number nothing writes out
        broken = FeatureRow("EUR.USD", 3, T0, (1.5,) * 10)
        assert [scorer.score(row) for row in (long, vast, broken)] == [None, None, None]
        assert missed.rows == [(1, "NOT_A_MESSAGE"), (2, "NOT_A_MESSAGE"), (3, "NOT_A_MESSAGE")]
        assert scorer.health == "ready" and scorer.pid == pid and scorer.failures == 0
        row = rows(2)[1]
        assert scorer.score(row) == scorer_for(model).score(row)
    finally:
        scorer.close()


# ---- the lineup's model, or none -------------------------------------------------------------------------------------


def test_a_child_that_holds_another_model_than_the_lineups_is_refused_at_the_start(tmp_path):
    model, other = trees(), ridge()
    missed = Missed()
    with pytest.raises(ScorerRefused, match=f"does not hold model {model.content_hash[:12]}"):
        started(tmp_path / "a", model, command=stand_in(tmp_path, "another_model_loaded", model))
    swapped = tmp_path / "swapped"
    swapped.mkdir()
    (swapped / f"{model.content_hash}.json").write_bytes(other.data())  # the file is another model than its name says
    with pytest.raises(ScorerRefused):
        ProcessScorer(model, swapped, PATIENT, clock=SimClock(T0), missed=missed)
    with pytest.raises(ScorerRefused):
        ProcessScorer(model, tmp_path / "empty", PATIENT, clock=SimClock(T0), missed=missed)
    assert missed.rows == []


def test_a_restarted_child_that_holds_another_model_is_refused_for_good_and_every_row_is_abstained_on(tmp_path):
    model, other = trees(), ridge()
    scorer, clock, missed = started(tmp_path, model, QUICK)
    row = rows(1)[0]
    try:
        assert scorer.score(row) == scorer_for(model).score(row)
        (tmp_path / f"{model.content_hash}.json").unlink()
        (tmp_path / f"{model.content_hash}.json").write_bytes(other.data())  # replaced behind the trader's back
        os.kill(scorer.pid, signal.SIGKILL)
        assert scorer.score(row) is None
        clock.advance(FIRST)
        assert until(lambda: scorer.tend() or scorer.health == "refused")
        for _ in range(3):
            clock.advance(LAST)
            assert scorer.score(row) is None and missed.reasons[-1] == "REFUSED"
        assert scorer.starts == 2 and scorer.pid is None, "nothing is started again: it would ask the same file"
    finally:
        scorer.close()


def test_a_child_too_slow_to_start_is_not_a_refusal_it_is_down_and_is_started_again(tmp_path):
    model = trees()
    slow = Times(timeout_ns=100 * MS, start_ns=200 * MS, restart_first_ns=FIRST, restart_last_ns=LAST)
    scorer, clock, missed = started(tmp_path, model, slow, command=stand_in(tmp_path, "asleep", model))
    try:
        assert scorer.health == "down" and scorer.last_failure == "TIMEOUT" and scorer.retry_at_ns == T0 + FIRST
        clock.advance(FIRST)
        assert scorer.score(rows(1)[0]) is None and scorer.health == "starting" and missed.reasons == ["STARTING"]
        clock.advance(slow.start_ns - 1)
        scorer.tend()
        assert scorer.health == "starting", "a start is given its time, on the clock that was handed in"
        clock.advance(1)
        scorer.tend()
        assert scorer.health == "down" and scorer.retry_at_ns - clock.now_ns() == 2 * FIRST
    finally:
        scorer.close()


def test_a_command_that_cannot_be_started_is_a_failure_like_any_other(tmp_path):
    scorer, clock, _ = started(tmp_path, trees(), QUICK, command=[str(tmp_path / "no-such-program")])
    assert scorer.health == "down" and scorer.failures == 1 and scorer.retry_at_ns == clock.now_ns() + FIRST
    scorer.close()


# ---- the end ---------------------------------------------------------------------------------------------------------


def test_closing_ends_the_childs_input_and_the_child_leaves_by_itself(tmp_path):
    scorer, _, _ = started(tmp_path, trees())
    process = scorer._child.process
    scorer.close()
    assert process.returncode == 0, "it read the end of its input and left: it was not killed"
    assert scorer.pid is None and scorer.health == "down"
    scorer.close()


def test_a_child_whose_trader_is_gone_leaves_by_itself(tmp_path):
    scorer, _, _ = started(tmp_path, trees())
    process = scorer._child.process
    process.stdin.close()  # what the child sees when the process that started it dies
    assert process.wait(timeout=20) == 0
    assert scorer.score(rows(1)[0]) is None, "and a scorer whose child left abstains like any other"
    scorer.close()


def test_a_child_is_started_with_the_packages_place_and_none_of_the_traders_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_A_SECRET_OF_THE_TRADER", "not for the child")
    model = trees()
    script, told = tmp_path / "tell.py", tmp_path / "told.json"
    script.write_text(
        "import json, os, sys\nfrom hftlab.scoring import wire\n"
        f"open({str(told)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        "wire.read_frame(sys.stdin.buffer.read)\n"
        f"sys.stdout.buffer.write(wire.frame(wire.loaded({model.content_hash!r})))\nsys.stdout.flush()\n"
        "sys.stdin.buffer.read()\n"
    )
    scorer, _, _ = started(tmp_path / "m", model, command=[sys.executable, str(script)])
    scorer.close()
    seen = json.loads(told.read_text())
    assert seen["PYTHONPATH"] == str(ROOT / "src") and seen["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "HFT_A_SECRET_OF_THE_TRADER" not in seen and "HOME" not in seen and "PATH" not in seen
    assert client._environment() == {"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
