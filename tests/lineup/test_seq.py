"""A sequence network in the lineup: served, weighed, promoted and watched by the code that does it for ridge.

Nothing in the lineup, the weekly decision or the drift look asks what kind a model is, and the sleeve that
trades a network's score is the ML sleeve every model has: what differs is the builder its report asks for,
which hands it windows of rows. These tests fit a network, so they need the array library of the base
requirements and are skipped where it cannot be imported.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip(
    "numpy", reason="the array library is not installed (make bootstrap): fitting a sequence network needs it"
)

from hftlab import lineup
from hftlab.calendar import Calendar
from hftlab.features import WindowBuilder
from hftlab.lineup import Lineup
from hftlab.lineup.serve import load, make, windows
from hftlab.models import NetScorer, scorer_for
from hftlab.scoring.client import Times
from hftlab.strategy import IdleSleeve, MlSleeve
from tests.learn.kit import WIN
from tests.lineup import test_decide, test_watch
from tests.lineup.kit import CHALLENGER, MARGIN, ML, NETWORK, desk, effect, place, put, register, settings
from tests.trader.kit import Desk
from tests.venues.kit import SECOND

PATIENT = Times(timeout_ns=20 * SECOND, start_ns=3_600 * SECOND, restart_first_ns=SECOND, restart_last_ns=4 * SECOND)


@pytest.fixture(scope="module")
def kept(tmp_path_factory):
    registry = tmp_path_factory.mktemp("registry")
    return registry, register(registry, 1), register(registry, 3, kind="seq")


def test_the_report_asks_for_windows_and_the_ml_sleeve_is_handed_a_builder_of_them(kept):
    registry, ridge, seq = kept
    model, report, _ = load(seq, registry)
    assert (model.kind, report["model"]["kind"]) == ("seq", "seq") and type(scorer_for(model)) is NetScorer
    config, _ = windows(report)
    assert config.window_rows == NETWORK.window == 4 and len(model.features) == 4 * 10
    assert windows(load(ridge, registry)[1])[0].window_rows == 1, "a report that names no window is served single rows"
    found = make(settings(registry)["sleeves"], Lineup(champion=ridge, challengers=(seq,)), registry=registry,
                 calendar=Calendar(), others=lambda entry: IdleSleeve(entry["name"]))  # fmt: skip
    assert found.refused == {} and [type(sleeve) for sleeve in found.sleeves[2:]] == [MlSleeve, MlSleeve]
    assert type(found.sleeves[3]._features) is WindowBuilder and type(found.sleeves[2]._features) is not WindowBuilder
    sleeve = found.sleeves[3]
    assert sleeve.settings()["features"]["window_rows"] == 4
    state = sleeve.initial()
    assert sleeve.restore(json.loads(json.dumps(sleeve.save(state)))) == state, "its state is kept as any sleeve's is"


def test_a_seq_challenger_trades_the_effect_in_shadow_beside_a_ridge_champion_and_the_day_replays(kept, tmp_path):
    registry, ridge, seq = kept
    state = tmp_path / "trader"
    put(state, Lineup(champion=ridge, challengers=(seq,)))
    made = desk(state, registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    assert made.of("start")[0]["serving"] == {ML: ridge.model, CHALLENGER: seq.model}
    entries = [e for e in made.of("decision") if e["leg"] == "entry" and e["sleeve"] == CHALLENGER]
    assert len(entries) > 15 and all(type(e["expected_micro"]) is int and e["expected_micro"] > MARGIN for e in entries)
    summary = {one["name"]: one for one in made.trader.summary()["sleeves"]}
    assert summary[CHALLENGER]["net_total_micro"]["USD"] == summary[CHALLENGER]["fills"] // 2 * WIN > 0
    assert 30 < summary[CHALLENGER]["fills"] <= summary[ML]["fills"], "a window takes three rows longer to be whole"
    assert made.trader.stages.all()[CHALLENGER] == "shadow" and made.trader.broker_book is None
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped, "whole-number sums decide the same from the recording"


def test_the_day_scored_by_the_scorers_own_process_is_the_day_scored_in_the_trader_byte_for_byte(kept, tmp_path):
    registry, ridge, seq = kept
    journals = {}
    for scorer in ("in_process", "process"):
        raw = settings(registry)
        raw["sleeves"] = [*raw["sleeves"][:2], place(ML, "champion", scorer=scorer),
                          place(CHALLENGER, "challenger-1", scorer=scorer)]  # fmt: skip
        folder = tmp_path / scorer
        put(folder, Lineup(champion=ridge, challengers=(seq,)))
        made = Desk(folder, paper=False, settings=raw, scorer_times=PATIENT)
        made.run(effect(made, 7, 20), quotes=False)
        made.stop()
        journals[scorer] = (made.day() / "decisions.jsonl").read_bytes()
        assert made.of("abstained") == [], "no window was lost on the way to the child"
    assert journals["process"] == journals["in_process"] and len(journals["process"]) > 10_000
    assert [e for e in made.of("decision") if e["leg"] == "entry" and e["sleeve"] == CHALLENGER]


def test_a_seq_candidate_becomes_a_challenger_at_the_weekly_decision_and_is_promoted_by_the_same_rule(tmp_path):
    world = test_decide.World(tmp_path)
    world.b = register(world.registry, 3, not_a_result=0, kind="seq")
    world.lineup(Lineup(champion=world.a, candidates=(world.b,)))
    kept = world.decide(test_decide.SUNDAY - 7 * test_decide.DAY)
    assert kept.action == "keep" and lineup.read(world.path).challengers == (world.b,), "as any candidate does"
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "promote" and outcome.said == f"promote: {world.b.model[:12]}"
    after = lineup.read(world.path)
    assert after.champion == world.b and after.previous == (world.a,)


def test_a_seq_challenger_is_promoted_by_nothing_when_the_rule_is_not_in_force_or_its_costs_are_placeholders(tmp_path):
    world = test_decide.World(tmp_path / "no-rule", rule=None)
    world.b = register(world.registry, 3, not_a_result=0, kind="seq")
    world.lineup(Lineup(champion=world.a, challengers=(world.b,)))
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "keep" and outcome.said == "keep: rule not in force" and outcome.promoted is None
    assert lineup.read(world.path).champion == world.a
    other = test_decide.World(tmp_path / "placeholder")
    other.b = register(other.registry, 3, not_a_result=1, kind="seq")
    other.lineup(Lineup(champion=other.a, challengers=(other.b,)))
    other.good_week()
    outcome = other.decide()
    assert outcome.action == "keep" and "report_rests_on_placeholders" in outcome.challengers[0].reasons


def test_the_drift_look_reads_a_seq_models_week_as_it_reads_a_ridge_models(tmp_path):
    world = test_watch.World(tmp_path)
    world.b = register(world.registry, 3, kind="seq")
    put(world.state, Lineup(champion=world.a, challengers=(world.b,)))
    made = desk(world.state, world.registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    found = world.look(made.clock.now_ns())
    assert [one.model for one in found] == [world.a.model, world.b.model]
    assert test_watch.names(found) == {ML: [], CHALLENGER: []}
    monitors = {finding.monitor for finding in found[1].findings}
    assert monitors == {finding.monitor for finding in found[0].findings} >= {"feature_shift", "spread_regime"}
    shifts = [f for f in found[1].findings if f.monitor == "feature_shift"]
    assert len(shifts) > len([f for f in found[0].findings if f.monitor == "feature_shift"]), "one per value shown"
    calibration = [f for f in found[1].findings if f.monitor == "calibration_decay" and f.value is not None]
    assert calibration, "what the network expected of its trades is journalled and compared with what they made"
