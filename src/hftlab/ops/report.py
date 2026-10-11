"""`python -m hftlab.ops.report`: the daily summary and the weekly scorecard, from the journals and nothing else.

    python -m hftlab.ops.report daily            the trading day that closed last, printed and written to a file
    python -m hftlab.ops.report weekly           the trading week that closed last
    python -m hftlab.ops.report due --send       what `hft-report.service` runs after the 17:00 New York rollover:
                                                 the daily summary, and the weekly scorecard as well when the day
                                                 that closed was the last of its week. Each is also sent as a page

The daily summary, for the trading day between two 17:00 New York rollovers:

  * the recorder: for each pair, how much of the market's open time was recorded, and the gaps nobody explained;
  * the trader: each sleeve's stage, its fills in the shadow book with the broker book's beside them, its
    result by currency from the SHADOW book, which is the result of record (ADR 0003), the desk's result in
    both books, the slippage report, reconciliation breaks, latches tripped and reset, stage changes;
  * the disk: the recording against its quota, and the tapes against their soft limit, their hard limit and
    their quota. The two limits are the ones the trader journalled when its last session of the window began.

The weekly scorecard, for the week that closed on a Friday: the same summed over its trading days, each
sleeve against the control sleeve, the changes of the lineup since the Friday before (the weekend's training
and its decision) with the training report each rests on, and what the journals hold so far towards the exit
gates of F2, F4 and F6. A count towards a gate is evidence, not the gate: the roadmap says what closes one.

Three rules for every number here:

  * Exact integers. Money is millionths of a currency, by currency, never added across currencies and never a
    float. A share is parts per million, rounded down.
  * A part that is missing is absent, and the report says it is absent and why. It is never shown as zero.
    A day whose last session did not say it stopped has fills that can be counted and no result that can be
    quoted: the count is given, the result is absent.
  * Nothing private and no free text. The report is built from numbers and from names and codes of this
    platform's own (a pair, a sleeve, a latch, a cause, a decision id), each checked to be shaped like one. A
    venue's message, an operator's name, a path and anything else that is not so shaped never enters it, and
    what is shaped like an account id is masked on the way out as every page's is (hftlab.ops.alerts).

Everything above `gather` is a function of journal lines, a calendar and a window: it reads no file and no
clock. `gather` reads; the command writes one file per report under `reports/` in the state directory and,
when asked, sends the text through the sender the alerts use. It keeps no state: the same journals give the
same report, so a report is rebuilt, not backed up.

Exit codes: 0 written (and sent, when asked), 1 a page could not be delivered, 2 a setting could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from hftlab.calendar import Calendar, CalendarError, new_york, next_close
from hftlab.calendar.week import ROLLOVER_TIME, instant, last_rollover
from hftlab.core import Clock, WallClock
from hftlab.journal import JournalError
from hftlab.journal import read as read_journal
from hftlab.lineup.journal import DRILL, KEEP, PROMOTE, REGISTER, ROLL_BACK, LineupError
from hftlab.lineup.journal import history as lineup_history
from hftlab.ops import host, status
from hftlab.ops.alerts import Page, Sender, scrub, sender_from
from hftlab.record.gaps import UNEXPLAINED
from hftlab.record.store import days as store_days
from hftlab.record.store import measure
from hftlab.record.store import read_journal as read_store_journal

SECOND: Final = 1_000_000_000
DAY_NS: Final = 86_400 * SECOND
MILLION: Final = 1_000_000
F2_DAYS, F4_DAYS, F6_CYCLES = 5, 5, 2  # the roadmap's numbers: what each count below is a count towards
LEARN_CONFIG: Final = Path("config/learn.yaml")
FOLDER: Final = "reports"  # in the state directory
PAGE_MAX: Final = 3_500  # ENGINEERING DEFAULT: a push notification's body is a few thousand characters at most
EXIT_OK, EXIT_NOT_SENT, EXIT_UNREADABLE = 0, 1, 2
NOT_TEXT: Final = "[text]"
DESK: Final = "desk"
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}")
_MARKET_CLOSED: Final = "market_closed"
_DECIDED: Final = (PROMOTE, KEEP, ROLL_BACK)
_SLIPPAGE: Final = ("count", "sum_increments", "inexact", "broker_only", "shadow_only", "unequal")

Line = Mapping[str, object]
Money = Mapping[str, int]


def name(value: object) -> str:
    """A name or a code as the report may show it: itself when it is shaped like one of ours, else `[text]`."""
    return value if isinstance(value, str) and _NAME.fullmatch(value) else NOT_TEXT


def _whole(value: object) -> int:
    return value if type(value) is int else 0


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: object) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


def _money(value: object) -> dict[str, int] | None:
    """Amounts by currency, when that is what the value is: whole numbers under names shaped like a currency's."""
    if not isinstance(value, dict) or not all(type(amount) is int for amount in value.values()):
        return None
    return {name(code): amount for code, amount in sorted(value.items())}


def _add(total: dict[str, int], more: Money) -> None:
    for code, amount in more.items():
        total[code] = total.get(code, 0) + amount


# ---- the windows -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    """A stretch of time with a name: a trading day by the New York date of its close, or a week by its ISO week."""

    label: str
    start_ns: int
    end_ns: int


def _day_closing(close_ns: int) -> Span:
    return Span(new_york(close_ns).date().isoformat(), last_rollover(close_ns - 1), close_ns)


def last_trading_day(calendar: Calendar, now_ns: int) -> Span:
    """The last trading day that had closed at `now_ns`: between two rollovers, with the market open in it at all."""
    span = _day_closing(last_rollover(now_ns))
    for _ in range(14):
        if calendar.open_ns_between(span.start_ns, span.end_ns) > 0:
            return span
        span = _day_closing(span.start_ns)
    raise CalendarError("no trading day in the two weeks before the report: the calendar closes every one")


def last_trading_week(calendar: Calendar, now_ns: int) -> tuple[Span, tuple[Span, ...]]:
    """The last week that had closed at `now_ns`, from the Friday close before it, and its trading days.

    The week's span runs from one Friday 17:00 New York to the next, so it holds the weekend whose training
    and decision the week was traded on. New York moves its clock on a Sunday before the week opens, so the
    five rollovers of a week are the same local time on five dates in a row.
    """
    close = next_close(now_ns - 15 * DAY_NS)
    while (later := next_close(close)) <= now_ns:
        close = later
    friday = new_york(close).date()
    before = instant(friday - timedelta(days=7), ROLLOVER_TIME)
    year, week, _ = friday.isocalendar()
    closes = [instant(friday - timedelta(days=back), ROLLOVER_TIME) for back in (4, 3, 2, 1, 0)]
    found = [_day_closing(at) for at in closes]
    traded = tuple(day for day in found if calendar.open_ns_between(day.start_ns, day.end_ns) > 0)
    return Span(f"{year}-W{week:02d}", before, close), traded


def trading_days(calendar: Calendar, first_ns: int, last_ns: int) -> list[Span]:
    """Every trading day that closed after `first_ns` and at or before `last_ns`, oldest first."""
    out = []
    span = _day_closing(last_rollover(last_ns))
    while span.end_ns > first_ns:
        if calendar.open_ns_between(span.start_ns, span.end_ns) > 0:
            out.append(span)
        span = _day_closing(span.start_ns)
    return out[::-1]


# ---- the recorder ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PairRecording:
    pair: str
    open_ns: int  # how long the market was open in the window
    recorded_ns: int  # how much of that the recording covers
    unexplained: int  # gaps with no reason that touch the window
    unexplained_ns: int  # the open time they cover

    @property
    def share_ppm(self) -> int | None:
        """Recorded over open, in millionths, rounded down. None when the market was shut for the whole window."""
        return self.recorded_ns * MILLION // self.open_ns if self.open_ns else None


@dataclass(frozen=True, slots=True)
class Recording:
    pairs: tuple[PairRecording, ...]
    starts: int  # times the recorder was started in the window
    stops: int  # times it said it stopped


def _merged(spans: Iterable[tuple[int, int]], window: Span) -> list[tuple[int, int]]:
    """The spans cut to the window and joined where they touch, so that no moment is counted twice."""
    cut = sorted((max(a, window.start_ns), min(b, window.end_ns)) for a, b in spans)
    out: list[tuple[int, int]] = []
    for a, b in cut:
        if a >= b:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def recording(lines: Sequence[Line], calendar: Calendar, window: Span) -> Recording | None:
    """How much of the window's open time the recorder's journals say was recorded, for each pair.

    `lines` are the store's journal lines, oldest first, from as far back as there are any. The recorder was
    up from each `start` to the `stop` or the `start` that follows it; the last run, when it never said it
    stopped, is counted up to the last line it wrote (it writes one an hour at least). When the oldest line
    is not a `start`, because the journal of the day the run began in has been handed to the offload, the run
    is counted from that oldest line. From that is taken every gap whose reason is not a closed market. None
    when no journal line says anything about the window: the part is absent.
    """
    runs: list[tuple[int, int]] = []
    since: int | None = None
    last_word = 0
    timed = False  # a line with a time of its own has been seen
    starts = stops = 0
    pairs: set[str] = set()
    gaps: list[tuple[str, int, int, str]] = []
    for line in lines:
        kind, at = line.get("type"), line.get("at_ns")
        if kind == "gap":
            start, end = line.get("start_ns"), line.get("end_ns")
            if type(start) is int and type(end) is int:
                gaps.append((name(line.get("pair")), start, end, str(line.get("reason"))))
                pairs.add(name(line.get("pair")))
                last_word = max(last_word, end)
            continue
        if kind == "instruments":
            pairs |= {name(one.get("pair")) for one in _list(line.get("instruments")) if isinstance(one, dict)}
        if type(at) is not int:
            continue
        if not timed and kind != "start":
            since = at  # the recorder wrote this line: it was running, and its `start` is no longer here
        timed, last_word = True, max(last_word, at)
        if kind in ("start", "stop"):
            if since is not None:
                runs.append((since, at))
            since = at if kind == "start" else None
            inside = window.start_ns <= at < window.end_ns
            starts, stops = starts + (inside and kind == "start"), stops + (inside and kind == "stop")
    if since is not None:
        runs.append((since, max(since, last_word)))
    up = _merged(runs, window)
    if not up and not any(start < window.end_ns and end > window.start_ns for _, start, end, _ in gaps):
        return None
    edges = [window.start_ns, *(edge for run in up for edge in run), window.end_ns]
    down = list(zip(edges[::2], edges[1::2], strict=True))  # the window less the runs
    open_ns = calendar.open_ns_between(window.start_ns, window.end_ns)
    out = []
    for pair in sorted(pairs):
        silent = [(start, end) for one, start, end, reason in gaps if one == pair and reason != _MARKET_CLOSED]
        lost = sum(calendar.open_ns_between(a, b) for a, b in _merged([*down, *silent], window))
        nobody = _merged(((s, e) for one, s, e, reason in gaps if one == pair and reason == UNEXPLAINED), window)
        count = len({(s, e) for one, s, e, reason in gaps if one == pair and reason == UNEXPLAINED
                     and s < window.end_ns and e > window.start_ns})  # fmt: skip
        out.append(
            PairRecording(pair, open_ns, open_ns - lost, count, sum(calendar.open_ns_between(a, b) for a, b in nobody))
        )
    return Recording(tuple(out), starts, stops)


# ---- the trader ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SleeveDay:
    name: str
    stage: str | None  # at the end of the day's journal
    shadow_fills: int
    broker_fills: int
    net_micro: Money | None  # the day's result in the shadow book, by currency; None: not said (see `Trading.closed`)


@dataclass(frozen=True, slots=True)
class Slippage:
    """Shadow against broker for one sleeve and pair (hftlab.trader.slippage), summed over the sessions."""

    sleeve: str
    pair: str
    count: int
    sum_increments: int
    lowest_increments: int | None
    highest_increments: int | None
    inexact: int
    broker_only: int
    shadow_only: int
    unequal: int


@dataclass(frozen=True, slots=True)
class Trading:
    """One trading day as its decision journal has it."""

    label: str
    sessions: int
    mode: str  # "paper", "shadow_only", or both in turn
    closed: bool  # the day's last session said it stopped, so the books' results at that moment are known
    saw_close: bool  # and it stopped because it saw the day's closing rollover
    sleeves: tuple[SleeveDay, ...]
    shadow_net_micro: Money | None  # the desk's result in the shadow book
    broker_net_micro: Money | None  # the desk's result in the broker book; None: not said, or no broker book
    has_broker_book: bool
    breaks: int
    latches_tripped: tuple[str, ...]
    latches_reset: tuple[str, ...]
    stage_changes: tuple[tuple[str, str, str, str], ...]  # sleeve, before, to, cause
    slippage: tuple[Slippage, ...]


def trading(label: str, entries: Sequence[Line]) -> Trading | None:
    """A day's decision journal, read once. None when the day has no session: the part is absent."""
    sessions = 0
    modes: list[str] = []
    stages: dict[str, str] = {}
    order: list[str] = []
    fills = {"shadow_fill": dict[str, int](), "broker_fill": dict[str, int]()}
    tripped: list[str] = []
    reset: list[str] = []
    changes: list[tuple[str, str, str, str]] = []
    slipped: dict[tuple[str, str], dict[str, int]] = {}
    extremes: dict[tuple[str, str], tuple[int, int]] = {}
    breaks = 0
    last_stop: Line | None = None
    for entry in entries:
        kind, sleeve = entry.get("type"), name(entry.get("sleeve"))
        if kind == "start":
            sessions, last_stop = sessions + 1, None
            mode = name(entry.get("mode"))
            modes += [mode] if mode not in modes else []
            for one in _list(entry.get("sleeves")):
                order += [name(one)] if name(one) not in order else []
            stages.update({name(key): name(value) for key, value in _dict(entry.get("stages")).items()})
        elif kind == "stage":
            stages[sleeve] = name(entry.get("to"))
            changes.append((sleeve, name(entry.get("before")), name(entry.get("to")), name(entry.get("cause"))))
        elif isinstance(kind, str) and kind in fills:
            fills[kind][sleeve] = fills[kind].get(sleeve, 0) + 1
        elif kind == "latch":
            (tripped if entry.get("tripped") == 1 else reset).append(name(entry.get("latch")))
        elif kind == "break":
            breaks += entry.get("broken") == 1
        elif kind == "slippage":
            for one in _list(entry.get("lines")):
                if isinstance(one, dict) and _whole(one.get("count")) + sum(_whole(one.get(k)) for k in _SLIPPAGE[3:]):
                    key = (name(one.get("sleeve")), name(one.get("pair")))
                    total = slipped.setdefault(key, dict.fromkeys(_SLIPPAGE, 0))
                    for part in _SLIPPAGE:
                        total[part] += _whole(one.get(part))
                    if _whole(one.get("count")):
                        low, high = _whole(one.get("lowest_increments")), _whole(one.get("highest_increments"))
                        was = extremes.get(key, (low, high))
                        extremes[key] = (min(was[0], low), max(was[1], high))
        elif kind == "stop":
            last_stop = entry
    if not sessions:
        return None
    said = _dict(last_stop.get("summary")) if last_stop is not None else {}
    nets = {name(one.get("name")): _money(one.get("net_today_micro"))
            for one in _list(said.get("sleeves")) if isinstance(one, dict)}  # fmt: skip
    books = _dict(said.get("books"))
    shadow, broker = books.get("shadow"), books.get("broker")
    sleeves = tuple(
        SleeveDay(
            one, stages.get(one), fills["shadow_fill"].get(one, 0), fills["broker_fill"].get(one, 0), nets.get(one)
        )
        for one in order
    )
    lines = tuple(
        Slippage(
            sleeve, pair, total["count"], total["sum_increments"],
            extremes[sleeve, pair][0] if (sleeve, pair) in extremes else None,
            extremes[sleeve, pair][1] if (sleeve, pair) in extremes else None,
            total["inexact"], total["broker_only"], total["shadow_only"], total["unequal"],
        )
        for (sleeve, pair), total in sorted(slipped.items())
    )  # fmt: skip
    return Trading(
        label, sessions, "+".join(modes), last_stop is not None,
        last_stop is not None and last_stop.get("day_closed") == 1, sleeves,
        _money(shadow.get("net_today_micro")) if isinstance(shadow, dict) else None,
        _money(broker.get("net_today_micro")) if isinstance(broker, dict) else None,
        "paper" in modes, breaks, tuple(tripped), tuple(reset), tuple(changes), lines,
    )  # fmt: skip


def clean_days(entries: Sequence[Line]) -> dict[int, dict[str, bool]]:
    """The stage journal's verdict on each closed trading day, by the rollover that closed it and by sleeve."""
    out: dict[int, dict[str, bool]] = {}
    for entry in entries:
        day = entry.get("day")
        if entry.get("type") == "day" and type(day) is int:
            out.setdefault(day, {})[name(entry.get("sleeve"))] = entry.get("clean") == 1
    return out


# ---- the disk --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Disk:
    what: str
    used_bytes: int
    quota_bytes: int | None  # None: no quota is set for it
    soft_bytes: int | None = None  # the tapes only: from here on the trader opens nothing. None: not journalled
    hard_bytes: int | None = None  # the tapes only: from here on it closes what it holds

    @property
    def percent(self) -> int | None:
        return self.used_bytes * 100 // self.quota_bytes if self.quota_bytes else None

    @property
    def standing(self) -> str | None:
        """Where the bytes stand against the two limits, when both are known."""
        if self.soft_bytes is None or self.hard_bytes is None:
            return None
        if self.used_bytes >= self.hard_bytes:
            return "AT THE HARD LIMIT"
        return "AT THE SOFT LIMIT" if self.used_bytes >= self.soft_bytes else "under both"


# ---- the lineup and the gates ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Training:
    """The few whole numbers of an evaluation report that say what was trained and whether it measures anything."""

    pair: str
    horizon_ns: int
    kind: str
    fitted_rows: int
    trials: int
    not_a_result: bool


@dataclass(frozen=True, slots=True)
class LineupChange:
    at_ns: int
    action: str
    champion: str | None  # the first twelve characters of its hash
    challengers: int
    candidates: int
    decision: str | None
    reasons: tuple[str, ...]
    report: str | None  # the first twelve characters of the hash of the report the line rests on
    training: Training | None  # that report, when it could be read


def training(report: Line) -> Training | None:
    model = report.get("model")
    if report.get("kind") != "evaluation-report" or not isinstance(model, dict):
        return None
    return Training(
        name(report.get("pair")), _whole(report.get("horizon_ns")), name(model.get("kind")),
        _whole(model.get("fitted_rows")), _whole(report.get("trials")), report.get("not_a_result") != 0,
    )  # fmt: skip


def lineup_changes(history: Sequence[Line], window: Span, reports: Mapping[str, Line]) -> tuple[LineupChange, ...]:
    """The lines the lineup journal gained in the window, each with the report it names when that was read."""
    out = []
    for line in history:
        at, hashed, champion = line.get("at_ns"), line.get("report"), line.get("champion")
        if type(at) is not int or not window.start_ns <= at < window.end_ns:
            continue
        found = reports.get(hashed) if isinstance(hashed, str) else None
        out.append(
            LineupChange(
                at, name(line.get("action")), name(champion.get("model"))[:12] if isinstance(champion, dict) else None,
                len(_list(line.get("challengers"))), len(_list(line.get("candidates"))),
                name(line.get("decision")) if line.get("decision") is not None else None,
                tuple(name(reason) for reason in _list(line.get("reasons"))),
                name(hashed)[:12] if isinstance(hashed, str) else None, training(found) if found is not None else None,
            )
        )  # fmt: skip
    return tuple(out)


def weekly_cycles(history: Sequence[Line]) -> tuple[int, int]:
    """(weekends on which the journal holds both a training registration and a decision, drills that passed).

    A weekend is the stretch between two Friday closes. Whether a cycle ran without manual help is not in the
    journal, and is not claimed.
    """
    seen: dict[int, set[str]] = {}
    drills = 0
    for line in history:
        at, action = line.get("at_ns"), line.get("action")
        if type(at) is not int or not isinstance(action, str):
            continue
        drills += action == DRILL and line.get("reasons") == ["passed"]
        if action == REGISTER or action in _DECIDED:
            seen.setdefault(next_close(at), set()).add("trained" if action == REGISTER else "decided")
    return sum(1 for found in seen.values() if found == {"trained", "decided"}), drills


@dataclass(frozen=True, slots=True)
class Gates:
    """What the journals hold so far towards three exit gates. None: the journal that would say is not there."""

    days_recorded: int | None  # trading days with every pair recorded for some of the open time
    days_every_gap_explained: int | None  # of those, the days with no unexplained gap: towards F2's five
    days_traded: int | None  # trading days the stage journal closed
    days_clean: int | None  # of those, the days clean for every sleeve: towards F4's five
    weekly_cycles: int | None  # weekends with a training registration and a decision: towards F6's two
    drills_passed: int | None


def gates(
    store: Sequence[Line] | None, stages: Sequence[Line] | None, history: Sequence[Line] | None, calendar: Calendar,
    until_ns: int,
) -> Gates:  # fmt: skip
    recorded = explained = None
    if store:
        stamps = [at for at in (line.get("at_ns") for line in store) if type(at) is int]
        recorded = explained = 0
        for day in trading_days(calendar, min(stamps) - DAY_NS, until_ns) if stamps else []:
            found = recording(store, calendar, day)
            if found is not None and found.pairs and all(pair.recorded_ns > 0 for pair in found.pairs):
                recorded += 1
                explained += all(pair.unexplained == 0 for pair in found.pairs)
    traded = clean = None
    if stages is not None:
        closed = {day: verdicts for day, verdicts in clean_days(stages).items() if day <= until_ns}
        traded, clean = len(closed), sum(1 for verdicts in closed.values() if all(verdicts.values()))
    cycles = drills = None
    if history is not None:
        cycles, drills = weekly_cycles([line for line in history if _whole(line.get("at_ns")) <= until_ns])
    return Gates(recorded, explained, traded, clean, cycles, drills)


# ---- the two reports -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Daily:
    day: Span
    recording: Recording | None
    trading: Trading | None
    clean: Mapping[str, bool] | None  # the stage journal's verdict on the day, by sleeve; None: not closed there
    disks: tuple[Disk, ...]
    absent: Mapping[str, str]  # each part that is not in the report, and why


@dataclass(frozen=True, slots=True)
class SleeveWeek:
    name: str
    stage: str | None  # at the end of the week's last day
    shadow_fills: int
    broker_fills: int
    net_micro: Money | None  # summed over the days that said a result; None: no day did
    days_with_result: int
    less_control_micro: Money | None  # this sleeve's result less the control sleeve's, over the days both said one


@dataclass(frozen=True, slots=True)
class Weekly:
    week: Span
    days: tuple[str, ...]  # the week's trading days
    days_traded: tuple[str, ...]  # the ones a trader ran in
    recording: Recording | None
    sleeves: tuple[SleeveWeek, ...]
    control: str
    shadow_net_micro: Money | None
    broker_net_micro: Money | None
    days_with_broker_result: int
    breaks: int
    latches_tripped: tuple[str, ...]
    latches_reset: tuple[str, ...]
    stage_changes: tuple[tuple[str, str, str, str], ...]
    slippage: tuple[Slippage, ...]
    days_clean: int | None  # of the week's days, those the stage journal closed as clean for every sleeve
    lineup: tuple[LineupChange, ...] | None
    gates: Gates
    disks: tuple[Disk, ...]
    absent: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Read:
    """Everything `gather` found, as journal lines. A journal that is not there, or does not verify, is None."""

    store: Sequence[Line] | None = None
    days: Mapping[str, Sequence[Line]] = field(default_factory=dict)  # decision journals by day label
    stages: Sequence[Line] | None = None
    history: Sequence[Line] | None = None
    reports: Mapping[str, Line] = field(default_factory=dict)  # evaluation reports by hash
    disks: tuple[Disk, ...] = ()
    absent: Mapping[str, str] = field(default_factory=dict)  # why a journal could not be used


def _absences(read: Read, found: Mapping[str, object], broker: bool | None) -> dict[str, str]:
    absent = dict(read.absent)
    if found.get("recorder") is None:
        absent.setdefault("recorder", "no journal of the recording says anything about this time")
    if found.get("trader") is None:
        absent.setdefault("trader", "no session of the trader is journalled in this time")
    elif broker is False:
        absent["broker book"] = "the trader ran shadow-only: it held nothing that could send an order"
    return absent


def daily(read: Read, calendar: Calendar, day: Span) -> Daily:
    """The daily summary of one trading day."""
    entries = read.days.get(day.label)
    traded = trading(day.label, entries) if entries is not None else None
    recorded = recording(read.store, calendar, day) if read.store is not None else None
    verdicts = clean_days(read.stages).get(day.end_ns) if read.stages is not None else None
    absent = _absences(read, {"recorder": recorded, "trader": traded}, traded.has_broker_book if traded else None)
    if traded is not None and not traded.closed:
        absent["results"] = "the day's last session did not say it stopped: its fills are counted, its result is not"
    if traded is not None and verdicts is None:
        absent["clean day"] = "the stage journal has not closed this day"
    return Daily(day, recorded, traded, verdicts, read.disks, absent)


def _joined(parts: Iterable[Slippage]) -> tuple[Slippage, ...]:
    out: dict[tuple[str, str], Slippage] = {}
    for one in parts:
        was = out.get((one.sleeve, one.pair))
        if was is None:
            out[one.sleeve, one.pair] = one
            continue
        lows = [v for v in (was.lowest_increments, one.lowest_increments) if v is not None]
        highs = [v for v in (was.highest_increments, one.highest_increments) if v is not None]
        out[one.sleeve, one.pair] = Slippage(
            one.sleeve, one.pair, was.count + one.count, was.sum_increments + one.sum_increments,
            min(lows) if lows else None, max(highs) if highs else None, was.inexact + one.inexact,
            was.broker_only + one.broker_only, was.shadow_only + one.shadow_only, was.unequal + one.unequal,
        )  # fmt: skip
    return tuple(out[key] for key in sorted(out))


def weekly(read: Read, calendar: Calendar, week: Span, days: Sequence[Span], control: str = "control") -> Weekly:
    """The weekly scorecard of one trading week."""
    found = [(day, trading(day.label, read.days[day.label])) for day in days if day.label in read.days]
    traded = [(day, one) for day, one in found if one is not None]
    order: list[str] = []
    for _, one in traded:
        order += [sleeve.name for sleeve in one.sleeves if sleeve.name not in order]
    by_day = [{sleeve.name: sleeve for sleeve in one.sleeves} for _, one in traded]
    sleeves = []
    for sleeve in order:
        net: dict[str, int] = {}
        less: dict[str, int] = {}
        said = both = shadow_fills = broker_fills = 0
        stage = None
        for sleeves_of_day in by_day:
            mine, theirs = sleeves_of_day.get(sleeve), sleeves_of_day.get(control)
            if mine is None:
                continue
            stage = mine.stage
            shadow_fills, broker_fills = shadow_fills + mine.shadow_fills, broker_fills + mine.broker_fills
            if mine.net_micro is not None:
                said += 1
                _add(net, mine.net_micro)
                if theirs is not None and theirs.net_micro is not None:
                    both += 1
                    _add(less, mine.net_micro)
                    _add(less, {code: -amount for code, amount in theirs.net_micro.items()})
        sleeves.append(
            SleeveWeek(
                sleeve, stage, shadow_fills, broker_fills, net if said else None, said,
                less if both and sleeve != control else None,
            )
        )  # fmt: skip
    shadow: dict[str, int] = {}
    broker: dict[str, int] = {}
    with_shadow = with_broker = 0
    for _, one in traded:
        if one.shadow_net_micro is not None:
            with_shadow += 1
            _add(shadow, one.shadow_net_micro)
        if one.broker_net_micro is not None:
            with_broker += 1
            _add(broker, one.broker_net_micro)
    recorded = recording(read.store, calendar, week) if read.store is not None else None
    verdicts = clean_days(read.stages) if read.stages is not None else None
    clean = None
    if verdicts is not None:
        clean = sum(1 for day in days if day.end_ns in verdicts and all(verdicts[day.end_ns].values()))
    changes = lineup_changes(read.history, week, read.reports) if read.history is not None else None
    any_paper = any(one.has_broker_book for _, one in traded)
    absent = _absences(read, {"recorder": recorded, "trader": traded or None}, any_paper if traded else None)
    unsaid = [day.label for day, one in traded if not one.closed]
    if unsaid:
        absent["results"] = f"no result for {', '.join(unsaid)}: the day's last session did not say it stopped"
    if traded and control not in order:
        absent["control sleeve"] = f"no sleeve named {name(control)} ran this week: nothing is compared with it"
    if changes is None:
        absent.setdefault("lineup", "there is no lineup journal: training has registered nothing")
    elif any(change.report is not None and change.training is None for change in changes):
        absent["training report"] = "a report the lineup journal names is not in the registry, or is not a report"
    return Weekly(
        week, tuple(day.label for day in days), tuple(day.label for day, _ in traded), recorded, tuple(sleeves),
        name(control), shadow if with_shadow else None, broker if with_broker else None, with_broker,
        sum(one.breaks for _, one in traded), tuple(latch for _, one in traded for latch in one.latches_tripped),
        tuple(latch for _, one in traded for latch in one.latches_reset),
        tuple(change for _, one in traded for change in one.stage_changes),
        _joined(line for _, one in traded for line in one.slippage), clean, changes,
        gates(read.store, read.stages, read.history, calendar, week.end_ns), read.disks, absent,
    )  # fmt: skip


# ---- as text ---------------------------------------------------------------------------------------------------------


def _amount(nets: Money | None) -> str:
    """Millionths by currency as text, exactly: `-126.000000 USD`. Absent is said, and is not a zero."""
    if nets is None:
        return "ABSENT"
    shown = [f"{'-' if amount < 0 else ''}{abs(amount) // MILLION}.{abs(amount) % MILLION:06d} {code}"
             for code, amount in sorted(nets.items())]  # fmt: skip
    return ", ".join(shown) or "nothing in any currency"


def _seconds(ns: int) -> str:
    return f"{ns // SECOND:,} s"


def _recording_lines(found: Recording | None) -> list[str]:
    if found is None:
        return ["recorder   ABSENT"]
    lines = [f"recorder   started {found.starts} time(s) and stopped {found.stops} time(s) in this time"]
    for pair in found.pairs:
        share = pair.share_ppm
        if share is None:
            lines.append(f"  {pair.pair}: the market was shut throughout")
            continue
        lines.append(
            f"  {pair.pair}: recorded {_seconds(pair.recorded_ns)} of {_seconds(pair.open_ns)} open "
            f"({share:,} per million); {pair.unexplained} unexplained gap(s), {_seconds(pair.unexplained_ns)}"
        )
    return lines if found.pairs else [*lines, "  no pair is described in the journals"]


def _slippage_lines(lines: Sequence[Slippage]) -> list[str]:
    out = []
    for one in lines:
        mean = f"mean {one.sum_increments // one.count}" if one.count else "mean ABSENT"
        out.append(
            f"  slippage {one.sleeve} {one.pair}: {one.count} order(s) compared, {mean}, lowest "
            f"{'ABSENT' if one.lowest_increments is None else one.lowest_increments}, highest "
            f"{'ABSENT' if one.highest_increments is None else one.highest_increments} increment(s), shadow less "
            f"broker; {one.inexact} inexact; {one.broker_only} broker only, {one.shadow_only} shadow only, "
            f"{one.unequal} unequal"
        )
    return out


def _events(breaks: int, tripped: Sequence[str], reset: Sequence[str], changes: Sequence[tuple[str, ...]]) -> list[str]:
    return [
        f"  reconciliation breaks {breaks}; latches tripped: {', '.join(tripped) or 'none'}; "
        f"latches reset: {', '.join(reset) or 'none'}",
        "  stage changes: " + ("; ".join(f"{s} {b} to {t} ({c})" for s, b, t, c in changes) or "none"),
    ]


def _disk_lines(disks: Sequence[Disk]) -> list[str]:
    out = []
    for disk in disks:
        quota = "no quota set" if disk.quota_bytes is None else f"of {disk.quota_bytes:,} bytes ({disk.percent}%)"
        limits = ""
        if disk.standing is not None:
            limits = f"; soft limit {disk.soft_bytes:,}, hard limit {disk.hard_bytes:,}: {disk.standing}"
        out.append(f"disk       {disk.what}: {disk.used_bytes:,} bytes {quota}{limits}")
    return out


def _absent_lines(absent: Mapping[str, str]) -> list[str]:
    return [f"absent     {part}: {why}" for part, why in sorted(absent.items())]


def render_daily(report: Daily) -> str:
    day = report.day
    lines = [
        f"HFT Lab daily summary: trading day {day.label}, "
        f"{status.stamp(day.start_ns)} to {status.stamp(day.end_ns)}. Paper account only.",
        *_recording_lines(report.recording),
    ]
    traded = report.trading
    if traded is None:
        lines.append("trader     ABSENT")
    else:
        end = "NOT STOPPED" if not traded.closed else "saw the day close" if traded.saw_close else "stopped early"
        lines.append(f"trader     {traded.sessions} session(s); {traded.mode}; {end}")
        for sleeve in traded.sleeves:
            clean = "" if report.clean is None or sleeve.name not in report.clean else (
                "; clean day" if report.clean[sleeve.name] else "; not a clean day")  # fmt: skip
            lines.append(
                f"  {sleeve.name}: stage {sleeve.stage or 'ABSENT'}; {sleeve.shadow_fills} shadow fill(s), "
                f"{sleeve.broker_fills} broker fill(s); shadow result {_amount(sleeve.net_micro)}{clean}"
            )
        lines.append(
            f"  desk: shadow book {_amount(traded.shadow_net_micro)}; broker book {_amount(traded.broker_net_micro)}"
        )
        lines += _slippage_lines(traded.slippage)
        lines += _events(traded.breaks, traded.latches_tripped, traded.latches_reset, traded.stage_changes)
    return "\n".join([*lines, *_disk_lines(report.disks), *_absent_lines(report.absent)])


def _towards(count: int | None, of: int | None, what: str, goal: int) -> str:
    return "ABSENT" if count is None or of is None else f"{count} of {of} {what} day(s), towards {goal}"


def render_weekly(report: Weekly) -> str:
    week = report.week
    lines = [
        f"HFT Lab weekly scorecard: week {week.label}, {status.stamp(week.start_ns)} to "
        f"{status.stamp(week.end_ns)}. Paper account only.",
        f"days       {len(report.days)} trading day(s); a trader ran in {len(report.days_traded)}: "
        f"{', '.join(report.days_traded) or 'none'}",
        *_recording_lines(report.recording),
    ]
    if not report.days_traded:
        lines.append("trader     ABSENT")
    else:
        lines.append(f"trader     each sleeve's result is the shadow book's; {report.control} is the control sleeve")
        for sleeve in report.sleeves:
            said = f" over {sleeve.days_with_result} of {len(report.days_traded)} day(s)"
            against = f"; less {report.control} {_amount(sleeve.less_control_micro)}"
            less = "" if sleeve.name == report.control else against
            lines.append(
                f"  {sleeve.name}: stage {sleeve.stage or 'ABSENT'}; {sleeve.shadow_fills} shadow fill(s), "
                f"{sleeve.broker_fills} broker fill(s); shadow result {_amount(sleeve.net_micro)}{said}{less}"
            )
        lines.append(
            f"  desk: shadow book {_amount(report.shadow_net_micro)}; broker book {_amount(report.broker_net_micro)}"
            f" over {report.days_with_broker_result} day(s)"
        )
        lines += _slippage_lines(report.slippage)
        lines += _events(report.breaks, report.latches_tripped, report.latches_reset, report.stage_changes)
    if report.lineup is None:
        lines.append("lineup     ABSENT")
    elif not report.lineup:
        lines.append("lineup     no change since the Friday before: no training registered, no decision journalled")
    for change in report.lineup or ():
        rests = ""
        if change.training is not None:
            found = change.training
            rests = (
                f"; report {change.report}: {found.pair}, {found.horizon_ns // SECOND} s, {found.kind}, "
                f"{found.fitted_rows:,} row(s), {found.trials} trial(s)"
                + ("; NOT A RESULT: placeholders" if found.not_a_result else "")
            )
        elif change.report is not None:
            rests = f"; report {change.report}: ABSENT"
        why = f" ({', '.join(change.reasons)})" if change.reasons else ""
        lines.append(
            f"lineup     {status.stamp(change.at_ns)} {change.action}{why}; champion {change.champion or 'none'}; "
            f"{change.challengers} challenger(s), {change.candidates} candidate(s); decision record "
            f"{change.decision or 'none'}{rests}"
        )
    found_gates = report.gates
    cycles = "ABSENT" if found_gates.weekly_cycles is None else (
        f"{found_gates.weekly_cycles} weekend(s) with a training and a decision, towards {F6_CYCLES}; "
        f"{found_gates.drills_passed} rollback drill(s) passed"
    )  # fmt: skip
    lines += [
        "gates      evidence so far, counted from the journals. A count is not a gate: see docs/roadmap.md",
        "  F2 days with every gap explained: "
        + _towards(found_gates.days_every_gap_explained, found_gates.days_recorded, "recorded", F2_DAYS),
        "  F4 clean trading days: " + _towards(found_gates.days_clean, found_gates.days_traded, "closed", F4_DAYS),
        f"  F6 weekly cycles: {cycles}",
    ]
    if report.days_clean is not None:
        lines.append(f"  this week: {report.days_clean} of {len(report.days)} trading day(s) clean for every sleeve")
    return "\n".join([*lines, *_disk_lines(report.disks), *_absent_lines(report.absent)])


def as_dict(report: Daily | Weekly) -> dict[str, object]:
    """The report as plain values: integers, text, lists and mappings. No float is in it."""
    return asdict(report)


# ---- reading ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Places:
    """Where the journals are, as the settings files that own them say."""

    record_root: Path
    state: Path
    calendar: Calendar
    trader: Path | None  # the trader's folder
    lineup: Path | None
    reports: Path | None  # the evaluation reports, each file named by its hash
    recording_quota: int | None
    tape_quota: int | None
    control: str


def _verified(path: Path, part: str, absent: dict[str, str]) -> list[dict[str, object]] | None:
    """A hash-chained journal's lines; None, with the reason noted, when it is not there or does not verify."""
    if not path.is_file():
        return None
    try:
        return read_journal(path)[1]
    except (JournalError, OSError):
        absent[part] = "its journal does not verify. It is never repaired, and nothing is reported from it"
        return None


def _tape_limits(days: Mapping[str, Sequence[Line]], quota: int | None) -> tuple[int | None, int | None]:
    """The soft and the hard limit on the tapes, as the last session that began in these days journalled them.

    They are the trader's own figures (the hard limit depends on the risk limits, which this layer does not
    read). None, both, when no session journalled any, or the quota they were worked out for is another one.
    """
    for label in sorted(days, reverse=True):
        for entry in reversed(days[label]):
            said = entry.get("tape") if entry.get("type") == "start" else None
            if isinstance(said, dict):
                soft, hard = said.get("soft_bytes"), said.get("hard_bytes")
                if type(soft) is int and type(hard) is int and said.get("quota_bytes") == quota:
                    return soft, hard
                return None, None
    return None, None


def gather(places: Places, labels: Sequence[str]) -> Read:
    """Read each journal once, and of the trader's days the ones named. Nothing is written.

    A journal that cannot be used leaves its part absent, with the reason.
    """
    absent: dict[str, str] = {}
    store: list[dict[str, object]] | None = None
    try:
        folders = store_days(places.record_root)
        store = [line for folder in folders for line in read_store_journal(folder)] if folders else None
    except (OSError, ValueError):
        absent["recorder"] = "a journal of the recording cannot be read"
    days: dict[str, list[dict[str, object]]] = {}
    stages = history = None
    disks = []
    if places.record_root.is_dir():
        disks.append(Disk("the recording", measure(places.record_root), places.recording_quota))
    if places.trader is not None and places.trader.is_dir():
        folder = places.trader / "days"
        for label in sorted(set(labels)):
            found = _verified(folder / label / "decisions.jsonl", "trader", absent)
            if found is not None:
                days[label] = found
        stages = _verified(places.trader / "stages.jsonl", "clean day", absent)
        tapes = sum(path.stat().st_size for path in folder.glob("*/session-*") if path.is_file())
        disks.append(Disk("the tapes", tapes, places.tape_quota, *_tape_limits(days, places.tape_quota)))
    if places.lineup is not None and places.lineup.is_file():
        try:
            history = lineup_history(places.lineup)
        except (LineupError, OSError):
            absent["lineup"] = "its journal does not verify. It is never repaired, and nothing is reported from it"
    reports: dict[str, dict[str, object]] = {}
    for line in history or []:
        hashed = line.get("report")
        if isinstance(hashed, str) and _NAME.fullmatch(hashed) and places.reports is not None:
            try:
                loaded = json.loads((places.reports / f"{hashed}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(loaded, dict):
                reports[hashed] = loaded
    return Read(store, days, stages, history, reports, tuple(disks), absent)


def load_places(
    config: Path = status.CONFIG,
    recorder_config: Path = status.RECORDER_CONFIG,
    shadow_config: Path = status.SHADOW_CONFIG,
    calendar: Path = status.CALENDAR,
    trader_config: Path = status.TRADER_CONFIG,
    learn_config: Path = LEARN_CONFIG,
) -> Places:
    sources = status.load_sources(config, recorder_config, shadow_config, calendar, trader_config=trader_config)
    trader: dict[str, Any] = status._mapping(trader_config) if trader_config.is_file() else {}
    learn: dict[str, Any] = status._mapping(learn_config) if learn_config.is_file() else {}
    tape, promotion = trader.get("tape"), learn.get("promotion")
    registry = trader.get("registry") or learn.get("state_dir")
    control = promotion.get("baseline_sleeve") if isinstance(promotion, dict) else None
    quota = status._mapping(recorder_config).get("quota_bytes")
    tape_quota = tape.get("quota_bytes") if isinstance(tape, dict) else None
    return Places(
        sources.record_root, sources.state, sources.calendar,
        sources.trader_status.parent if sources.trader_status is not None else None, sources.lineup,
        Path(registry) / "reports" if isinstance(registry, str) else None,
        quota if type(quota) is int else None, tape_quota if type(tape_quota) is int else None,
        control if isinstance(control, str) else "control",
    )  # fmt: skip


# ---- the command -----------------------------------------------------------------------------------------------------


def keep(state: Path, kind: str, label: str, text: str) -> Path:
    """Write a report to its file, whole: `reports/daily-2026-10-14.txt`. The same day again replaces it."""
    folder = state / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}-{label}.txt"
    partial = path.with_suffix(".partial")
    partial.write_text(text + "\n", encoding="utf-8")
    os.replace(partial, path)
    return path


def page_of(kind: str, label: str, text: str) -> Page:
    """The report as a page: every line masked as a page's is, and cut where a notification would be."""
    body = "\n".join(scrub(line) for line in text.splitlines())
    if len(body) > PAGE_MAX:
        body = body[:PAGE_MAX].rsplit("\n", 1)[0] + "\n(cut here: the whole report is in its file on the host)"
    title = "Daily summary" if kind == "daily" else "Weekly scorecard"
    return Page(f"{host.ALERT_PREFIX}report.{kind}", f"{title} {label}", body, 2)


def produce(kinds: Sequence[str], places: Places, now_ns: int) -> list[tuple[str, str, str]]:
    """(kind, label, text) for each report asked for, as the journals stand at `now_ns`."""
    out = []
    day = last_trading_day(places.calendar, now_ns)
    week, days = last_trading_week(places.calendar, now_ns)
    read = gather(places, [day.label, *(one.label for one in days)])
    for kind in kinds:
        if kind == "daily":
            out.append((kind, day.label, render_daily(daily(read, places.calendar, day))))
        elif kind == "weekly" or (kind == "due-weekly" and day.end_ns == week.end_ns):
            text = render_weekly(weekly(read, places.calendar, week, days, places.control))
            out.append(("weekly", week.label, text))
    return out


def main(argv: Sequence[str] | None = None, clock: Clock | None = None, sender: Sender | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.report", description="The daily and weekly reports.")
    parser.add_argument("what", choices=["daily", "weekly", "due"])
    parser.add_argument("--send", action="store_true", help="also send each report as a page")
    parser.add_argument("--config", type=Path, default=status.CONFIG)
    parser.add_argument("--recorder-config", type=Path, default=status.RECORDER_CONFIG)
    parser.add_argument("--shadow-config", type=Path, default=status.SHADOW_CONFIG)
    parser.add_argument("--calendar", type=Path, default=status.CALENDAR)
    parser.add_argument("--trader-config", type=Path, default=status.TRADER_CONFIG)
    parser.add_argument("--learn-config", type=Path, default=LEARN_CONFIG)
    args = parser.parse_args(argv)
    try:
        places = load_places(
            args.config, args.recorder_config, args.shadow_config, args.calendar, args.trader_config, args.learn_config
        )
        kinds = ["daily", "due-weekly"] if args.what == "due" else [args.what]
        made = produce(kinds, places, (clock or WallClock()).now_ns())
    except (status.StatusError, CalendarError) as exc:
        print(f"report: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    code = EXIT_OK
    for kind, label, text in made:
        path = keep(places.state, kind, label, text)
        print(text)
        print(f"report: written to {path}", file=sys.stderr)
        if args.send:
            deliver = sender or sender_from(host.settings(), lambda line: print(line, file=sys.stderr, flush=True))
            code = code if deliver(page_of(kind, label, text)) else EXIT_NOT_SENT
    return code


if __name__ == "__main__":
    sys.exit(main())
