"""The weekly decision: `python -m hftlab.learn.promote`, on Sunday before the open (ADR 0023).

    python -m hftlab.learn.promote              read the week, decide, write the lineup, page
    python -m hftlab.learn.promote --dry-run    the same reading and the same decision; writes nothing
    python -m hftlab.learn.promote --drill      prove the rollback on a copy of the journal

It reads the trader's decision journals, takes each sleeve's round trips in the shadow book by trading day,
the reconciliation breaks and the loss latches of the week, and asks `hftlab.learn.promote.decide` once per
challenger. What it may then write:

  roll back   the champion's own loss latch tripped in the week while it served. The champion before it is
              restored. This needs no rule: a rule that is not in force passes nothing and still rolls back.
  promote     `decide` says so, AND the rule is in force, AND the decision record it names exists in
              research/decisions/ with the owner's approval, AND the risk limits are in force, AND the week
              had no reconciliation break, AND the challenger's report does not rest on placeholders, AND
              the challenger's place served that model in every session of the week, AND its model was
              fitted to at least the rule's `min_days` days of this platform's own recordings: its report
              says how many days came from where, and an imported day counts for nothing (ADR 0028). A
              model file that says where its rows came from (version 2, ADR 0022) has to say the same days
              as its report, or no day of it is counted; a file of the older version does not say, and the
              report alone is read. Every one of these is checked here as well as in the rule; none is
              assumed from another.
  keep        anything else. The week's challengers leave and the registered candidates take their places.

Every change is one line of the lineup journal, with the hash of the decision report it rests on, and one
page. A running trader is asked, by a request file, to start its next session from the new lineup. A week is
decided once: a second run in the same weekend is refused, so that it cannot move the lineup a second time.

Nothing here reaches a venue, places an order or sets a stage. The trader steps a place down when its model
changes (hftlab.trader.stages), and the way up is its stage rule again.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final, Protocol

from hftlab.calendar import CalendarError, load_calendar, new_york
from hftlab.core import CASH_SCALE, Clock, WallClock
from hftlab.journal import JournalError, canonical
from hftlab.journal import read as read_journal
from hftlab.learn.config import LearnConfigError, Settings, load
from hftlab.learn.promote import KEEP, PROMOTE, ROLL_BACK, Rule, ShadowResult, decide
from hftlab.learn.train import keep_report
from hftlab.lineup import journal as lineups
from hftlab.lineup.journal import Lineup, LineupError, Named
from hftlab.lineup.serve import CHAMPION, places
from hftlab.lineup.serve import load as load_model
from hftlab.models.file import UNKNOWN, Model
from hftlab.ops import host
from hftlab.ops.alerts import Alerts, sender_from
from hftlab.record.format import CorruptSegment, instrument_of
from hftlab.record.origin import RECORDED
from hftlab.risk import RiskConfigError, RiskNotInForce, load_risk_config
from hftlab.trader.config import Settings as TraderSettings
from hftlab.trader.config import TraderConfigError
from hftlab.trader.config import load as load_trader
from hftlab.trader.stages import day_of
from hftlab.trader.tape import REQUESTS, leave_request

CONFIG: Final = Path("config/learn.yaml")
TRADER: Final = Path("config/trader.yaml")
RISK: Final = Path("config/risk.yaml")
CALENDAR: Final = Path("config/calendar")
RECORDS: Final = Path("research/decisions")
REPORT_VERSION: Final = 1
OPERATOR: Final = "the weekly decision"
DECISIONS: Final = (lineups.PROMOTE, lineups.KEEP, lineups.ROLL_BACK)
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
_LOSS: Final = ("DAILY_LOSS_SLEEVE", "DRAWDOWN_SLEEVE")  # the latches that are one sleeve's own (hftlab.risk)
_APPROVED = re.compile(r"(?m)^approved_by:[ \t]*owner[ \t]*$")
# why a challenger that the rule passed is still not promoted
RULE_NOT_IN_FORCE, NO_RECORD, NO_LIMITS = "rule_not_in_force", "decision_record_missing", "limits_not_in_force"
BREAKS, PLACEHOLDER, NOT_LOADED = "reconciliation_breaks", "report_rests_on_placeholders", "model_cannot_be_loaded"
NOT_SERVED, NO_PLACE, FEES = "model_not_served_all_week", "no_place_configured", "fees_not_in_quote_currency"
SEVERAL_PAIRS: Final = "traded_more_than_one_pair"


class DecideRefused(Exception):
    """The decision will not be made as things are. A person is needed; a retry is not."""


class Notify(Protocol):
    """How the owner is told of a change. `hftlab.ops.alerts.Alerts` is one."""

    def fire(self, name: str, title: str, message: str, priority: int = 4) -> bool: ...


# ---- the week, read from the trader's journals -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Week:
    label: str  # the New York date of the rollover that ends the weekend the decision is made in
    days: tuple[str, ...]  # the day folders read for it, oldest first


@dataclass(slots=True)
class Evidence:
    """What the trader's journals say of one week, by sleeve."""

    by_day: dict[str, dict[str, dict[int, list[int]]]] = field(default_factory=dict)  # sleeve -> pair -> day -> trips
    served: dict[str, set[str]] = field(default_factory=dict)  # sleeve -> the models its sessions served; "" none
    latched: dict[str, set[str]] = field(default_factory=dict)  # sleeve -> the models serving when its latch tripped
    breaks: int = 0
    sessions: int = 0
    fees_elsewhere: dict[str, int] = field(default_factory=dict)  # sleeve -> fees charged in another currency
    # sleeve -> (the model that expected it, what it expected, what the trip made), for the drift monitor
    expected: dict[str, list[tuple[str, int, int]]] = field(default_factory=dict)

    def result(self, sleeve: str | None, pair: str | None, *, latch: bool = False, own_days: int = 0) -> ShadowResult:
        """What a sleeve made in one pair. Results of two pairs are in two currencies, and are never added."""
        by_day = dict(self.by_day.get(sleeve or "", {}).get(pair or "", {}))
        return ShadowResult(by_day, self.breaks, latch, own_days)

    def pairs(self, sleeve: str | None) -> list[str]:
        return sorted(self.by_day.get(sleeve or "", {}))


def week_of(now_ns: int, folders: Sequence[str]) -> Week:
    """The week a decision at `now_ns` is about: the seven New York dates up to the end of the trading day in hand."""
    end = new_york(day_of(now_ns)).date()
    first = (end - timedelta(days=6)).isoformat()
    return Week(end.isoformat(), tuple(sorted(name for name in folders if first <= name <= end.isoformat())))


class _Trips:
    """Round trips of the shadow book, from flat to flat, in millionths of the quote currency after its fees."""

    def __init__(self) -> None:
        self._scale: dict[str, tuple[int, str]] = {}  # pair -> (millionths per price unit, quote currency)
        self._open: dict[tuple[str, str], tuple[int, int, list[str]]] = {}  # (sleeve, pair) -> quantity, cash, fills
        self._done: list[tuple[str, str, int, int, list[str], tuple[str, int] | None]] = []  # ..., what was expected
        self._fees: dict[str, tuple[int, str]] = {}
        self._said: dict[tuple[str, str], tuple[str, int]] = {}  # (sleeve, pair) -> the last entry's model and hope
        self._hoped: dict[tuple[str, str], tuple[str, int]] = {}  # the same, of the trip that is open

    def expect(self, entry: Mapping[str, object], model: str) -> None:
        """An entry was decided with an expectation. It belongs to the trip the entry opens, if it opens one."""
        expected = entry.get("expected_micro")
        if entry.get("leg") == "entry" and type(expected) is int:
            self._said[str(entry.get("sleeve")), str(entry.get("pair"))] = (model, expected)

    def describe(self, described: object) -> None:
        for entry in described if isinstance(described, list) else []:
            instrument = instrument_of(entry)
            self._scale[instrument.pair] = (10 ** (CASH_SCALE - instrument.price_scale), instrument.quote)

    def fill(self, entry: Mapping[str, object]) -> None:
        sleeve, pair, fill_id = str(entry.get("sleeve")), str(entry.get("pair")), str(entry.get("fill_id"))
        quantity, price, at = entry.get("quantity"), entry.get("price"), entry.get("at_ns")
        if type(quantity) is not int or type(price) is not int or type(at) is not int or pair not in self._scale:
            raise DecideRefused(f"a shadow fill of {sleeve} in {pair} cannot be read: {dict(entry)!r}")
        sign = 1 if entry.get("side") == "BUY" else -1
        held, cash, fills = self._open.get((sleeve, pair), (0, 0, []))
        closing = min(quantity, abs(held)) if held * sign < 0 else 0
        if closing:
            held, cash, fills = held + sign * closing, cash - sign * closing * price, [*fills, fill_id]
            if held == 0:
                # A fill that goes through zero closes this trip, with its whole fee, and opens the next.
                self._done.append((sleeve, pair, at, cash, fills, self._hoped.pop((sleeve, pair), None)))
                cash, fills = 0, []
        opening = quantity - closing
        if opening:
            if held == 0 and (sleeve, pair) in self._said:
                self._hoped[sleeve, pair] = self._said.pop((sleeve, pair))
            held, cash, fills = held + sign * opening, cash - sign * opening * price, [*fills, fill_id]
        self._open[sleeve, pair] = (held, cash, fills)

    def fee(self, entry: Mapping[str, object]) -> None:
        amount = entry.get("commission")
        if type(amount) is int:
            self._fees[str(entry.get("fill_id"))] = (amount, str(entry.get("currency")))

    def closed(self) -> list[tuple[str, str, int, int, int, tuple[str, int] | None]]:
        """(sleeve, pair, when it closed, its result, fees in another currency, what was expected), as they closed."""
        out = []
        for sleeve, pair, at, cash, fills, hoped in self._done:
            per_unit, quote = self._scale[pair]
            charged = [self._fees[fill_id] for fill_id in dict.fromkeys(fills) if fill_id in self._fees]
            ours = sum(amount for amount, currency in charged if currency == quote)
            elsewhere = sum(1 for _, currency in charged if currency != quote)
            out.append((sleeve, pair, at, cash * per_unit - ours, elsewhere, hoped))
        return out


def read_week(state_dir: Path, now_ns: int) -> tuple[Week, Evidence]:
    """The week's evidence. Every day folder up to the week's end is read, so a trip is seen from its first fill."""
    days = state_dir / "days"
    folders = sorted(path.name for path in days.iterdir() if path.is_dir()) if days.is_dir() else []
    week = week_of(now_ns, folders)
    found, trips = Evidence(), _Trips()
    serving: dict[str, str] = {}
    last = week.days[-1] if week.days else ""
    for folder in folders:
        if folder > last:
            break
        try:
            entries = read_journal(days / folder / "decisions.jsonl")[1]
        except JournalError as exc:
            raise DecideRefused(f"the decisions of {folder} do not verify; nothing is decided on them: {exc}") from exc
        inside = folder in week.days
        for entry in entries:
            kind = entry.get("type")
            if kind == "start":
                trips.describe(entry.get("instruments"))
                said = entry.get("serving")
                serving = {str(k): str(v or "") for k, v in said.items()} if isinstance(said, dict) else {}
                if inside:
                    found.sessions += 1
                    for sleeve, model in serving.items():
                        found.served.setdefault(sleeve, set()).add(model)
            elif kind == "decision":
                trips.expect(entry, serving.get(str(entry.get("sleeve")), ""))
            elif kind == "shadow_fill":
                trips.fill(entry)
            elif kind == "shadow_commission":
                trips.fee(entry)
            elif kind == "latch" and inside and entry.get("tripped") == 1:
                parts = str(entry.get("latch")).split("/")  # DAILY_LOSS_SLEEVE/<sleeve>/<currency>
                if parts[0] in _LOSS and len(parts) > 1:
                    found.latched.setdefault(parts[1], set()).add(serving.get(parts[1], ""))
            elif kind == "break" and inside and entry.get("broken") == 1:
                found.breaks += 1
    labels = set(week.days)
    for sleeve, pair, at, result, elsewhere, hoped in trips.closed():
        day = day_of(at)
        if new_york(day).date().isoformat() in labels:
            found.by_day.setdefault(sleeve, {}).setdefault(pair, {}).setdefault(day, []).append(result)
            if hoped is not None:
                found.expected.setdefault(sleeve, []).append((*hoped, result))
            if elsewhere:
                found.fees_elsewhere[sleeve] = found.fees_elsewhere.get(sleeve, 0) + elsewhere
    return week, found


# ---- the decision ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gates:
    """What a promotion needs beside the rule's own checks. Each is looked up, none is assumed."""

    rule_in_force: bool  # every part of the rule is written in config/learn.yaml
    record: bool  # the decision record the rule names exists, with the owner's approval
    limits: bool  # the risk limits are in force
    unfit: Mapping[str, str] = field(default_factory=dict)  # a challenger's model -> why its report cannot carry it
    # a challenger's model -> the days it was fitted to, by source (`recorded`, `imported:...`): what its report
    # says, and counted only where a model file that says its own days says the same (`vouched_days`)
    fitted: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    # a challenger's model -> the days its own file says, by source; UNKNOWN for a file that does not say
    files: Mapping[str, Mapping[str, int] | str] = field(default_factory=dict)

    def own_days(self, model: str) -> int:
        """The days of this platform's own recordings a model was fitted to. Imported days are not among them."""
        return self.fitted.get(model, {}).get(RECORDED, 0)


@dataclass(frozen=True, slots=True)
class Judged:
    """One challenger, and why it was not promoted. No reason: the rule and every gate passed it."""

    model: str
    sleeve: str
    reasons: tuple[str, ...]
    evidence: Mapping[str, int]

    def plain(self) -> dict[str, object]:
        said = {"model": self.model, "sleeve": self.sleeve, "reasons": list(self.reasons)}
        return {**said, "evidence": dict(self.evidence)}


@dataclass(frozen=True, slots=True)
class Outcome:
    action: str
    after: Lineup
    reasons: tuple[str, ...]
    promoted: Named | None = None
    challengers: tuple[Judged, ...] = ()  # what was found for each, for the report

    @property
    def said(self) -> str:
        """The conclusion, as the command prints it."""
        if self.action == ROLL_BACK:
            return "roll back: the champion breached its loss latch"
        if self.action == PROMOTE and self.promoted is not None:
            return f"promote: {self.promoted.model[:12]}"
        return "keep: " + ("rule not in force" if RULE_NOT_IN_FORCE in self.reasons else ", ".join(self.reasons))


def _sleeve_at(held: Mapping[str, tuple[str, int]], place: tuple[str, int]) -> str | None:
    return next((name for name, where in held.items() if where == place), None)


def weigh(
    lineup: Lineup, evidence: Evidence, held: Mapping[str, tuple[str, int]], rule: Rule, gates: Gates, baseline: str
) -> Outcome:
    """Promote, keep or roll back, and the lineup that follows. Reads nothing and writes nothing."""
    champion_sleeve = _sleeve_at(held, (CHAMPION, 0))
    serving = lineup.champion.model if lineup.champion else None
    breached = serving is not None and serving in evidence.latched.get(champion_sleeve or "", ())
    standard_sleeve = champion_sleeve if lineup.champion else baseline
    nothing = ShadowResult({})
    if lineup.champion is not None and decide(ShadowResult({}, 0, breached), nothing, rule).action == ROLL_BACK:
        return Outcome(ROLL_BACK, lineup.rolled_back(), ("champion_latch_breached",))
    found: list[Judged] = []
    best: tuple[int, Named] | None = None
    for number, challenger in enumerate(lineup.challengers):
        sleeve = _sleeve_at(held, ("challenger", number))
        latched = challenger.model in evidence.latched.get(sleeve or "", ())
        traded = evidence.pairs(sleeve)
        pair = traded[0] if len(traded) == 1 else None  # the two are compared in that pair, and so in one currency
        own = gates.own_days(challenger.model)
        theirs = evidence.result(sleeve, pair, latch=latched, own_days=own)
        verdict = decide(evidence.result(standard_sleeve, pair), theirs, rule)
        more = [SEVERAL_PAIRS] if len(traded) > 1 else []
        if not gates.rule_in_force or rule.decision is None:
            more.append(RULE_NOT_IN_FORCE)
        if not gates.record:
            more.append(NO_RECORD)
        if not gates.limits:
            more.append(NO_LIMITS)
        if evidence.breaks:
            more.append(BREAKS)
        if challenger.model in gates.unfit:
            more.append(gates.unfit[challenger.model])
        if sleeve is None:
            more.append(NO_PLACE)
        elif evidence.served.get(sleeve, set()) != {challenger.model}:
            more.append(NOT_SERVED)
        if evidence.fees_elsewhere.get(sleeve or "") or evidence.fees_elsewhere.get(standard_sleeve or ""):
            more.append(FEES)
        failed = verdict.reasons if verdict.action != PROMOTE else ()
        reasons = (*failed, *(reason for reason in more if reason not in failed))
        found.append(Judged(challenger.model, sleeve or "", reasons, verdict.evidence))
        ahead = verdict.evidence.get("advantage_lower_micro", 0)
        if verdict.action == PROMOTE and not reasons and (best is None or ahead > best[0]):
            best = (ahead, challenger)
    if best is not None:
        return Outcome(PROMOTE, lineup.promoted(best[1]), ("all_checks_passed",), best[1], tuple(found))
    why = sorted({reason for one in found for reason in one.reasons}) if found else ["no_challenger"]
    if not gates.rule_in_force and RULE_NOT_IN_FORCE not in why:
        why = [RULE_NOT_IN_FORCE, *why]
    return Outcome(KEEP, lineup.kept(), tuple(why), None, tuple(found))


def approved(records: Path, decision: str | None) -> bool:
    """Is there a decision record of this id under `records`, in which the owner approved?"""
    if decision is None or not records.is_dir():
        return False
    for path in sorted(records.iterdir()):
        if path.is_file() and re.fullmatch(rf"{decision}(-[a-z0-9]+)*\.md", path.name):
            return _APPROVED.search(path.read_text(encoding="utf-8", errors="replace")) is not None
    return False


def unfit(lineup: Lineup, registry: Path) -> dict[str, str]:
    """For each challenger that cannot carry a promotion: why. Its files are read by hash, like the trader's."""
    return read_reports(lineup, registry)[0]


def fitted_days(report: Mapping[str, object]) -> dict[str, int]:
    """The days a report says its model was fitted to, by source. A report that does not say has none."""
    sources = report.get("sources")
    model = sources.get("model") if isinstance(sources, dict) else None
    days = model.get("days") if isinstance(model, dict) else None
    if not isinstance(days, dict):
        return {}
    return {str(key): count for key, count in sorted(days.items()) if type(count) is int and count >= 0}


def vouched_days(model: Model, report: Mapping[str, object]) -> dict[str, int]:
    """The days a model was fitted to, by source, as far as its two files vouch for them.

    The report says them, and a model file of version 2 says them under its own hash. Where both speak they
    must say the same, or no day is counted at all. A file of version 1 does not say: its split is unknown,
    and the report alone is read, as it was before the file had the field.
    """
    said, own = fitted_days(report), model.fitted("days")
    return said if own is None or own == said else {}


def read_reports(
    lineup: Lineup, registry: Path
) -> tuple[dict[str, str], dict[str, dict[str, int]], dict[str, dict[str, int] | str]]:
    """Each challenger's files, read once: why it cannot carry a promotion, the days it was fitted to, and the
    days its model file says for itself."""
    out: dict[str, str] = {}
    fitted: dict[str, dict[str, int]] = {}
    files: dict[str, dict[str, int] | str] = {}
    for challenger in lineup.challengers:
        try:
            model, report, _ = load_model(challenger, registry)
        except LineupError:
            out[challenger.model] = NOT_LOADED
            continue
        if report.get("not_a_result") != 0:
            out[challenger.model] = PLACEHOLDER
        fitted[challenger.model] = vouched_days(model, report)
        own = model.fitted("days")
        files[challenger.model] = UNKNOWN if own is None else own
    return out, fitted, files


# ---- the command ----------------------------------------------------------------------------------------------------


def say(text: str) -> None:
    print(f"promote: {text}", file=sys.stderr, flush=True)


@dataclass(frozen=True, slots=True)
class Desk:
    """Where everything a decision reads and writes is."""

    learn: Settings
    trader: TraderSettings
    limits: bool
    records: Path
    notify: Notify

    @property
    def registry(self) -> Path:
        return self.trader.registry or self.learn.state_dir

    @property
    def held(self) -> dict[str, tuple[str, int]]:
        return places(self.trader.sleeves)


def _report(desk: Desk, week: Week, lineup: Lineup, evidence: Evidence, gates: Gates, outcome: Outcome) -> bytes:
    rule = desk.learn.rule
    said: dict[str, object] = {
        "kind": "decision-report",
        "version": REPORT_VERSION,
        "week": week.label,
        "days": list(week.days),
        "sessions": evidence.sessions,
        "lineup_before": {"head": lineup.head, **lineup.plain()},
        "rule": {
            "in_force": int(gates.rule_in_force), "decision": rule.decision, "min_trades": rule.min_trades,
            "min_days": rule.min_days, "max_drawdown_micro": rule.max_drawdown_micro, "draws": rule.draws,
            "level_ppm": rule.level_ppm, "seed": rule.seed,
        },
        "gates": {"record": int(gates.record), "limits": int(gates.limits), "unfit": dict(sorted(gates.unfit.items()))},
        "fitted_days_by_source": {model: dict(days) for model, days in sorted(gates.fitted.items())},
        "model_file_days_by_source": {
            model: days if isinstance(days, str) else dict(days) for model, days in sorted(gates.files.items())
        },
        "baseline_sleeve": desk.learn.baseline_sleeve,
        "reconciliation_breaks": evidence.breaks,
        "latched": {sleeve: sorted(models) for sleeve, models in sorted(evidence.latched.items())},
        "trades": {
            sleeve: {pair: sum(map(len, days.values())) for pair, days in sorted(pairs.items())}
            for sleeve, pairs in sorted(evidence.by_day.items())
        },
        "challengers": [one.plain() for one in outcome.challengers],
        "action": outcome.action,
        "reasons": list(outcome.reasons),
        "lineup_after": outcome.after.plain(),
    }  # fmt: skip
    return (canonical(said) + "\n").encode("ascii")


def gates_of(desk: Desk, lineup: Lineup) -> Gates:
    in_force = desk.learn.rule_in_force and desk.learn.rule.decision is not None
    record = approved(desk.records, desk.learn.rule.decision)
    return Gates(in_force, record, desk.limits, *read_reports(lineup, desk.registry))


def run(desk: Desk, now_ns: int, *, dry_run: bool = False) -> tuple[int, Outcome]:
    """One weekly decision. Returns the exit code and what was concluded."""
    path = desk.learn.lineup
    lineup = lineups.read(path)
    week, evidence = read_week(desk.trader.state_dir, now_ns)
    if any(line.get("action") in DECISIONS and line.get("week") == week.label for line in lineups.history(path)):
        raise DecideRefused(f"the week of {week.label} is already decided; a week is decided once")
    gates = gates_of(desk, lineup)
    outcome = weigh(lineup, evidence, desk.held, desk.learn.rule, gates, desk.learn.baseline_sleeve)
    report = _report(desk, week, lineup, evidence, gates, outcome)
    said = {"week": week.label, "action": outcome.action, "reasons": list(outcome.reasons), "said": outcome.said}
    if dry_run:
        sys.stdout.write(report.decode("ascii"))
        say(f"dry run, nothing written. {outcome.said}")
        return EXIT_OK, outcome
    if outcome.after.same_models(lineup):
        say(f"{outcome.said}. The lineup is unchanged, and nothing is written.")
        print(json.dumps({**said, "changed": False, "report": None}, sort_keys=True), flush=True)
        return EXIT_OK, outcome
    digest = keep_report(desk.learn.reports, report)
    lineups.append(
        path, outcome.after, action=outcome.action, at_ns=now_ns, reasons=outcome.reasons, week=week.label,
        decision=desk.learn.rule.decision if outcome.action == PROMOTE else None, report=digest,
        evidence={
            "challengers": [one.plain() for one in outcome.challengers], "sessions": evidence.sessions,
            "breaks": evidence.breaks,
        },
    )  # fmt: skip
    leave_request(desk.trader.state_dir / REQUESTS, "lineup", "", outcome.action, OPERATOR)
    champion = outcome.after.champion.model[:12] if outcome.after.champion else "none"
    text = f"{outcome.said}. Champion {champion}, {len(outcome.after.challengers)} challenger(s). Report {digest[:12]}."
    key = f"lineup.{outcome.action}.{week.label}"
    desk.notify.fire(key, f"Lineup: {outcome.action}", text, _PRIORITY[outcome.action])
    say(text)
    print(json.dumps({**said, "changed": True, "report": digest}, sort_keys=True), flush=True)
    return EXIT_OK, outcome


_PRIORITY: Final = {KEEP: 2, PROMOTE: 4, ROLL_BACK: 5}


def drill(desk: Desk, now_ns: int) -> int:
    """Prove the rollback: promote on a copy, breach the champion's latch, roll back, and check what came back.

    The real journal gets one line that says the drill ran and what it found. Its lineup is the one in force
    before, unchanged: nothing the trader serves moves.
    """
    path = desk.learn.lineup
    real = lineups.read(path)
    with tempfile.TemporaryDirectory(prefix="hft-drill-") as scratch:
        copy = Path(scratch) / lineups.FILE
        if path.is_file():
            shutil.copyfile(path, copy)
        lineup = lineups.read(copy)
        if not lineup.challengers and lineup.candidates:
            lineup = lineups.append(copy, lineup.kept(), action=lineups.KEEP, at_ns=now_ns, reasons=("drill",))
        if not lineup.challengers:
            raise DecideRefused("the lineup holds no challenger and no candidate to drill with; train one first")
        subject, before = lineup.challengers[0], lineup.champion
        risen = lineup.promoted(subject)
        promoted = lineups.append(copy, risen, action=lineups.PROMOTE, at_ns=now_ns, reasons=("drill",))
        held = desk.held if _sleeve_at(desk.held, (CHAMPION, 0)) else {"drill": (CHAMPION, 0)}
        sleeve = _sleeve_at(held, (CHAMPION, 0)) or ""
        forced = Evidence(latched={sleeve: {subject.model}}, served={sleeve: {subject.model}})
        gates = Gates(desk.learn.rule_in_force, False, False)
        outcome = weigh(promoted, forced, held, desk.learn.rule, gates, desk.learn.baseline_sleeve)
        checks = {"rolled_back": int(outcome.action == ROLL_BACK), "restored": 0, "gone": 0, "loads": 0}
        if outcome.action == ROLL_BACK:
            after = lineups.append(copy, outcome.after, action=lineups.ROLL_BACK, at_ns=now_ns)
            checks["restored"] = int(after.champion == before and after.previous == lineup.previous)
            checks["gone"] = int(subject not in (after.champion, *after.previous))
            try:
                for one in (subject, *((before,) if before else ())):
                    load_model(one, desk.registry)
                checks["loads"] = 1
            except LineupError as exc:
                say(f"drill: a model of the lineup cannot be loaded: {exc}")
    passed = all(checks.values())
    said: dict[str, object] = {
        "kind": "drill-report", "version": REPORT_VERSION, "lineup": {"head": real.head, **real.plain()},
        "subject": subject.model, "restored": before.model if before else None, "checks": checks, "passed": int(passed),
    }  # fmt: skip
    digest = keep_report(desk.learn.reports, (canonical(said) + "\n").encode("ascii"))
    after_real = lineups.append(
        path, real, action=lineups.DRILL, at_ns=now_ns, reasons=("passed" if passed else "failed",), report=digest,
        evidence={"subject": subject.model, **checks},
    )  # fmt: skip
    assert after_real.same_models(real)
    found = json.dumps(checks, sort_keys=True)
    text = f"Rollback drill {'passed' if passed else 'FAILED'} on {subject.model[:12]}: {found}."
    desk.notify.fire(f"lineup.drill.{now_ns}", "Rollback drill", text, 2 if passed else 5)
    say(text + " The lineup in force is unchanged.")
    print(json.dumps({"drill": checks, "passed": passed, "report": digest}, sort_keys=True), flush=True)
    return EXIT_OK if passed else EXIT_FAILED


def limits_in_force(path: Path) -> bool:
    try:
        load_risk_config(path)
    except RiskNotInForce:
        return False
    return True


def main(argv: Sequence[str] | None = None, clock: Clock | None = None, notify: Notify | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.learn.promote", description="The weekly lineup decision.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--trader", type=Path, default=TRADER, help="the trader's settings: journals and places")
    parser.add_argument("--risk", type=Path, default=RISK)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--records", type=Path, default=RECORDS, help="where the decision records are")
    parser.add_argument("--dry-run", action="store_true", help="decide and print; write nothing")
    parser.add_argument("--drill", action="store_true", help="prove the rollback on a copy of the lineup journal")
    args = parser.parse_args(argv)
    clock = clock or WallClock()
    try:
        learn, trader = load(args.config), load_trader(args.trader)
        if learn.lineup != trader.state_dir / lineups.FILE:
            read_by = trader.state_dir / lineups.FILE
            raise DecideRefused(f"{args.config} names {learn.lineup}, and the trader reads {read_by}")
        now = clock.now_ns()
        if load_calendar(args.calendar).is_open(now) and not args.dry_run:
            raise DecideRefused("the market is open: the week is decided at the weekend, when it is over")
        if notify is None:
            settings = host.settings()
            notify = Alerts(host.state_dir(settings["HFT_STATE_DIR"]) / "alerts", sender_from(settings, say), clock)
        desk = Desk(learn, trader, limits_in_force(args.risk), args.records, notify)
        if args.drill:
            return drill(desk, now)
        return run(desk, now, dry_run=args.dry_run)[0]
    except (DecideRefused, LearnConfigError, TraderConfigError, RiskConfigError, CalendarError) as exc:
        say(f"refused: {exc}")
        return EXIT_REFUSED
    except (LineupError, JournalError, CorruptSegment, OSError) as exc:
        say(f"FAILED: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
