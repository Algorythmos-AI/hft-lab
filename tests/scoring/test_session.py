"""A scorer process under the trader: the same day as in process, a failure that stops nothing, and a replay without it.

The trader is the real one, on the fake broker's market, and the child is a real process. Its clock is the
session's, which is simulated here and runs far ahead of a real process: where a test needs a child to have
started, it waits for it in real time.
"""
from __future__ import annotations

import os
import signal

import pytest

from hftlab.calendar import Calendar
from hftlab.journal import JournalError, read
from hftlab.lineup import Lineup
from hftlab.lineup.serve import make
from hftlab.scoring import client
from hftlab.scoring.client import ScorerRefused, Times
from hftlab.scoring.record import beside, recorded
from hftlab.strategy import IdleSleeve, MlSleeve
from hftlab.trader.config import parse
from tests.lineup.kit import CHALLENGER, ML, effect, place, put, register, settings
from tests.scoring.kit import until
from tests.trader.kit import Desk
from tests.venues.kit import MS, SECOND

TIMES = Times(timeout_ns=300 * MS, start_ns=3_600 * SECOND, restart_first_ns=SECOND, restart_last_ns=4 * SECOND)
PATIENT = Times(timeout_ns=20 * SECOND, start_ns=3_600 * SECOND, restart_first_ns=SECOND, restart_last_ns=4 * SECOND)


@pytest.fixture(scope="module")
def kept(tmp_path_factory):
    registry = tmp_path_factory.mktemp("registry")
    return registry, register(registry, 1), register(registry, 3)


def places(registry, scorer: str) -> dict:
    """The trader's settings with both ML places scored this way."""
    raw = settings(registry)
    raw["sleeves"] = [*raw["sleeves"][:2], place(ML, "champion", scorer=scorer),
                      place(CHALLENGER, "challenger-1", scorer=scorer)]  # fmt: skip
    return raw


def desk(folder, registry, scorer: str = "process", times: Times | None = TIMES) -> Desk:
    return Desk(folder, paper=False, settings=places(registry, scorer), scorer_times=times)


def no_child(monkeypatch) -> None:
    """From here on, starting a process is an error: what a replay must never do."""

    def refuse(*args, **kwargs):
        raise AssertionError("a replay started a scorer process")

    monkeypatch.setattr(client.subprocess, "Popen", refuse)


def ready(made: Desk) -> bool:
    made.trader._scorers.tend()
    return set(made.trader._scorers.health().values()) == {"ready"}


# ---- the same day ----------------------------------------------------------------------------------------------------


def test_a_day_scored_in_a_process_is_the_day_scored_in_the_trader_byte_for_byte_and_replays_with_no_child(
    kept, tmp_path, monkeypatch
):
    registry, a, b = kept
    journals = {}
    for scorer in ("in_process", "process"):
        folder = tmp_path / scorer
        put(folder, Lineup(champion=a, challengers=(b,)))
        made = desk(folder, registry, scorer, PATIENT)
        assert set(made.trader._scorers.health()) == ({ML, CHALLENGER} if scorer == "process" else set())
        made.run(effect(made, 7, 20), quotes=False)
        made.stop()
        journals[scorer] = (made.day() / "decisions.jsonl").read_bytes()
    assert journals["process"] == journals["in_process"] and len(journals["process"]) > 10_000
    entries = [e for e in made.of("decision") if e["leg"] == "entry" and e["sleeve"] in (ML, CHALLENGER)]
    assert len(entries) > 20 and made.of("abstained") == [], "the models traded, and no row was lost"
    assert not list(made.day().glob("*.abstained.jsonl")), "a day without a failure leaves no record of one"
    assert made.trader._scorers.health() == {}, "and its children were stopped with the session"
    no_child(monkeypatch)
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped


# ---- a failure that stops nothing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", [signal.SIGSTOP, signal.SIGKILL])
def test_a_hung_or_a_killed_scorer_makes_its_sleeve_abstain_while_the_trader_runs_on_and_then_it_recovers(
    kept, tmp_path, monkeypatch, how
):
    registry, a, b = kept
    put(tmp_path, Lineup(champion=a, challengers=(b,)))
    made = desk(tmp_path, registry)
    made.run(effect(made, 7, 10), quotes=False)
    before = len(made.of("decision"))
    assert made.of("abstained") == []
    assert {e["sleeve"] for e in made.of("decision")} >= {ML, CHALLENGER, "control"}

    failed_at = made.clock.now_ns()
    os.kill(made.trader._scorers.pid(ML), how)
    turns = made.trader.summary()["turns"]
    made.run(effect(made, 8, 10), quotes=False)
    back_at = made.clock.now_ns()
    assert made.trader.summary()["turns"] - turns >= turns >= 30, "the loop took its turns as before: one a second"
    lost = made.of("abstained")
    assert {e["sleeve"] for e in lost} == {ML} and len(lost) > 20, "the sleeve whose scorer failed, and no other"
    assert lost[0]["why"] in (("TIMEOUT",) if how == signal.SIGSTOP else ("CLOSED", "NOT_SENT"))
    assert {e["why"] for e in lost[1:]} <= {"DOWN", "STARTING"} and "DOWN" in {e["why"] for e in lost}
    assert len({(e["row"], e["known_ns"]) for e in lost}) == len(lost), "no row was asked twice"
    during = [e for e in made.of("decision")[before:] if e["leg"] == "entry"]
    assert {e["sleeve"] for e in during} >= {CHALLENGER, "control"}, "the other sleeves went on trading"
    assert not [e for e in during if e["sleeve"] == ML and e["at_ns"] > failed_at + SECOND], "and this one, none"
    assert f"scorer.{ML}" in made.pages.fired and f"scorer.{ML}" not in made.pages.resolved

    assert until(lambda: ready(made)), "a new child was started after the back-off, by the loop itself"
    made.run(effect(made, 9, 10), quotes=False)
    after = [e for e in made.of("decision") if e["leg"] == "entry" and e["sleeve"] == ML and e["at_ns"] > back_at]
    assert len(after) >= 5 and [e for e in made.of("abstained") if e["at_ns"] > back_at + 2 * SECOND] == []
    assert f"scorer.{ML}" in made.pages.resolved, "and the owner is told that it answers again"
    made.stop()

    lost = made.of("abstained")
    record = beside(next(made.day().glob("session-*.tape.jsonl")))
    assert record.name == "session-001.abstained.jsonl"
    assert recorded(record) == {ML: {(e["row"], e["known_ns"]): e["why"] for e in lost}}, "row for row, beside the tape"
    no_child(monkeypatch)
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped, "the day replays byte for byte, abstentions and all, with no child"


def test_a_session_killed_while_its_scorer_was_down_replays_as_far_as_it_got(kept, tmp_path, monkeypatch):
    registry, a, b = kept
    put(tmp_path, Lineup(champion=a, challengers=(b,)))
    made = desk(tmp_path, registry)
    made.run(effect(made, 7, 5), quotes=False)
    os.kill(made.trader._scorers.pid(CHALLENGER), signal.SIGKILL)
    made.run(effect(made, 8, 5), quotes=False)
    assert {e["sleeve"] for e in made.of("abstained")} == {CHALLENGER}
    made.trader.abandon()  # the process is gone without a word
    no_child(monkeypatch)
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and not replayed.stopped


def test_a_record_that_does_not_verify_is_not_replayed_from(kept, tmp_path):
    registry, a, b = kept
    put(tmp_path, Lineup(champion=a, challengers=(b,)))
    made = desk(tmp_path, registry)
    made.run(effect(made, 7, 3), quotes=False)
    os.kill(made.trader._scorers.pid(ML), signal.SIGKILL)
    made.run(effect(made, 8, 3), quotes=False)
    made.stop()
    record = next(made.day().glob("*.abstained.jsonl"))
    record.write_bytes(record.read_bytes().replace(b'"why":"DOWN"', b'"why":"NONE"', 1))
    with pytest.raises(JournalError, match="does not match its own hash"):
        made.replay(tmp_path / "scratch")


# ---- who may have one ------------------------------------------------------------------------------------------------


def test_a_place_that_asks_for_a_process_gets_one_or_runs_idle_never_a_quiet_in_process_scorer(kept, tmp_path):
    registry, a, b = kept
    entries = places(registry, "process")["sleeves"]
    assert parse(places(registry, "process")).sleeves[2]["scorer"] == "process", "the settings accept it"
    asked = []

    def hand_over(name, model, models):
        asked.append((name, model.content_hash, models))
        if name == CHALLENGER:
            raise ScorerRefused("the scorer process does not hold that model")
        return client.scorer_for(model)

    lineup = Lineup(champion=a, challengers=(b,))
    options = {"registry": registry, "calendar": Calendar(), "others": lambda e: IdleSleeve(e["name"])}
    found = make(entries, lineup, scoring=hand_over, **options)
    assert asked == [(ML, a.model, registry / "models"), (CHALLENGER, b.model, registry / "models")]
    assert found.serving == {ML: a.model, CHALLENGER: None} and "does not hold that model" in found.refused[CHALLENGER]
    assert [type(sleeve) for sleeve in found.sleeves[2:]] == [MlSleeve, IdleSleeve]
    nobody = make(entries, lineup, **options)
    assert nobody.serving == {ML: None, CHALLENGER: None}
    assert all("this run was handed none" in why for why in nobody.refused.values())

    put(tmp_path, lineup)
    made = desk(tmp_path, registry, times=None)  # the settings ask for a process and the trader has no times for one
    assert made.of("start")[0]["serving"] == {ML: None, CHALLENGER: None}
    assert [e["sleeve"] for e in made.of("lineup_refused")] == [ML, CHALLENGER] and "lineup.ml" in made.pages.fired
    made.run(5)
    made.stop()
    assert read(made.day() / "decisions.jsonl")[0].entries > 10, "and the rest of the desk ran"
