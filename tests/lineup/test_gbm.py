"""A gradient-boosted model in the lineup: served, weighed, promoted and watched by the code that does it for ridge.

Nothing in the lineup, the weekly decision or the drift look asks what kind a model is. These tests put a gbm
model where the others put a ridge one and expect the same. They fit with the library, so they need the
optional ML set (`make ml`) and are skipped without it.
"""
from __future__ import annotations

import ast

import pytest

pytest.importorskip(
    "lightgbm", reason="the optional ML set is not installed (make ml): fitting a gbm model needs its library"
)

from hftlab import lineup
from hftlab.lineup import Lineup
from hftlab.lineup.serve import load
from hftlab.models import TreeScorer, scorer_for
from tests.learn.kit import WIN
from tests.lineup import test_decide, test_watch
from tests.lineup.kit import CHALLENGER, MARGIN, ML, desk, effect, put, register
from tests.support import sources


def test_a_gbm_challenger_trades_the_effect_in_shadow_beside_a_ridge_champion_and_the_day_replays(tmp_path):
    registry = tmp_path / "learn"
    ridge, gbm = register(registry, 1), register(registry, 3, kind="gbm")
    model, report, _ = load(gbm, registry)
    assert (model.kind, report["model"]["kind"]) == ("gbm", "gbm") and type(scorer_for(model)) is TreeScorer
    state = tmp_path / "trader"
    put(state, Lineup(champion=ridge, challengers=(gbm,)))
    made = desk(state, registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    assert made.of("start")[0]["serving"] == {ML: ridge.model, CHALLENGER: gbm.model}
    entries = [e for e in made.of("decision") if e["leg"] == "entry" and e["sleeve"] == CHALLENGER]
    assert len(entries) > 15 and all(type(e["expected_micro"]) is int and e["expected_micro"] > MARGIN for e in entries)
    summary = {one["name"]: one for one in made.trader.summary()["sleeves"]}
    assert summary[CHALLENGER]["net_total_micro"]["USD"] == summary[CHALLENGER]["fills"] // 2 * WIN > 0
    assert made.trader.stages.all()[CHALLENGER] == "shadow" and made.trader.broker_book is None
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped, "whole-number trees decide the same from the recording"


def test_a_gbm_candidate_becomes_a_challenger_at_the_weekly_decision_and_is_promoted_by_the_same_rule(tmp_path):
    world = test_decide.World(tmp_path)
    world.b = register(world.registry, 3, not_a_result=0, kind="gbm")
    world.lineup(Lineup(champion=world.a, candidates=(world.b,)))
    kept = world.decide(test_decide.SUNDAY - 7 * test_decide.DAY)
    assert kept.action == "keep" and lineup.read(world.path).challengers == (world.b,), "as any candidate does"
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "promote" and outcome.said == f"promote: {world.b.model[:12]}"
    after = lineup.read(world.path)
    assert after.champion == world.b and after.previous == (world.a,)


def test_a_gbm_challenger_is_promoted_by_nothing_when_the_rule_is_not_in_force(tmp_path):
    world = test_decide.World(tmp_path, rule=None)
    world.b = register(world.registry, 3, not_a_result=0, kind="gbm")
    world.lineup(Lineup(champion=world.a, challengers=(world.b,)))
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "keep" and outcome.said == "keep: rule not in force" and outcome.promoted is None
    assert lineup.read(world.path).champion == world.a


def test_a_gbm_model_on_placeholder_costs_is_kept_out_like_any_other(tmp_path):
    world = test_decide.World(tmp_path)
    world.b = register(world.registry, 3, not_a_result=1, kind="gbm")
    world.lineup(Lineup(champion=world.a, challengers=(world.b,)))
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "keep" and "report_rests_on_placeholders" in outcome.challengers[0].reasons


def test_the_drift_look_reads_a_gbm_models_week_as_it_reads_a_ridge_models(tmp_path):
    world = test_watch.World(tmp_path)
    world.b = register(world.registry, 3, kind="gbm")
    put(world.state, Lineup(champion=world.a, challengers=(world.b,)))
    made = desk(world.state, world.registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    found = world.look(made.clock.now_ns())
    assert [one.model for one in found] == [world.a.model, world.b.model]
    assert test_watch.names(found) == {ML: [], CHALLENGER: []}
    monitors = {finding.monitor for finding in found[1].findings}
    assert monitors == {finding.monitor for finding in found[0].findings} >= {"feature_shift", "spread_regime"}
    calibration = [f for f in found[1].findings if f.monitor == "calibration_decay" and f.value is not None]
    assert calibration, "what the trees expected of their trades is journalled and compared with what they made"


def test_nothing_that_serves_weighs_or_watches_a_model_asks_what_kind_it_is():
    """No special case: the lineup, the decision and the drift look never read a model's kind."""
    for rel, tree in sources():
        if rel.startswith("src/hftlab/lineup/") or rel in ("src/hftlab/learn/promote.py", "src/hftlab/learn/drift.py"):
            words = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
            assert not words & {"gbm", "ridge"}, rel
            assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "kind"
                        and isinstance(node.value, ast.Name) and node.value.id == "model"], rel  # fmt: skip
