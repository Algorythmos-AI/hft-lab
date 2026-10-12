"""The staged rollout: how far each sleeve is allowed to go, kept in a hash-chained journal (ADR 0019).

    off            the sleeve is not run
    shadow         its orders go to the shadow venue only
    min_size       paper orders, every one that adds to a position at the pair's minimum size
    normal         paper orders at the size the sleeve asks for
    stepped_down   paper orders that reduce a position only; nothing that adds

A stage changes by rule and by nothing else:

  * UP one step, at the close of a trading day, when the rule for that step is met: the configured number of
    clean trading days in a row in the present stage, the decision record that approved the step named in the
    configuration, the risk limits in force, no latch tripped and no reconciliation break standing, and the
    venue's capability file showing that the account may open the positions the sleeve needs (`cannot_open`
    empty: hftlab.venues.capability). A rule with a number or a record missing is never met, so no default
    promotes anything. When everything but the limits or the account is there, the refusal is journalled.
  * DOWN at once, to `stepped_down`, when a latch that concerns the sleeve trips or a reconciliation break is
    flagged while the sleeve is placing paper orders.
  * DOWN to `shadow` or `off` when an operator asks, with the operator's name in the journal.
  * DOWN at once, to `stepped_down`, when the model a sleeve serves is another than the one it served before
    (ADR 0023): a model judged in shadow never inherits a stage that places orders.

A trading day is the time between two 17:00 New York rollovers in which the market was open at all. It is
clean when the trader saw its close, ran for at least the configured part of it, was never stopped uncleanly
in it, and nothing went wrong in it: no latch, no break, no change of the sleeve's stage. What went wrong is
journalled when it happens, so a restart does not forget it; a day the trader did not see the close of is
written down as not clean by the next start.

Everything here is a function of the journal, the calendar and the times it is handed. It reads no clock.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from hftlab.calendar import Calendar, last_rollover
from hftlab.journal import Journal, read

OFF, SHADOW, MIN_SIZE, NORMAL, STEPPED_DOWN = "off", "shadow", "min_size", "normal", "stepped_down"
STAGES: Final = (OFF, SHADOW, MIN_SIZE, NORMAL, STEPPED_DOWN)
PAPER: Final = (MIN_SIZE, NORMAL, STEPPED_DOWN)  # the stages in which a sleeve's orders can reach the broker
NEXT: Final = {SHADOW: MIN_SIZE, MIN_SIZE: NORMAL, STEPPED_DOWN: MIN_SIZE}
DAY_NS: Final = 24 * 3_600 * 1_000_000_000
DESK: Final = ""  # the scope of something that concerns every sleeve
_DECISION = re.compile(r"HFT-DEC-[0-9]{4}")

# causes of a change, and of a day that is not clean
PROMOTED, LATCH, BREAK, OPERATOR, MODEL = "PROMOTED", "LATCH", "BREAK", "OPERATOR", "MODEL"
UNCLEAN_STOP, NOT_RUNNING, TOO_SHORT, STAGE_CHANGED = "UNCLEAN_STOP", "NOT_RUNNING", "TOO_SHORT", "STAGE_CHANGED"
LIMITS_NOT_IN_FORCE, STILL_LATCHED, STILL_BROKEN = "LIMITS_NOT_IN_FORCE", "STILL_LATCHED", "STILL_BROKEN"
ACCOUNT_CANNOT_OPEN = "ACCOUNT_CANNOT_OPEN"  # the capability file does not show the positions as openable


class StageError(ValueError):
    """The rollout configuration or its journal says something that cannot be acted on."""


@dataclass(frozen=True, slots=True)
class Rule:
    """What one step up needs. Either value missing, and the step is never taken."""

    clean_days: int | None
    decision: str | None  # the HFT-DEC record in which the owner approved this step for this sleeve

    def __post_init__(self) -> None:
        if self.clean_days is not None and (type(self.clean_days) is not int or self.clean_days < 1):
            raise StageError(f"clean_days must be null or a whole number of at least 1, not {self.clean_days!r}")
        if self.decision is not None and not (isinstance(self.decision, str) and _DECISION.fullmatch(self.decision)):
            raise StageError(f"decision must be null or a decision record id like HFT-DEC-0001, not {self.decision!r}")

    @property
    def complete(self) -> bool:
        return self.clean_days is not None and self.decision is not None


NEVER: Final = Rule(None, None)


@dataclass(frozen=True, slots=True)
class Rollout:
    """One sleeve's ladder: where it starts when the journal has never heard of it, and its two steps up."""

    start: str = SHADOW
    to_min_size: Rule = NEVER
    to_normal: Rule = NEVER

    def __post_init__(self) -> None:
        if self.start not in (OFF, SHADOW):
            raise StageError(f"a sleeve starts in {SHADOW} or {OFF}, never in {self.start!r}: the rest is earned")

    def rule_to(self, stage: str) -> Rule:
        return self.to_min_size if stage == MIN_SIZE else self.to_normal if stage == NORMAL else NEVER


@dataclass(frozen=True, slots=True)
class Change:
    """One change of one sleeve's stage, as it was journalled. `number` is its place in the journal."""

    sleeve: str
    before: str
    to: str
    cause: str
    detail: str
    at_ns: int
    number: int


def day_of(at_ns: int) -> int:
    """The trading day an instant belongs to, named by the rollover that closes it."""
    opened = last_rollover(at_ns)
    return last_rollover(opened + DAY_NS + DAY_NS // 2)


def is_trading_day(calendar: Calendar, day: int) -> bool:
    return calendar.open_ns_between(last_rollover(day - 1), day) > 0


class Stages:
    def __init__(
        self,
        journal: Journal,
        rollout: Mapping[str, Rollout],
        *,
        calendar: Calendar,
        in_force: bool,
        min_day_ns: int | None,
        cannot_open: str = "",
    ) -> None:
        self._journal, self._rollout, self._calendar = journal, dict(rollout), calendar
        self._cannot_open = cannot_open  # why the account cannot open what the sleeves need; "": it can
        self._in_force, self._min_day_ns = in_force, min_day_ns
        self._stage = {name: ladder.start for name, ladder in self._rollout.items()}
        self._days: dict[str, list[tuple[int, str, bool]]] = {name: [] for name in self._rollout}  # day, stage, clean
        self._dirty: dict[int, set[tuple[str, str]]] = {}  # day -> (scope, cause)
        self._covered: dict[int, int] = {}  # day -> time whole sessions ran in it
        self._open: tuple[int, int] | None = None  # (day, since) of a session that has not said it stopped
        self._refused: set[tuple[int, str]] = set()
        self._first: int | None = None  # the day of the first session ever: nothing before it was missed
        self._served: dict[str, str] = {}  # sleeve -> the model it last served; "" for none
        for entry in read(journal.path)[1]:
            self._restore(entry)
        journal.open()

    # ---- reading ------------------------------------------------------------------------------------------------

    def stage(self, sleeve: str) -> str:
        return self._stage[sleeve]

    def all(self) -> dict[str, str]:
        return dict(self._stage)

    def above_shadow(self) -> list[str]:
        """The sleeves whose stage lets an order reach the broker."""
        return sorted(name for name, stage in self._stage.items() if stage in PAPER)

    def clean_run(self, sleeve: str) -> int:
        """Clean trading days in a row, up to the last one closed, all in the sleeve's present stage."""
        run = 0
        for _, stage, clean in reversed(self._days[sleeve]):
            if not clean or stage != self._stage[sleeve]:
                break
            run += 1
        return run

    # ---- a session ----------------------------------------------------------------------------------------------

    def begin(self, at_ns: int) -> None:
        """A session starts. What the last one left unsaid is written down first, against the day it belongs to."""
        day = day_of(at_ns)
        if self._open is not None:
            self._mark(self._open[0], DESK, UNCLEAN_STOP, at_ns)  # it started and never said it stopped
        if self._first is not None:
            self._close_missed(day, at_ns)
        self._journal.write("session", event="start", day=day, at_ns=at_ns)
        self._journal.flush()
        self._open, self._first = (day, at_ns), day if self._first is None else self._first

    def end(self, at_ns: int) -> None:
        """The session stops on purpose. The time it ran counts towards its day."""
        if self._open is None:
            return
        day, since = self._open
        self._covered[day] = self._covered.get(day, 0) + max(0, at_ns - since)
        self._journal.write("session", event="stop", day=day, at_ns=at_ns, ran_ns=max(0, at_ns - since))
        self._journal.flush()
        self._open = None

    def _close_missed(self, today: int, at_ns: int) -> None:
        """The last trading day before this one, if nobody saw its close: it happened, and it was not clean."""
        day = last_rollover(today - 1)
        for _ in range(10):
            if is_trading_day(self._calendar, day):
                break
            day = last_rollover(day - 1)
        else:
            return
        if self._first is None or day < self._first:
            return
        for sleeve in sorted(self._rollout):
            if not any(closed == day for closed, _, _ in self._days[sleeve]):
                self._close(sleeve, day, clean=False, causes=[NOT_RUNNING], covered=self._covered.get(day, 0))
        self._journal.flush()

    # ---- what goes wrong ----------------------------------------------------------------------------------------

    def mark(self, at_ns: int, cause: str, sleeve: str = DESK) -> None:
        """Something happened that keeps the day in hand from being clean, for one sleeve or for all."""
        self._mark(day_of(at_ns), sleeve, cause, at_ns)

    def _mark(self, day: int, scope: str, cause: str, at_ns: int) -> None:
        if (scope, cause) not in self._dirty.setdefault(day, set()):
            self._dirty[day].add((scope, cause))
            self._journal.write("dirty", day=day, sleeve=scope, cause=cause, at_ns=at_ns)
            self._journal.flush()

    def step_down(self, sleeve: str, cause: str, detail: str, at_ns: int) -> Change | None:
        """A latch or a break: a sleeve that is placing paper orders may only reduce from now on."""
        if self._stage.get(sleeve) not in (MIN_SIZE, NORMAL):
            return None
        return self._change(sleeve, STEPPED_DOWN, cause, detail, at_ns)

    def serve(self, sleeve: str, model: str | None, at_ns: int) -> Change | None:
        """A session starts with this model in the sleeve's place. Another model than before, and the stage is lost."""
        named = model or ""
        if sleeve not in self._stage or self._served.get(sleeve) == named:
            return None
        self._journal.write("model", sleeve=sleeve, model=named, at_ns=at_ns)
        self._journal.flush()
        self._served[sleeve] = named
        return self.step_down(sleeve, MODEL, named[:12], at_ns)

    def demote(self, sleeve: str, to: str, operator: str, at_ns: int) -> Change | None:
        """An operator takes a sleeve down to `shadow` or `off`. Never up: up is earned."""
        if sleeve not in self._stage or to not in (OFF, SHADOW) or self._stage[sleeve] == to:
            return None
        if to == SHADOW and self._stage[sleeve] == OFF:
            return None  # that would be a step up
        return self._change(sleeve, to, OPERATOR, operator, at_ns)

    def _change(self, sleeve: str, to: str, cause: str, detail: str, at_ns: int) -> Change:
        before = self._stage[sleeve]
        number = self._journal.entries
        self._journal.write("stage", sleeve=sleeve, before=before, to=to, cause=cause, detail=detail, at_ns=at_ns)
        self._journal.flush()
        self._stage[sleeve] = to
        if cause != PROMOTED:  # a step up is taken at a day's close, and the day it opens starts clean
            self._dirty.setdefault(day_of(at_ns), set()).add((sleeve, STAGE_CHANGED))
        return Change(sleeve, before, to, cause, detail, at_ns, number)

    # ---- the close of a day -------------------------------------------------------------------------------------

    def close_day(self, day: int, at_ns: int, *, latched: bool, broken: bool) -> list[Change]:
        """The trader has seen `day` close. Each sleeve's day is judged, and a step up is taken where it is earned."""
        changes: list[Change] = []
        if not is_trading_day(self._calendar, day):
            return changes
        covered = self._covered.get(day, 0) + (max(0, at_ns - self._open[1]) if self._open else 0)
        for sleeve in sorted(self._rollout):
            if any(closed == day for closed, _, _ in self._days[sleeve]):
                continue
            causes = sorted(cause for scope, cause in self._dirty.get(day, ()) if scope in (DESK, sleeve))
            if self._min_day_ns is None or covered < self._min_day_ns:
                causes.append(TOO_SHORT)
            self._close(sleeve, day, clean=not causes, causes=causes, covered=covered)
            change = self._promote(sleeve, day, at_ns, latched=latched, broken=broken)
            if change is not None:
                changes.append(change)
        self._journal.flush()
        return changes

    def _close(self, sleeve: str, day: int, *, clean: bool, causes: list[str], covered: int) -> None:
        stage = self._stage[sleeve]
        self._journal.write("day", day=day, sleeve=sleeve, stage=stage, clean=int(clean), causes=causes, ran_ns=covered)
        self._days[sleeve].append((day, stage, clean))

    def _promote(self, sleeve: str, day: int, at_ns: int, *, latched: bool, broken: bool) -> Change | None:
        to = NEXT.get(self._stage[sleeve])
        rule = self._rollout[sleeve].rule_to(to) if to else NEVER
        if to is None or not rule.complete:
            return None
        assert rule.clean_days is not None and rule.decision is not None
        if self.clean_run(sleeve) < rule.clean_days:
            return None
        held, detail = "", ""
        if self._cannot_open:
            held, detail = ACCOUNT_CANNOT_OPEN, self._cannot_open
        elif not self._in_force:
            held = LIMITS_NOT_IN_FORCE
        elif latched or broken:
            held = STILL_LATCHED if latched else STILL_BROKEN
        if held:
            if (day, sleeve) not in self._refused:
                self._refused.add((day, sleeve))
                said = {"detail": detail} if detail else {}
                self._journal.write("held", day=day, sleeve=sleeve, to=to, cause=held, at_ns=at_ns, **said)
            return None
        return self._change(sleeve, to, PROMOTED, rule.decision, at_ns)

    # ---- the journal, read back ---------------------------------------------------------------------------------

    def _restore(self, entry: dict[str, object]) -> None:
        kind, sleeve, day = entry.get("type"), entry.get("sleeve"), entry.get("day")
        if kind == "session" and isinstance(day, int) and self._first is None:
            self._first = day
        if kind == "stage" and isinstance(sleeve, str) and entry.get("to") in STAGES:
            if sleeve in self._stage:
                self._stage[sleeve] = str(entry["to"])
            at = entry.get("at_ns")
            if isinstance(at, int) and entry.get("cause") != PROMOTED:
                self._dirty.setdefault(day_of(at), set()).add((sleeve, STAGE_CHANGED))
        elif kind == "model" and isinstance(sleeve, str):
            self._served[sleeve] = str(entry.get("model") or "")
        elif kind == "dirty" and isinstance(day, int) and isinstance(sleeve, str):
            self._dirty.setdefault(day, set()).add((sleeve, str(entry.get("cause"))))
        elif kind == "day" and isinstance(day, int) and isinstance(sleeve, str) and sleeve in self._days:
            self._days[sleeve].append((day, str(entry.get("stage")), entry.get("clean") == 1))
        elif kind == "held" and isinstance(day, int) and isinstance(sleeve, str):
            self._refused.add((day, sleeve))
        elif kind == "session" and isinstance(day, int):
            at, ran = entry.get("at_ns"), entry.get("ran_ns")
            if entry.get("event") == "start" and isinstance(at, int):
                self._open = (day, at)
            elif entry.get("event") == "stop":
                self._covered[day] = self._covered.get(day, 0) + (ran if isinstance(ran, int) else 0)
                self._open = None
