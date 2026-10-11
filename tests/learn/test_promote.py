"""The promotion decision: promote, keep or roll back, from shadow results and a rule written beforehand."""
from __future__ import annotations

import ast
import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.learn import validate
from hftlab.learn.promote import KEEP, PROMOTE, ROLL_BACK, Rule, ShadowResult, Verdict, decide
from tests.support import imports, sources

RULE = Rule("HFT-DEC-0007", min_trades=20, min_days=5, max_drawdown_micro=60, draws=400, level_ppm=900_000, seed=7)
DAYS = (1, 2, 3, 4, 5)
CONTROL = ShadowResult({day: [-6, -6, -6, -6, -6] for day in DAYS})  # a champion that pays the cost and no more
GOOD = ShadowResult({day: [14, 9, -6, 12, 14, 8] for day in DAYS})


def but(result: ShadowResult, **changes) -> ShadowResult:
    return dataclasses.replace(result, **changes)


def test_a_challenger_that_passes_every_check_of_the_rule_is_promoted():
    verdict = decide(CONTROL, GOOD, RULE)
    assert (verdict.action, verdict.reasons) == (PROMOTE, ("all_checks_passed",))
    assert verdict.evidence == {"trades": 30, "days": 5, "advantage_lower_micro": 14, "result_lower_micro": 8,
                                "drawdown_micro": 6}  # fmt: skip
    assert decide(CONTROL, GOOD, RULE) == verdict, "the same evidence under the same rule is the same verdict"
    assert decide(but(CONTROL, by_day=dict(reversed(CONTROL.by_day.items()))), GOOD, RULE) == verdict


@pytest.mark.parametrize(
    ("challenger", "rule", "reasons"),
    [
        (but(GOOD, by_day={d: GOOD.by_day[d][:3] for d in DAYS}), RULE, ("too_few_trades",)),
        (GOOD, dataclasses.replace(RULE, min_days=6), ("too_few_days",)),
        (but(GOOD, by_day={**{d: GOOD.by_day[d] for d in DAYS[:4]}, 9: GOOD.by_day[5]}), RULE,
         ("days_differ", "advantage_not_above_zero", "result_not_above_zero")),
        (but(GOOD, by_day=CONTROL.by_day), RULE,
         ("advantage_not_above_zero", "result_not_above_zero", "drawdown_beyond_bound")),
        (but(GOOD, by_day={d: [-1, -2, -1, -2, -1] for d in DAYS}), dataclasses.replace(RULE, max_drawdown_micro=99),
         ("result_not_above_zero",)),
        (GOOD, dataclasses.replace(RULE, max_drawdown_micro=5), ("drawdown_beyond_bound",)),
        (but(GOOD, reconciliation_breaks=1), RULE, ("reconciliation_breaks",)),
        (but(GOOD, latch_breached=True), RULE, ("challenger_latch_breached",)),
        (GOOD, dataclasses.replace(RULE, decision=None), ("rule_not_registered",)),
        (ShadowResult({d: [] for d in DAYS}), RULE,
         ("too_few_trades", "advantage_not_above_zero", "result_not_above_zero")),
        (but(GOOD, by_day={d: [-90] * 6 for d in DAYS}, reconciliation_breaks=2, latch_breached=True), RULE,
         ("advantage_not_above_zero", "result_not_above_zero", "drawdown_beyond_bound", "reconciliation_breaks",
          "challenger_latch_breached")),
    ],
)  # fmt: skip
def test_anything_less_keeps_the_champion_and_says_which_check_failed(challenger, rule, reasons):
    verdict = decide(CONTROL, challenger, rule)
    assert (verdict.action, verdict.reasons) == (KEEP, reasons)


def test_losing_less_than_a_losing_champion_is_an_advantage_and_still_not_a_reason_to_trade():
    loses_less = but(GOOD, by_day={d: [-1, -2, -1, -2, -1] for d in DAYS})
    verdict = decide(CONTROL, loses_less, dataclasses.replace(RULE, max_drawdown_micro=99))
    assert verdict.evidence["advantage_lower_micro"] > 0 > verdict.evidence["result_lower_micro"]
    assert verdict.action == KEEP


def test_a_champion_that_breached_its_latch_is_rolled_back_whatever_the_challenger_did():
    """The forced rollback: the one verdict that needs nobody's evidence but the latch's."""
    breached = but(CONTROL, latch_breached=True)
    for challenger in (GOOD, but(GOOD, reconciliation_breaks=3), ShadowResult({}), CONTROL):
        for rule in (RULE, dataclasses.replace(RULE, decision=None), dataclasses.replace(RULE, min_trades=10**6)):
            assert decide(breached, challenger, rule) == Verdict(ROLL_BACK, ("champion_latch_breached",))
    assert decide(but(GOOD, latch_breached=True), GOOD, RULE).action == ROLL_BACK, "however well it had done"
    assert decide(CONTROL, GOOD, RULE).action == PROMOTE, "and without the breach the same evidence promotes"


RESULTS = st.dictionaries(st.integers(1, 6), st.lists(st.integers(-50, 50), max_size=6), max_size=6)
RULES = st.builds(
    Rule, st.sampled_from(["HFT-DEC-0001", None]), st.integers(1, 12), st.integers(1, 4), st.integers(0, 120),
    st.just(60), st.sampled_from([500_000, 900_000]), st.integers(0, 99),
)  # fmt: skip


@given(RESULTS, RESULTS, st.integers(0, 1), st.booleans(), st.booleans(), RULES)
def test_a_promotion_means_every_check_passed_and_a_breach_means_rollback(a, b, breaks, c_latch, latch, rule):
    champion, challenger = ShadowResult(a, 0, latch), ShadowResult(b, breaks, c_latch)
    verdict = decide(champion, challenger, rule)
    assert verdict.action in (PROMOTE, KEEP, ROLL_BACK) and verdict.reasons
    assert (verdict.action == ROLL_BACK) == latch
    if verdict.action == PROMOTE:
        how = {"seed": rule.seed, "draws": rule.draws, "level_ppm": rule.level_ppm}
        assert rule.decision is not None and breaks == 0 and not c_latch and set(a) == set(b)
        assert challenger.trades >= rule.min_trades and len(b) >= rule.min_days
        assert validate.drawdown(b) <= rule.max_drawdown_micro
        assert validate.advantage(b, a, **how).lower > 0 and validate.mean_per_trade(b, **how).lower > 0
    if verdict.action == KEEP:
        assert "all_checks_passed" not in verdict.reasons and len(set(verdict.reasons)) == len(verdict.reasons)


@pytest.mark.parametrize(
    "change",
    [{"decision": "DEC-7"}, {"decision": "HFT-EXP-0001"}, {"min_trades": 0}, {"min_days": 0}, {"draws": 0},
     {"max_drawdown_micro": -1}, {"level_ppm": 0}, {"level_ppm": 1_000_000}],
)  # fmt: skip
def test_a_rule_has_no_defaults_and_refuses_numbers_that_are_no_rule(change):
    with pytest.raises(ValueError):
        dataclasses.replace(RULE, **change)
    with pytest.raises(TypeError):
        Rule("HFT-DEC-0007")
    assert all(f.default is dataclasses.MISSING for f in dataclasses.fields(Rule))


def test_the_decision_reads_no_file_no_clock_and_no_process():
    tree = dict(sources())["src/hftlab/learn/promote.py"]
    reached = {name.split(".")[0] for _, name in imports(tree, "src/hftlab/learn/promote.py")}
    assert reached <= {"__future__", "re", "collections", "dataclasses", "typing", "hftlab"}
    ours = {name for _, name in imports(tree, "src/hftlab/learn/promote.py") if name.startswith("hftlab")}
    assert {name.rsplit(".", 1)[0] for name in ours} == {"hftlab.learn.validate", "hftlab.lineup.decide"}
    (last,) = [node for node in tree.body if isinstance(node, ast.If)]
    assert ast.unparse(last.test) == "__name__ == '__main__'" and tree.body[-1] is last
    inside = {alias.name for node in ast.walk(last) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert inside == {"main"}, "the command (`python -m hftlab.learn.promote`) is the lineup's; only its start is here"
    above = [node for node in tree.body[:-1] for found in ast.walk(node) if isinstance(found, ast.ImportFrom)]
    assert all(node.module != "hftlab.lineup.decide" for node in above)
