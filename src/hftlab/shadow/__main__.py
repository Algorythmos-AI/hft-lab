"""`python -m hftlab.shadow`: run the sleeves on quotes, their orders on the shadow venue (docs/runbooks/shadow.md).

    python -m hftlab.shadow --day var/record/2026-10-14      over a recorded day (several days: one session)
    python -m hftlab.shadow --venue ibkr_paper               over live quotes from the paper gateway
    python -m hftlab.shadow --verify var/shadow/live.jsonl   check a journal's hash chain

No mode sends an order to a broker: the live mode holds a market-data view with no order methods, and every
order goes to the shadow venue. The result is printed as JSON and is the last entry of the journal.

Exit codes: 0 done, 1 a journal that does not verify, 2 refused or misconfigured.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from hftlab.calendar import load_calendar
from hftlab.calendar.events import CalendarError
from hftlab.core import Instrument, InstrumentKind, WallClock
from hftlab.core._checks import pair as check_pair
from hftlab.record.format import CorruptSegment, RecordedEvent
from hftlab.replay.reader import events, instruments
from hftlab.safety.lock import PaperOnlyError
from hftlab.safety.venues import UnknownVenueError, venue
from hftlab.shadow.journal import Journal, JournalError, verify
from hftlab.shadow.runner import MS, Shadow, ShadowError, live_events, open_live, run
from hftlab.strategy import ControlSleeve, IdleSleeve, Sleeve
from hftlab.venues.port import VenueError
from hftlab.venues.sim import CostModel, CostModelError, load_cost_model

SECOND = 1_000 * MS
CONFIG = Path("config/shadow.yaml")
COSTS = Path("config/cost_model.yaml")
CALENDAR = Path("config/calendar")


class ConfigError(ValueError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("journal_dir"), str):
        raise ConfigError(f"{path} must be a mapping with a journal_dir")
    for key in ("seed", "heartbeat_ms"):
        if type(loaded.get(key)) is not int or loaded[key] < 0:
            raise ConfigError(f"{path}: {key} must be a whole, non-negative number")
    if not isinstance(loaded.get("sleeves"), list) or not loaded["sleeves"]:
        raise ConfigError(f"{path}: `sleeves` must list at least one sleeve")
    loaded["pairs"] = tuple(parse_pair(entry) for entry in loaded.get("pairs") or [])
    return loaded


def parse_pair(entry: object) -> tuple[str, InstrumentKind]:
    if not isinstance(entry, dict) or set(entry) != {"pair", "form"} or entry["form"] not in ("cash", "cfd"):
        raise ConfigError(f"a pair is written {{pair: EUR.USD, form: cash}} with form cash or cfd, not {entry!r}")
    try:
        return check_pair(entry["pair"]), InstrumentKind(entry["form"].upper())
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def minimum_sizes(traded: Sequence[Instrument], model: CostModel) -> dict[str, int]:
    """The smallest order each pair takes: the larger of the venue's and the cost model's minimum, on the size grid."""
    out = {}
    for instrument in traded:
        smallest = max(instrument.min_order_size, model.min_order_size)
        out[instrument.pair] = -(-smallest // instrument.size_increment) * instrument.size_increment
    return out


def make_sleeves(entries: Sequence[object], sizes: Mapping[str, int]) -> list[Sleeve[Any]]:
    out: list[Sleeve[Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ConfigError(f"a sleeve needs a name and a kind, not {entry!r}")
        kind, rest = entry.get("kind"), {key: value for key, value in entry.items() if key not in ("name", "kind")}
        try:
            if kind == "idle" and not rest:
                out.append(IdleSleeve(entry["name"]))
            elif kind == "control" and set(rest) == {"seed", "entry_per_million", "hold_seconds", "retry_seconds"}:
                if any(type(value) is not int for value in rest.values()):
                    raise ConfigError(f"sleeve {entry['name']}: its four settings must be whole numbers")
                out.append(
                    ControlSleeve(
                        entry["name"],
                        seed=rest["seed"],
                        sizes=sizes,
                        entry_per_million=rest["entry_per_million"],
                        hold_ns=rest["hold_seconds"] * SECOND,
                        retry_ns=rest["retry_seconds"] * SECOND,
                    )
                )
            else:
                raise ConfigError(f"sleeve {entry['name']}: unknown kind or settings for kind {kind!r}: {sorted(rest)}")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    return out


def recorded(days: Sequence[Path]) -> tuple[list[Instrument], Iterator[RecordedEvent]]:
    described: dict[str, Instrument] = {}
    for day in days:
        if not day.is_dir():
            raise ConfigError(f"{day} is not a day folder")
        for pair, instrument in instruments(day).items():
            if described.setdefault(pair, instrument) != instrument:
                raise ConfigError(f"{day.name} describes {pair} differently from the days before it")
    if not described:
        raise ConfigError("the recording does not say what its instruments are; it cannot be priced")

    def stream() -> Iterator[RecordedEvent]:
        for day in days:
            yield from events(day)

    return list(described.values()), stream()


def run_shadow(args: argparse.Namespace, config: dict[str, Any]) -> int:
    model = load_cost_model(args.cost_model)
    calendar = load_calendar(args.calendar)
    journal_dir = Path(config["journal_dir"])
    source = None
    if args.day:
        traded, stream = recorded(args.day)
        path = args.journal or journal_dir / f"{args.day[0].name}.jsonl"
        if path.exists() and path.stat().st_size:
            raise ConfigError(f"{path} already holds a journal: a recorded day is run into a new file each time")
        label = "recording:" + ",".join(day.name for day in args.day)
    else:
        try:
            if not venue(args.venue).needs_paper_lock:
                raise ConfigError(f"{args.venue} has no market data of its own to follow")
        except UnknownVenueError as exc:
            raise ConfigError(str(exc.args[0])) from exc
        if not config["pairs"]:
            raise ConfigError("live mode needs `pairs` in the config")
        from hftlab.venues.ibkr.feed import market_data

        clock = WallClock()
        source = market_data(clock, host=args.host, port=args.port, client_id=args.client_id)
        traded = open_live(source, config["pairs"])
        stopping: list[int] = []
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda received, _frame: stopping.append(received))
        names = [instrument.pair for instrument in traded]
        stream = live_events(source, clock, names, lambda: bool(stopping), heartbeat_ns=config["heartbeat_ms"] * MS)
        path = args.journal or journal_dir / "live.jsonl"
        label = f"venue:{args.venue}"
    sleeves = make_sleeves(config["sleeves"], minimum_sizes(traded, model))
    shadow = Shadow(traded, model, sleeves, Journal(path), calendar=calendar, seed=config["seed"], source=label)
    if model.placeholder:
        print("shadow: the cost model is a PLACEHOLDER; no result of this run measures anything", file=sys.stderr)
    print(f"shadow: session {shadow.session} over {label}, journal {path}", file=sys.stderr, flush=True)
    try:
        result = run(shadow, stream)
    finally:
        if source is not None:
            source.disconnect()
    print(json.dumps({**result, "journal": str(path), "cost_model_is_placeholder": model.placeholder}, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.shadow", description="Run sleeves on the shadow venue.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--cost-model", type=Path, default=COSTS)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--day", type=Path, nargs="+", help="recorded day folder(s), in order")
    parser.add_argument("--venue", default="ibkr_paper", help="live mode: a registered venue that has market data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=78)
    parser.add_argument("--journal", type=Path, help="where to write the journal (default: under journal_dir)")
    parser.add_argument("--verify", type=Path, help="check a journal's hash chain and exit")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            state = verify(args.verify)
            report = {"journal": str(args.verify), "ok": True, "entries": state.entries, "sessions": state.sessions}
            print(json.dumps({**report, "torn_last_line": state.torn, "last_hash": state.last_hash}, indent=2))
            return 0
        return run_shadow(args, load_config(args.config))
    except JournalError as exc:
        print(f"shadow: the journal does not verify: {exc}", file=sys.stderr)
        return 1
    except (ConfigError, CostModelError, CalendarError, CorruptSegment, ShadowError) as exc:
        print(f"shadow: {exc}", file=sys.stderr)
        return 2
    except PaperOnlyError as exc:
        print(f"shadow: refused by the paper-only lock: {exc}", file=sys.stderr)
        return 2
    except VenueError as exc:
        print(f"shadow: the venue failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
