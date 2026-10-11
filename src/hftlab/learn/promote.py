"""The promotion decision, as a pure function (ADR 0008, ADR 0021).

    decide(champion, challenger, rule) -> Verdict(action, reasons)

Given what a champion and a challenger did in shadow over the same days, and a rule written down beforehand,
it says one of three things and why:

  roll_back   the champion breached its loss latch. It is replaced by the model before it without waiting for
              anyone, and nothing is promoted on the same decision.
  promote     the challenger passed every check of the rule.
  keep        anything else. Most weeks this is the right answer.

The checks, each with its own reason code when it fails:

  rule_not_registered            the rule names no decision record: a rule nobody wrote down passes nothing
  too_few_days / too_few_trades  less shadow evidence than the rule asks for
  too_few_own_days               the challenger's model was fitted to fewer days of this platform's own
                                 recordings than the rule's `min_days`. A day of imported history is not
                                 counted, however many there are: results on somebody else's quotes are not
                                 this venue's results (ADR 0028)
  days_differ                    the two did not run over the same trading days, so they cannot be compared
  advantage_not_above_zero       the lower bound of the day-block bootstrap of the challenger's mean result per
                                 trade, less the champion's, after costs, is not above zero
  result_not_above_zero          the lower bound of the challenger's own mean result per trade is not above
                                 zero: losing less than a losing champion is not a reason to trade
  drawdown_beyond_bound          the challenger's largest fall from a high is beyond the rule's bound
  reconciliation_breaks          the challenger's week had a reconciliation break
  challenger_latch_breached      the challenger breached its own loss latch in shadow

The rule has one number of days, and it is asked twice: of the week's shadow evidence, and of the recorded
days the model was fitted to. A model fitted to imported history alone has none of the second, and no rule
passes it: `min_days` is at least one.

Results are the shadow book's, in integer millionths of the quote currency per trade, listed by trading day.
The function reads nothing else: no file, no clock, no process. It moves no model and sets no stage; who acts
on a verdict, and when, is not decided in this module. The bootstrap is exact and seeded by the rule, so the
same evidence under the same rule is the same verdict on any machine.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from hftlab.learn.validate import MILLION, advantage, drawdown, mean_per_trade

PROMOTE: Final = "promote"
KEEP: Final = "keep"
ROLL_BACK: Final = "roll_back"
_DECISION = re.compile(r"HFT-DEC-[0-9]{4}")


@dataclass(frozen=True, slots=True)
class Rule:
    """The gate, as registered. Every number is the rule's own: there is no default to fall back on."""

    decision: str | None  # the decision record that set it; None for a rule that is not in force
    min_trades: int
    min_days: int
    max_drawdown_micro: int
    draws: int
    level_ppm: int
    seed: int

    def __post_init__(self) -> None:
        if self.decision is not None and not _DECISION.fullmatch(self.decision):
            raise ValueError(f"a rule is registered by an HFT-DEC-NNNN record, not {self.decision!r}")
        if min(self.min_trades, self.min_days, self.draws) < 1 or self.max_drawdown_micro < 0:
            raise ValueError("a rule asks for at least one trade, one day and one draw, and no negative bound")
        if not 0 < self.level_ppm < MILLION:
            raise ValueError("the confidence level is between 0 and 1,000,000 millionths")


@dataclass(frozen=True, slots=True)
class ShadowResult:
    """What one model did in shadow: its result per trade after costs, by trading day, and what went wrong.

    `own_days` is how many days of this platform's own recordings the model was fitted to, as its evaluation
    report says. Days of imported history are not in it, and a model nobody vouched for has none.
    """

    by_day: Mapping[int, Sequence[int]]
    reconciliation_breaks: int = 0
    latch_breached: bool = False
    own_days: int = 0

    @property
    def trades(self) -> int:
        return sum(len(day) for day in self.by_day.values())


@dataclass(frozen=True, slots=True)
class Verdict:
    action: str
    reasons: tuple[str, ...]
    evidence: Mapping[str, int] = field(default_factory=dict)  # the numbers the checks were made on


def decide(champion: ShadowResult, challenger: ShadowResult, rule: Rule) -> Verdict:
    if champion.latch_breached:
        return Verdict(ROLL_BACK, ("champion_latch_breached",))
    evidence: dict[str, int] = {"trades": challenger.trades, "days": len(challenger.by_day)}
    evidence["own_days"] = challenger.own_days
    reasons: list[str] = []

    def check(passed: bool, code: str) -> None:
        if not passed:
            reasons.append(code)

    check(rule.decision is not None, "rule_not_registered")
    check(len(challenger.by_day) >= rule.min_days, "too_few_days")
    check(challenger.trades >= rule.min_trades, "too_few_trades")
    check(challenger.own_days >= rule.min_days, "too_few_own_days")
    same_days = set(champion.by_day) == set(challenger.by_day)
    check(same_days, "days_differ")
    how = {"seed": rule.seed, "draws": rule.draws, "level_ppm": rule.level_ppm}
    if same_days and challenger.trades:
        ahead = advantage(challenger.by_day, champion.by_day, **how).lower
        own = mean_per_trade(challenger.by_day, **how).lower
        evidence["advantage_lower_micro"] = ahead.numerator // ahead.denominator  # rounded down
        evidence["result_lower_micro"] = own.numerator // own.denominator
        check(ahead > 0, "advantage_not_above_zero")
        check(own > 0, "result_not_above_zero")
    else:
        reasons += ["advantage_not_above_zero", "result_not_above_zero"]
    evidence["drawdown_micro"] = drawdown(challenger.by_day)
    check(evidence["drawdown_micro"] <= rule.max_drawdown_micro, "drawdown_beyond_bound")
    check(challenger.reconciliation_breaks == 0, "reconciliation_breaks")
    check(not challenger.latch_breached, "challenger_latch_breached")
    if reasons:
        return Verdict(KEEP, tuple(reasons), evidence)
    return Verdict(PROMOTE, ("all_checks_passed",), evidence)


if __name__ == "__main__":  # `python -m hftlab.learn.promote`. The command is another module's; this one stays pure
    from hftlab.lineup.decide import main

    raise SystemExit(main())
