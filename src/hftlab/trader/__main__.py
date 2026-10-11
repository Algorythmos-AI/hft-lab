"""`python -m hftlab.trader`: the process that trades, on the broker's paper account and on nothing else.

Its runbook is docs/runbooks/trader.md.

    python -m hftlab.trader                              run, until stopped
    python -m hftlab.trader --replay var/trader/days/2026-10-14     replay a day and compare its decisions
    python -m hftlab.trader --stages                     each sleeve's stage, and its clean days in a row
    python -m hftlab.trader --reset LATCH --operator NAME           ask the running trader to reset a latch
    python -m hftlab.trader --demote SLEEVE off --operator NAME     ask it to take a sleeve down (off or shadow)
    python -m hftlab.trader --dry-run-seconds 60 --state-dir DIR    the same loop on the fake broker; no gateway
    python -m hftlab.trader --housekeep                  compress the closed parts of the tapes; say what they use

A place whose settings say `scorer: process` is scored by a child of this process (ADR 0023, section 5). How
long the child may take is `scorer:` in the learning settings (--learn), which are read for that and nothing
else, and only when a place asks for a process.

What it refuses to start on (exit code 2): a venue that is not the registered paper venue, a paper-only lock
that does not pass, settings or risk limits that cannot be read, a sleeve whose stage lets it place paper
orders while the limits are not in force, and a tape quota that cannot hold the reserve kept for closing.

The tapes at a limit are not a reason to refuse (hftlab.trader.room). At their soft limit a session starts on
exits only and says so; at their hard limit it closes what the broker book holds, ends, and the process waits
for room, which the housekeeping job makes, and then begins the next session by itself.

With the limits not in force it runs in SHADOW-ONLY mode, and says so: it holds a view of the venue that has no
order methods, builds no order manager, and every sleeve's orders go to the shadow venue alone. That is what
the shipped configuration gives.

A session lasts until the 17:00 New York rollover or until the process is told to stop; after a rollover the
next session starts in the same process, the way a restart would. On SIGTERM it takes no new turn, leaves
working orders to the order manager's rules, flushes its journals and exits 0.

Exit codes: 0 stopped on purpose (or: the replay matched), 1 the venue failed or a replay differed, 2 refused.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import signal
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import IO, Any

from hftlab.calendar import Calendar, CalendarError, load_calendar
from hftlab.core import Clock, Instrument, InstrumentKind, SimClock, WallClock
from hftlab.journal import JournalError, read
from hftlab.learn.config import LearnConfigError
from hftlab.learn.config import load as load_learning
from hftlab.lineup.serve import PROCESS
from hftlab.ops import host
from hftlab.ops.alerts import Alerts, sender_from
from hftlab.risk import ROOM, RiskConfig, RiskConfigError, RiskNotInForce, file_switches, load_risk_config
from hftlab.safety.lock import PaperOnlyError
from hftlab.safety.venues import UnknownVenueError, venue
from hftlab.scoring.client import Times
from hftlab.shadow.__main__ import ConfigError
from hftlab.trader import room
from hftlab.trader.config import Settings, TraderConfigError, digest, load
from hftlab.trader.replay import replay_day
from hftlab.trader.session import Notify, Silent, Trader, TraderRefused
from hftlab.trader.stages import OFF, SHADOW
from hftlab.trader.tape import REQUESTS, LiveEdge, TapeError, housekeep, leave_request, tape_bytes
from hftlab.venues.port import Venue, VenueError
from hftlab.venues.sim import CostModel, CostModelError, load_cost_model
from hftlab.venues.view import MarketDataView, QuoteSource

SECOND = 1_000_000_000
CONFIG = Path("config/trader.yaml")
RISK = Path("config/risk.yaml")
COSTS = Path("config/cost_model.yaml")
LEARN = Path("config/learn.yaml")
CALENDAR = Path("config/calendar")
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


def say(text: str) -> None:
    print(f"trader: {text}", file=sys.stderr, flush=True)


def limits(path: Path) -> RiskConfig | None:
    """The risk limits when they are in force; None when the file is well formed and says they are not."""
    try:
        return load_risk_config(path)
    except RiskNotInForce:
        return None


def scorer_times(path: Path, settings: Settings) -> Times | None:
    """How long a scorer process may take, when a place asks for one; None when every place scores in process."""
    if not any(entry.get("scorer") == PROCESS for entry in settings.sleeves):
        return None
    learning = load_learning(path)
    return Times(
        learning.scorer_timeout_ns, learning.scorer_start_ns, learning.scorer_restart_first_ns,
        learning.scorer_restart_last_ns,
    )  # fmt: skip


def config_digest(args: argparse.Namespace) -> str:
    return digest([args.config, args.risk, args.cost_model, *sorted(args.calendar.glob("*.yaml"))])


@contextlib.contextmanager
def only_one(state: Path) -> Iterator[None]:
    """One trader per state directory: two would write the same journals and send the same orders."""
    state.mkdir(parents=True, exist_ok=True)
    with (state / "trader.lock").open("w") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TraderRefused(f"another trader holds {state}") from exc
        yield


def keep_house(settings: Settings, risk: Path) -> int:
    """Compress the closed parts of the tapes. It takes no lock: the part a running trader writes is left alone.

    It says what the tapes use against their limits. The hard limit depends on the risk limits; when those
    cannot be read, or the quota cannot hold the reserve, the housekeeping is done all the same and only the
    quota is named.
    """
    days = settings.state_dir / "days"
    done = housekeep(days)
    against = "no quota" if settings.tape_quota_bytes is None else f"a quota of {settings.tape_quota_bytes}"
    try:
        found = room.limits(settings, limits(risk))
    except (RiskConfigError, TraderConfigError):
        found = None
    if found is not None:
        against = f"soft limit {found.soft}, hard limit {found.hard}, {against}"
    say(f"tapes: compressed {done} closed part(s); {tape_bytes(days)} bytes on disk, {against}")
    return EXIT_OK


def serve(
    edge: LiveEdge,
    settings: Settings,
    *,
    model: CostModel,
    calendar: Calendar,
    risk: RiskConfig | None,
    notify: Notify,
    config_digest: str,
    should_stop: Callable[[], bool],
    times: Times | None = None,
) -> int:
    """Session after session until told to stop. Each starts as a restart would: from the journals, blind."""
    sessions = 0
    while not should_stop():
        trader = Trader(
            edge, settings, edge.open(), model=model, calendar=calendar, risk=risk, notify=notify,
            config_digest=config_digest, scorer_times=times,
        )  # fmt: skip
        sessions += 1
        say(f"session {trader.session} of {trader.label}; stages {json.dumps(trader.stages.all(), sort_keys=True)}")
        if edge.level != ROOM:
            found = edge.room() or {}
            say(
                f"EXITS ONLY: the tapes hold {found.get('used_bytes')} bytes, at their {room.NAMES[edge.level]} limit "
                f"(soft {found.get('soft_bytes')}, hard {found.get('hard_bytes')}, quota {found.get('quota_bytes')}). "
                "Nothing is opened until room is made; see the runbook, section 8."
            )
        trader.run()
        print(json.dumps(trader.close(), sort_keys=True), flush=True)
        if trader.flat_at_hard_limit:
            say("the tapes are at their hard limit and the broker book is flat: waiting for room, with no session")
            if edge.wait_for_room():
                say("the tapes have room again")
    return sessions


def fake(pairs: Sequence[tuple[str, InstrumentKind]], clock: SimClock) -> tuple[Any, Callable[[], None]]:
    """The fake broker with a moving book for each pair, and the function that keeps a second of quotes ahead."""
    from hftlab.venues.fake import FakeConfig, build

    sizes = {"min_order_size": 20_000, "size_increment": 1_000, "price_increment": 5}
    instruments = [
        Instrument.from_pair(pair, price_scale=3 if pair.endswith(".JPY") else 5, kind=kind, **sizes)
        for pair, kind in pairs
    ]
    broker = build(clock, instruments, FakeConfig(), seed=1, auto_advance=True)
    scripted = [clock.now_ns()]

    def keep_ahead() -> None:
        while scripted[0] <= clock.now_ns() + SECOND:
            for number, (pair, _) in enumerate(pairs):
                mid = 151_230 if pair.endswith(".JPY") else 108_505
                walk = {"count": 20, "interval_ns": 50_000_000, "mid": mid, "spread": 10, "step": 5}
                broker.market.generate_quotes(pair, start_ns=scripted[0] + number * 1_000_000, **walk)
            scripted[0] += SECOND

    return broker, keep_ahead


def run_live(args: argparse.Namespace, settings: Settings) -> int:
    if not venue(settings.venue).needs_paper_lock:
        raise TraderRefused(f"{settings.venue} is not a broker's paper account")
    model, calendar, risk = load_cost_model(args.cost_model), load_calendar(args.calendar), limits(args.risk)
    times = scorer_times(args.learn, settings)
    tape = room.edge_options(settings, risk)  # refused here, before any connection, when the quota is too small
    stopping: list[int] = []
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda received, _frame: stopping.append(received))
    switch_dir = args.state_dir or host.state_dir(host.settings()["HFT_STATE_DIR"])
    names = [risk.kill_switch, risk.flatten_switch] if risk else [host.KILL, host.FLATTEN]
    clock: Clock
    source: QuoteSource
    full: Venue | None = None
    notify: Notify
    if args.dry_run_seconds is not None:
        sim = SimClock(1_791_342_000 * SECOND)  # Wednesday 2026-10-07 03:00 UTC: an open market, in the past
        broker, keep_ahead = fake(settings.pairs, sim)
        until = sim.now_ns() + args.dry_run_seconds * SECOND

        def should_stop() -> bool:
            keep_ahead()
            return bool(stopping) or sim.now_ns() >= until

        clock, notify = sim, Silent()
        source, full = (broker, broker) if risk else (MarketDataView(broker), None)
    else:
        clock = WallClock()
        if risk is not None:
            from hftlab.venues.ibkr.paper import paper_venue

            source = full = paper_venue(clock, host=args.host, port=args.port, client_id=args.client_id)
        else:
            from hftlab.venues.ibkr.feed import market_data

            source = market_data(clock, host=args.host, port=args.port, client_id=args.client_id)
        sender = sender_from(host.settings(), lambda line: print(line, file=sys.stderr, flush=True))
        notify = Alerts(switch_dir / "alerts", sender, WallClock())

        def should_stop() -> bool:
            return bool(stopping)

    if risk is None:
        say("SHADOW-ONLY: the risk limits are not in force. No order can reach a broker from this run.")
    else:
        say(f"PAPER: limits in force by {risk.decision}. Orders go to the paper account of {settings.venue}.")
    if model.placeholder:
        say("the cost model is a PLACEHOLDER; no result of this run measures anything")
    with only_one(settings.state_dir):
        edge = LiveEdge(
            source, clock, pairs=settings.pairs, switches=file_switches(switch_dir, names),
            requests=settings.state_dir / REQUESTS, should_stop=should_stop, venue=full, poll_ns=settings.poll_ns,
            beat_ns=settings.beat_ns, fsync=settings.fsync, **tape,
        )  # fmt: skip
        try:
            serve(
                edge, settings, model=model, calendar=calendar, risk=risk, notify=notify,
                config_digest=config_digest(args), should_stop=should_stop, times=times,
            )  # fmt: skip
        finally:
            source.disconnect()
    return EXIT_OK


def run_replay(args: argparse.Namespace, settings: Settings) -> int:
    model, calendar, risk = load_cost_model(args.cost_model), load_calendar(args.calendar), limits(args.risk)
    with tempfile.TemporaryDirectory(prefix="hft-replay-") as scratch:
        found = replay_day(
            args.replay, Path(scratch), settings, model=model, calendar=calendar, risk=risk,
            config_digest=config_digest(args),
        )  # fmt: skip
    print(json.dumps({"day": args.replay.name, "sessions": [one.as_dict() for one in found]}, indent=2))
    return EXIT_OK if all(one.identical for one in found) else EXIT_FAILED


def show_stages(settings: Settings, out: IO[str]) -> int:
    """Read the stage journal and print where each sleeve stands. It opens nothing for writing."""
    path = settings.state_dir / "stages.jsonl"
    stage = {name: ladder.start for name, ladder in settings.rollout.items()}
    days: dict[str, list[tuple[str, bool]]] = {name: [] for name in stage}
    for entry in read(path)[1]:
        sleeve = entry.get("sleeve")
        if entry.get("type") == "stage" and sleeve in stage:
            stage[str(sleeve)] = str(entry.get("to"))
        elif entry.get("type") == "day" and sleeve in days:
            days[str(sleeve)].append((str(entry.get("stage")), entry.get("clean") == 1))
    for name in sorted(stage):
        run = 0
        for at, clean in reversed(days[name]):
            if not clean or at != stage[name]:
                break
            run += 1
        print(f"{name}: {stage[name]}; {run} clean trading day(s) in a row in this stage", file=out)
    return EXIT_OK


def ask(settings: Settings, what: str, subject: str, value: str, operator: str) -> int:
    """Leave a request for the running trader. It acts on it at its next beat and journals what it did."""
    leave_request(settings.state_dir / REQUESTS, what, subject, value, operator)
    say(f"asked: {what} {subject} {value}".rstrip() + f", by {operator}. The trader's journal says what came of it.")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.trader", description="Trade on the paper account.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--risk", type=Path, default=RISK)
    parser.add_argument("--cost-model", type=Path, default=COSTS)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--learn", type=Path, default=LEARN, help="the learning settings: a scorer process's times")
    parser.add_argument("--state-dir", type=Path, help="where KILL and FLATTEN are looked for (default: the host's)")
    parser.add_argument("--journals", type=Path, help="the trader's own folder (default: the config's state_dir)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=77)
    parser.add_argument("--dry-run-seconds", type=int, help="run this many simulated seconds on the fake broker")
    parser.add_argument("--replay", type=Path, help="a day folder of the trader: replay it and compare")
    parser.add_argument("--stages", action="store_true", help="print each sleeve's stage and exit")
    parser.add_argument("--housekeep", action="store_true", help="compress the closed parts of the tapes and exit")
    parser.add_argument("--reset", metavar="LATCH", help="ask the running trader to reset this latch")
    parser.add_argument("--demote", nargs=2, metavar=("SLEEVE", "STAGE"), help="ask it to take a sleeve down")
    parser.add_argument("--operator", help="who is asking; it goes into the journal")
    args = parser.parse_args(argv)
    try:
        settings = load(args.config)
        if args.journals:
            settings = dataclasses.replace(settings, state_dir=args.journals)
        if args.stages:
            return show_stages(settings, sys.stdout)
        if args.housekeep:
            return keep_house(settings, args.risk)
        if args.reset or args.demote:
            if not args.operator:
                raise TraderRefused("--operator NAME is needed: the journal says who asked")
            if args.reset:
                return ask(settings, "reset", args.reset, "", args.operator)
            if args.demote[0] not in settings.names or args.demote[1] not in (OFF, SHADOW):
                raise TraderRefused(f"a sleeve of {list(settings.names)} can be taken down to {OFF} or {SHADOW}")
            return ask(settings, "stage", args.demote[0], args.demote[1], args.operator)
        if args.replay:
            return run_replay(args, settings)
        return run_live(args, settings)
    except (
        TraderRefused, TraderConfigError, ConfigError, RiskConfigError, CostModelError, CalendarError, LearnConfigError,
    ) as exc:  # fmt: skip
        say(f"refused: {exc}")
        return EXIT_REFUSED
    except UnknownVenueError as exc:
        say(f"refused: {exc.args[0]}")
        return EXIT_REFUSED
    except PaperOnlyError as exc:
        say(f"refused by the paper-only lock: {exc}")
        return EXIT_REFUSED
    except (JournalError, TapeError) as exc:
        say(f"refused: a journal or a tape cannot be used: {exc}")
        return EXIT_REFUSED
    except VenueError as exc:
        say(f"the venue failed: {exc}")
        return EXIT_FAILED
    except OSError as exc:  # raised by the housekeeping: a compressed copy did not read back, or the disk refused
        say(f"FAILED: {exc}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
