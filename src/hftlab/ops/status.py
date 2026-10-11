"""`python -m hftlab.ops.status`: one screen on the recorder, its store, the shadow run, the trader and the switches.

    python -m hftlab.ops.status             the screen
    python -m hftlab.ops.status --json      the same reading as JSON
    python -m hftlab.ops.status --alert     also page the owner when a problem starts and when it is over

It reads four things the platform already writes and judges them: the recorder's status file, which the
recorder replaces every few seconds and which is its heartbeat; the store's journals; the shadow journal; and
the trader's status file, once a trader has run. It reads nothing else and writes nothing but the alert state.
The status snapshot (hftlab.publish) is built from this same reading, so the screen and the published page
cannot disagree.

The dead-man rule: a recorder that last said "recording", whose status is older than the configured age while
the market is open, is dead, whatever the service manager believes. That is a failure with its own exit code.
A recorder that has never been started is not a failure: there is nothing to be dead.

Exit codes: 0 nothing is wrong (warnings are shown and do not fail), 1 something is wrong, 2 a setting or a
file could not be read, 3 the dead-man rule.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.calendar import Calendar, CalendarError, load_calendar, new_york, next_close, next_open
from hftlab.calendar.week import OPEN_TIME, instant
from hftlab.core import Clock, WallClock
from hftlab.lineup.journal import DRILL, PROMOTE, REGISTER, ROLL_BACK, LineupError
from hftlab.lineup.journal import FILE as LINEUP_FILE
from hftlab.lineup.journal import history as lineup_history
from hftlab.ops import host
from hftlab.ops.alerts import Alerts, sender_from
from hftlab.record.format import CorruptSegment
from hftlab.record.gaps import UNEXPLAINED
from hftlab.record.segment import count_records
from hftlab.record.store import day_of, days, measure, read_journal, segments
from hftlab.shadow.journal import JournalError
from hftlab.shadow.journal import read as read_shadow

SECOND: Final = 1_000_000_000
CONFIG: Final = Path("config/ops.yaml")
RECORDER_CONFIG: Final = Path("config/recorder.yaml")
SHADOW_CONFIG: Final = Path("config/shadow.yaml")
TRADER_CONFIG: Final = Path("config/trader.yaml")
CALENDAR: Final = Path("config/calendar")
STATUS_FILE: Final = "status.json"
SHADOW_JOURNAL: Final = "live.jsonl"  # the journal of the shadow runner on quotes from the gateway
BACKUP_WORD: Final = "backup/last.json"  # in the state directory: what became of the last backup run
VENUE: Final = "ibkr_paper"  # the one venue this desk records and trades on
CONNECTED: Final = ("RECONCILING", "READY")
BAD, WARN = "bad", "warn"
EXIT_OK, EXIT_BAD, EXIT_UNREADABLE, EXIT_DEAD = 0, 1, 2, 3
COUNTED: Final = {"decision": "decisions", "order": "orders", "refused": "refused", "fill": "fills"}
# The problems this command pages for and takes back. Another command's alert is not this one's to resolve.
PAGED: Final = (
    "recorder_status_unreadable", "recorder_not_on_paper_venue", "recorder_io_error", "recorder_quota",
    "recorder_stale", "recorder_stopped", "connection_down", "shadow_journal_broken", "backup_failed",
    "lineup_journal_broken",
)  # fmt: skip


class StatusError(ValueError):
    """A setting this command needs could not be read."""


@dataclass(frozen=True, slots=True)
class Sources:
    """Where to look, and how old is too old."""

    record_root: Path
    shadow_journal: Path
    state: Path
    calendar: Calendar
    dead_man_ns: int
    disk_warn_percent: int
    trader_status: Path | None = None  # the trader's status file; None where no trader is set up
    lineup: Path | None = None  # the lineup journal, in the trader's state directory (ADR 0023)


@dataclass(frozen=True, slots=True)
class Window:
    """One trading week: Sunday 17:15 to Friday 17:00 New York, as UTC nanoseconds."""

    session: str  # the ISO week it trades in, "2026-W42"
    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class Problem:
    code: str  # short, lower case, no spaces: it is also the alert key and the snapshot's reason
    level: str  # "warn" or "bad"
    text: str  # one sentence for the screen and the page. It never goes into the snapshot
    dead_man: bool = False


@dataclass(frozen=True, slots=True)
class RecorderReading:
    state: str  # "running" or "stopped"
    said: str  # what its status file says: recording, stopped, quota, io-error
    beat_ns: int  # when it last wrote its status
    age_ns: int
    venue: str
    connection: str
    run: int
    records_this_run: int
    records_today: int
    last_record_ns: int | None
    gaps_unexplained_today: int
    silences_open: int
    used_bytes: int
    quota_bytes: int | None
    longest_between_polls_ns: int | None
    recent_between_polls_ns: int | None
    polls: int | None


@dataclass(frozen=True, slots=True)
class ShadowReading:
    sessions: int
    entries: int
    last_ns: int | None
    sleeves: Mapping[str, Mapping[str, int]]  # for the last session: decisions, orders, refused, fills


@dataclass(frozen=True, slots=True)
class BackupReading:
    at_ns: int
    ok: bool
    files: int


@dataclass(frozen=True, slots=True)
class SleeveReading:
    name: str
    stage: str
    trades_today: int
    trades_total: int
    net_today_micro: Mapping[str, int] | None  # by currency, from the shadow book; None: not worked out
    net_total_micro: Mapping[str, int] | None


@dataclass(frozen=True, slots=True)
class BookReading:
    net_today_micro: Mapping[str, int] | None
    net_total_micro: Mapping[str, int] | None


@dataclass(frozen=True, slots=True)
class TraderReading:
    """What the trader last said of itself (hftlab.trader writes it on every beat)."""

    state: str  # "running" or "stopped", by the age of what it said
    mode: str  # "shadow_only" or "paper"
    at_ns: int
    age_ns: int
    day: str
    connection: str
    sleeves: tuple[SleeveReading, ...]
    shadow: BookReading
    broker: BookReading | None  # None in a run that sends nothing to a broker
    breaks_today: int
    open_positions: int
    latched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LearningReading:
    """What the lineup journal says: who serves, and when it last changed. A hash is shortened to an id."""

    champion: str | None  # the first twelve characters of the champion's hash; None: no model has been promoted
    challengers: int
    candidates: int
    last_training_ns: int | None  # when training last registered a candidate
    last_promotion_ns: int | None
    last_rollback_ns: int | None
    last_drill_ns: int | None
    drill_passed: bool | None  # None: no rollback drill has run


@dataclass(frozen=True, slots=True)
class Reading:
    """Everything the screen and the snapshot say, read once."""

    at_ns: int
    market_open: bool
    weeks: tuple[Window, ...]
    kill: bool
    flatten: bool
    recorder: RecorderReading | None  # None: nothing has ever been recorded here
    shadow: ShadowReading | None  # None: the shadow runner has never run on live quotes here
    backup: BackupReading | None  # None: the backup has never run here
    problems: tuple[Problem, ...]
    trader: TraderReading | None = None  # None: no trader has run here
    learning: LearningReading | None = None  # None: training has registered nothing here

    @property
    def level(self) -> str:
        levels = {problem.level for problem in self.problems}
        return BAD if BAD in levels else WARN if WARN in levels else "ok"


# ---- the calendar ----------------------------------------------------------------------------------------------


def stamp(ts_ns: int) -> str:
    """A UTC time as the snapshot contract writes one: 2026-10-14T03:00:05Z."""
    return datetime.fromtimestamp(ts_ns // SECOND, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def trading_weeks(now_ns: int, count: int = 2) -> tuple[Window, ...]:
    """The trading week that is open at `now_ns`, or the next one to open, and the ones that follow it.

    New York moves its clock on a Sunday at 02:00, before the week opens, so a week never contains the change
    and its opening is always the Sunday five days before its Friday close.
    """
    out = []
    close = next_close(next_open(now_ns))
    for _ in range(count):
        friday = new_york(close).date()
        year, week, _ = friday.isocalendar()
        out.append(Window(f"{year}-W{week:02d}", instant(friday - timedelta(days=5), OPEN_TIME), close))
        close = next_close(close)
    return tuple(out)


# ---- reading ---------------------------------------------------------------------------------------------------


def _whole(value: object) -> int | None:
    return value if type(value) is int else None


def _recorder(sources: Sources, now_ns: int, market_open: bool) -> tuple[RecorderReading | None, list[Problem]]:
    root, path = sources.record_root, sources.record_root / STATUS_FILE
    if not path.is_file() and not days(root):
        return None, []
    problems: list[Problem] = []
    said: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        said = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        said = {}
    if not said:
        text = f"{path} is missing or cannot be read, although there are recordings beside it."
        problems.append(Problem("recorder_status_unreadable", BAD, text))
    status = str(said.get("status", "unknown"))
    beat = _whole(said.get("at_ns")) or 0
    age = max(0, now_ns - beat)
    alive = status == "recording" and age <= sources.dead_man_ns
    today = root / day_of(now_ns)
    records_today, journalled, open_now = 0, 0, 0
    silent = said.get("silent") if alive else None  # a dead recorder's list of silences is old news
    if today.is_dir():
        for segment in segments(today):
            try:
                records_today += count_records(segment)
            except (OSError, CorruptSegment):  # being compressed this moment, or damaged: the day check says which
                continue
        try:
            gaps = [line for line in read_journal(today) if line.get("type") == "gap"]
        except (OSError, ValueError):
            gaps = []
            problems.append(Problem("journal_unreadable", WARN, f"The journal of {today.name} cannot be read."))
        unexplained = {(g.get("pair"), g.get("start_ns")) for g in gaps if g.get("reason") == UNEXPLAINED}
        journalled = len(unexplained)
        for gap in silent if isinstance(silent, list) else []:
            if isinstance(gap, dict) and gap.get("reason") == UNEXPLAINED:
                open_now += (gap.get("pair"), gap.get("start_ns")) not in unexplained
    measured = said.get("loop")
    loop: dict[str, Any] = measured if isinstance(measured, dict) else {}
    reading = RecorderReading(
        state="running" if alive else "stopped", said=status, beat_ns=beat, age_ns=age,
        venue=str(said.get("venue", "")), connection=str(said.get("connection", "")), run=_whole(said.get("run")) or 0,
        records_this_run=_whole(said.get("records_this_run")) or 0, records_today=records_today,
        last_record_ns=_whole(said.get("last_record_ns")) or None, gaps_unexplained_today=journalled + open_now,
        silences_open=len(silent) if isinstance(silent, list) else 0, used_bytes=measure(root),
        quota_bytes=_whole(said.get("quota_bytes")),
        longest_between_polls_ns=_whole(loop.get("longest_between_polls_ns")),
        recent_between_polls_ns=_whole(loop.get("recent_between_polls_ns")), polls=_whole(loop.get("polls")),
    )  # fmt: skip
    return reading, (problems + _judge(reading, sources, market_open) if said else problems)


def _judge(recorder: RecorderReading, sources: Sources, market_open: bool) -> list[Problem]:
    """What is wrong with the recorder, from its reading alone."""
    out = []
    seconds = recorder.age_ns // SECOND
    if recorder.venue != VENUE:
        text = f"The store was last written from the venue {recorder.venue!r}, not {VENUE}: a dry run, not the market."
        out.append(Problem("recorder_not_on_paper_venue", BAD, text))
    if recorder.said == "io-error":
        text = "The recorder stopped because the operating system refused a write (no space left, or an I/O error)."
        out.append(Problem("recorder_io_error", BAD, text))
    elif recorder.said == "quota":
        text = "The recorder stopped at its quota. Nothing is recorded until room is made."
        out.append(Problem("recorder_quota", BAD, text))
    elif recorder.said == "recording" and recorder.state == "stopped":
        text = f'The recorder last said "recording" {seconds} s ago and has been silent since'
        if market_open:
            out.append(Problem("recorder_stale", BAD, f"{text}, while the market is open.", dead_man=True))
        else:
            out.append(Problem("recorder_not_running", WARN, f"{text}. The market is closed."))
    elif recorder.state == "stopped" and market_open:
        text = f"The recorder is stopped ({recorder.said}, {seconds} s ago) while the market is open."
        out.append(Problem("recorder_stopped", BAD, text))
    if recorder.state == "running" and market_open and recorder.connection not in CONNECTED:
        text = f"The recorder has no connection to the gateway ({recorder.connection}) while the market is open."
        out.append(Problem("connection_down", BAD, text))
    if recorder.gaps_unexplained_today:
        text = f"{recorder.gaps_unexplained_today} silence(s) in today's recording have no explanation."
        out.append(Problem("gaps_unexplained", WARN, text))
    quota = recorder.quota_bytes
    if quota and recorder.said != "quota" and recorder.used_bytes * 100 >= quota * sources.disk_warn_percent:
        text = f"The recording has used {recorder.used_bytes * 100 // quota}% of its quota."
        out.append(Problem("disk_near_quota", WARN, text))
    return out


def _shadow(path: Path) -> tuple[ShadowReading | None, list[Problem]]:
    if not path.is_file():
        return None, []
    try:
        state, entries = read_shadow(path)
    except (JournalError, OSError) as exc:
        text = f"The shadow journal does not verify ({exc}). It is never repaired: see the shadow-run runbook."
        return None, [Problem("shadow_journal_broken", BAD, text)]
    counts: dict[str, dict[str, int]] = {}
    last = None
    for entry in entries:
        kind, at, sleeve = entry.get("type"), entry.get("at_ns"), entry.get("sleeve")
        last = max(last or 0, at) if type(at) is int else last
        if kind == "start":
            names = entry.get("sleeves")
            listed = names if isinstance(names, list) else []
            counts = {str(name): dict.fromkeys(COUNTED.values(), 0) for name in listed}
        elif isinstance(kind, str) and kind in COUNTED and isinstance(sleeve, str) and sleeve in counts:
            counts[sleeve][COUNTED[kind]] += 1
    return ShadowReading(state.sessions, state.entries, last, counts), []


def _backup(state: Path) -> tuple[BackupReading | None, list[Problem]]:
    """The word the backup job left. It has no network of its own, so a failed run is paged from here."""
    try:
        said = json.loads((state / BACKUP_WORD).read_text(encoding="utf-8"))
        reading = BackupReading(int(said["at_ns"]), said["ok"] is True, int(said["files"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None, []
    if reading.ok:
        return reading, []
    text = f"The hand-over of the journals at {stamp(reading.at_ns)} did not complete. Nothing was deleted."
    return reading, [Problem("backup_failed", BAD, text)]


def _nets(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict) or not all(isinstance(k, str) and type(v) is int for k, v in value.items()):
        return None
    return dict(value)


def _book(value: object) -> BookReading | None:
    if not isinstance(value, dict):
        return None
    return BookReading(_nets(value.get("net_today_micro")), _nets(value.get("net_total_micro")))


def _trader(sources: Sources, now_ns: int) -> tuple[TraderReading | None, list[Problem]]:
    """The trader's own word, read and not judged beyond its age: it pages its latches and breaks itself."""
    path = sources.trader_status
    if path is None or not path.is_file():
        return None, []
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
        books = said["books"]
        shadow = _book(books["shadow"])
        sleeves = tuple(
            SleeveReading(
                str(one["name"]), str(one["stage"]), int(one["trades_today"]), int(one["trades_total"]),
                _nets(one.get("net_today_micro")), _nets(one.get("net_total_micro")),
            )
            for one in said["sleeves"]
        )  # fmt: skip
        at = int(said["at_ns"])
        if shadow is None:
            raise ValueError("no shadow book")
        reading = TraderReading(
            state="running" if now_ns - at <= sources.dead_man_ns else "stopped", mode=str(said["mode"]), at_ns=at,
            age_ns=max(0, now_ns - at), day=str(said["day"]), connection=str(said["connection"]), sleeves=sleeves,
            shadow=shadow, broker=_book(books.get("broker")), breaks_today=int(books["reconciliation_breaks_today"]),
            open_positions=int(books["open_positions"]), latched=tuple(map(str, said.get("latched", []))),
        )  # fmt: skip
    except (OSError, ValueError, KeyError, TypeError):
        text = f"{path} is there and cannot be read as the trader's status."
        return None, [Problem("trader_status_unreadable", WARN, text)]
    problems = []
    if reading.latched:
        text = f"The trader has {len(reading.latched)} latch(es) tripped: entries are stopped until an operator resets."
        problems.append(Problem("trader_latched", WARN, text))
    if reading.breaks_today:
        text = f"The trader flagged {reading.breaks_today} reconciliation break(s) today."
        problems.append(Problem("trader_reconciliation_break", WARN, text))
    return reading, problems


def _learning(path: Path | None) -> tuple[LearningReading | None, list[Problem]]:
    """The lineup in force and the times of its changes, from a journal that verifies. Nothing registered: None."""
    if path is None or not path.is_file():
        return None, []
    try:
        lines = lineup_history(path)
    except (LineupError, OSError) as exc:
        text = f"The lineup journal does not verify ({exc}). No model is served from it, and it is never repaired."
        return None, [Problem("lineup_journal_broken", BAD, text)]
    if not lines:
        return None, []
    last: dict[str, int] = {}
    passed = None
    for line in lines:
        action, at = line.get("action"), line.get("at_ns")
        if isinstance(action, str) and type(at) is int:
            last[action] = at
            if action == DRILL:
                passed = line.get("reasons") == ["passed"]
    now = lines[-1]
    champion, challengers, candidates = now.get("champion"), now.get("challengers"), now.get("candidates")
    named = str(champion.get("model"))[:12] if isinstance(champion, dict) else None
    reading = LearningReading(
        named, len(challengers) if isinstance(challengers, list) else 0,
        len(candidates) if isinstance(candidates, list) else 0, last.get(REGISTER), last.get(PROMOTE),
        last.get(ROLL_BACK), last.get(DRILL), passed,
    )  # fmt: skip
    problems = []
    if passed is False:
        text = f"The rollback drill of {stamp(last[DRILL])} failed: see the learning runbook before the next decision."
        problems.append(Problem("rollback_drill_failed", WARN, text))
    return reading, problems


def read(sources: Sources, now_ns: int) -> Reading:
    """Read everything once and judge it. The same files and the same time give the same reading."""
    market_open = sources.calendar.is_open(now_ns)
    recorder, problems = _recorder(sources, now_ns, market_open)
    shadow, more = _shadow(sources.shadow_journal)
    backup, last = _backup(sources.state)
    trader, said = _trader(sources, now_ns)
    learning, learned = _learning(sources.lineup)
    kill, flatten = host.switches(sources.state)
    found = tuple(problems + more + last + said + learned)
    weeks = trading_weeks(now_ns)
    return Reading(now_ns, market_open, weeks, kill, flatten, recorder, shadow, backup, found, trader, learning)


def exit_code(reading: Reading) -> int:
    if any(problem.dead_man for problem in reading.problems):
        return EXIT_DEAD
    return EXIT_BAD if reading.level == BAD else EXIT_OK


# ---- showing ---------------------------------------------------------------------------------------------------


def _ms(ns: int | None) -> str:
    return "not measured" if ns is None else f"{ns // 1_000_000}.{ns % 1_000_000 // 100_000} ms"


def _mb(size: int) -> str:
    return f"{size // 1_000_000:,}.{size % 1_000_000 // 100_000} MB"


def _money(nets: Mapping[str, int] | None) -> str:
    """Millionths by currency as text, exactly: `-126.000000 USD`. Amounts in different currencies are not added."""
    if nets is None:
        return "not worked out"
    shown = [f"{'-' if amount < 0 else ''}{abs(amount) // 1_000_000}.{abs(amount) % 1_000_000:06d} {code}"
             for code, amount in sorted(nets.items()) if amount or len(nets) == 1]  # fmt: skip
    return ", ".join(shown) or "0"


def render(reading: Reading) -> str:
    """The one screen."""
    week = reading.weeks[0]
    opens = "open" if reading.market_open else "closed"
    lines = [
        f"HFT Lab status at {stamp(reading.at_ns)}",
        f"  market     {opens}; week {week.session} runs {stamp(week.start_ns)} to {stamp(week.end_ns)}",
    ]
    recorder = reading.recorder
    if recorder is None:
        lines.append("  recorder   never started: nothing has been recorded here")
    else:
        used = _mb(recorder.used_bytes)
        if recorder.quota_bytes:
            used += f" of {_mb(recorder.quota_bytes)} ({recorder.used_bytes * 100 // recorder.quota_bytes}%)"
        last = stamp(recorder.last_record_ns) if recorder.last_record_ns else "none yet"
        lines += [
            f"  recorder   {recorder.state}; last beat {recorder.age_ns // SECOND} s ago, saying "
            f'"{recorder.said}"; run {recorder.run}; venue {recorder.venue or "unknown"}; '
            f"connection {recorder.connection or 'unknown'}",
            f"  records    {recorder.records_today:,} today; {recorder.records_this_run:,} this run; last at {last}",
            f"  gaps       {recorder.gaps_unexplained_today} unexplained today; "
            f"{recorder.silences_open} silence(s) open now",
            f"  disk       {used}",
            f"  loop       longest between two polls {_ms(recorder.longest_between_polls_ns)}; "
            f"recent {_ms(recorder.recent_between_polls_ns)}",
        ]
    shadow = reading.shadow
    if shadow is None:
        lines.append("  shadow     no journal of a shadow run on gateway quotes")
    else:
        last = stamp(shadow.last_ns) if shadow.last_ns else "none yet"
        lines.append(f"  shadow     {shadow.sessions} session(s); {shadow.entries:,} journal entries; last at {last}")
        for name, counts in shadow.sleeves.items():
            listed = ", ".join(f"{count} {what}" for what, count in counts.items())
            lines.append(f"             sleeve {name}, this session: {listed}")
    trader = reading.trader
    if trader is None:
        lines.append("  trader     never run")
    else:
        lines.append(
            f"  trader     {trader.state}; {trader.mode.replace('_', '-')}; last beat {trader.age_ns // SECOND} s ago; "
            f"connection {trader.connection}; {trader.open_positions} open position(s); "
            f"{trader.breaks_today} reconciliation break(s) today"
        )
        for sleeve in trader.sleeves:
            lines.append(
                f"             sleeve {sleeve.name}: {sleeve.stage}; {sleeve.trades_today} trade(s) today, "
                f"{sleeve.trades_total} in all; shadow net {_money(sleeve.net_total_micro)}"
            )
        if trader.latched:
            lines.append(f"             latched: {', '.join(trader.latched)}")
    learning = reading.learning
    if learning is None:
        lines.append("  learning   nothing registered: no training run has kept a candidate here")
    else:

        def when(at: int | None) -> str:
            return stamp(at) if at is not None else "never"

        drill = "never run" if learning.drill_passed is None else "passed" if learning.drill_passed else "FAILED"
        lines += [
            f"  learning   champion {learning.champion or 'none'}; {learning.challengers} challenger(s) in shadow; "
            f"{learning.candidates} candidate(s) waiting",
            f"             last training {when(learning.last_training_ns)}; promotion "
            f"{when(learning.last_promotion_ns)}; rollback {when(learning.last_rollback_ns)}; drill {drill}",
        ]
    if reading.backup is None:
        lines.append("  backup     never run")
    else:
        how = f"{reading.backup.files} file(s) handed over" if reading.backup.ok else "FAILED"
        lines.append(f"  backup     last run {stamp(reading.backup.at_ns)}: {how}")
    lines.append(f"  switches   KILL {'SET' if reading.kill else 'off'}; FLATTEN {'SET' if reading.flatten else 'off'}")
    lines.append(f"  health     {reading.level.upper()}")
    for problem in reading.problems:
        mark = "DEAD-MAN" if problem.dead_man else problem.level
        lines.append(f"     [{mark}] {problem.code}: {problem.text}")
    return "\n".join(lines)


def as_dict(reading: Reading) -> dict[str, object]:
    return {**asdict(reading), "level": reading.level, "exit_code": exit_code(reading)}


def page(reading: Reading, alerts: Alerts) -> None:
    """Page each bad problem once, and say when one the owner was told about is over. Warnings do not page."""
    wanted = {problem.code: problem for problem in reading.problems if problem.level == BAD}
    for key in alerts.firing():
        code = key.removeprefix(host.ALERT_PREFIX)
        if code in PAGED and code not in wanted:
            alerts.resolve(code, f"Over: {code}", f"The status check of {stamp(reading.at_ns)} no longer finds it.")
    for code, problem in wanted.items():
        alerts.fire(code, f"HFT Lab: {code}", problem.text, 5 if problem.dead_man else 4)


# ---- the command -----------------------------------------------------------------------------------------------


def _mapping(path: Path) -> dict[str, Any]:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StatusError(f"cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StatusError(f"{path} is not a mapping")
    return loaded


def load_sources(
    config: Path = CONFIG,
    recorder_config: Path = RECORDER_CONFIG,
    shadow_config: Path = SHADOW_CONFIG,
    calendar: Path = CALENDAR,
    state: Path | None = None,
    trader_config: Path = TRADER_CONFIG,
) -> Sources:
    """The places and the limits, from the config files that already own them."""
    settings = _mapping(config).get("status")
    limits = settings if isinstance(settings, dict) else {}
    dead_man, warn = limits.get("dead_man_seconds"), limits.get("disk_warn_percent")
    if type(dead_man) is not int or dead_man <= 0 or type(warn) is not int or not 0 < warn <= 100:
        raise StatusError(f"{config}: status needs a positive dead_man_seconds and a disk_warn_percent of 1 to 100")
    root, journal_dir = _mapping(recorder_config).get("root"), _mapping(shadow_config).get("journal_dir")
    if not isinstance(root, str) or not isinstance(journal_dir, str):
        raise StatusError(f"{recorder_config} needs a root and {shadow_config} a journal_dir")
    trader_dir = _mapping(trader_config).get("state_dir") if trader_config.is_file() else None
    return Sources(
        record_root=Path(root), shadow_journal=Path(journal_dir) / SHADOW_JOURNAL,
        state=state or host.state_dir(host.settings()["HFT_STATE_DIR"]), calendar=load_calendar(calendar),
        dead_man_ns=dead_man * SECOND, disk_warn_percent=warn,
        trader_status=Path(trader_dir) / STATUS_FILE if isinstance(trader_dir, str) else None,
        lineup=Path(trader_dir) / LINEUP_FILE if isinstance(trader_dir, str) else None,
    )  # fmt: skip


def main(argv: Sequence[str] | None = None, clock: Clock | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.status", description="One screen on the platform.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--recorder-config", type=Path, default=RECORDER_CONFIG)
    parser.add_argument("--shadow-config", type=Path, default=SHADOW_CONFIG)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--json", action="store_true", help="print the reading as JSON")
    parser.add_argument("--alert", action="store_true", help="page the owner on a change (once per problem)")
    parser.add_argument("--quiet", action="store_true", help="print only what is wrong")
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.config, args.recorder_config, args.shadow_config, args.calendar)
    except (StatusError, CalendarError) as exc:
        print(f"status: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    clock = clock or WallClock()
    reading = read(sources, clock.now_ns())
    if args.alert:
        sender = sender_from(host.settings(), lambda line: print(line, file=sys.stderr, flush=True))
        page(reading, Alerts(sources.state / "alerts", sender, clock))
    if args.json:
        print(json.dumps(as_dict(reading), indent=2, sort_keys=True))
    elif args.quiet:
        for problem in reading.problems:
            print(f"status: [{problem.level}] {problem.code}: {problem.text}")
    else:
        print(render(reading))
    return exit_code(reading)


if __name__ == "__main__":
    sys.exit(main())
