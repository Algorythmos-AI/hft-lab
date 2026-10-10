"""`python -m hftlab.record`: record quotes until told to stop (docs/runbooks/recorder.md).

    python -m hftlab.record                        record from the paper gateway, as config/recorder.yaml says
    python -m hftlab.record --dry-run-seconds 60   record a minute of the built-in fake broker; no gateway
    python -m hftlab.record --housekeep            compress closed hours and hand old ones to the offload, once

SIGTERM and SIGINT stop it cleanly: the last block is flushed and a STOP record is written. Exit codes: 0
stopped on request, 2 refused or misconfigured, 3 stopped because the quota was reached.
"""
from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from hftlab.calendar import load_calendar
from hftlab.core import Clock, Instrument, InstrumentKind, SimClock, WallClock
from hftlab.core._checks import pair as check_pair
from hftlab.record.recorder import MS, Recorder, RecorderSettings
from hftlab.record.store import DirectoryOffload, Housekeeper, Offload, Store, StoreSettings
from hftlab.safety.lock import PaperOnlyError
from hftlab.safety.venues import UnknownVenueError, venue
from hftlab.venues.port import VenueError
from hftlab.venues.view import MarketDataView, QuoteSource

SECOND = 1_000 * MS
CONFIG = Path("config/recorder.yaml")
CALENDAR = Path("config/calendar")
STATUS_FILE = "status.json"


class ConfigError(ValueError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} is not a mapping")
    whole = ("quota_bytes", "keep_hours", "block_records", "flush_ms", "heartbeat_ms", "gap_threshold_s")
    for key in whole:
        if type(loaded.get(key)) is not int or loaded[key] <= 0:
            raise ConfigError(f"{path}: {key} must be a positive whole number")
    pairs = loaded.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ConfigError(f"{path}: `pairs` must list at least one pair")
    loaded["pairs"] = tuple(parse_pair(entry) for entry in pairs)
    return loaded


def parse_pair(entry: object) -> tuple[str, InstrumentKind]:
    if not isinstance(entry, dict) or set(entry) != {"pair", "form"} or entry["form"] not in ("cash", "cfd"):
        raise ConfigError(f"a pair is written {{pair: EUR.USD, form: cash}} with form cash or cfd, not {entry!r}")
    try:
        return check_pair(entry["pair"]), InstrumentKind(entry["form"].upper())
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def store_settings(config: dict[str, Any]) -> StoreSettings:
    return StoreSettings(
        quota_bytes=config["quota_bytes"],
        keep_ns=config["keep_hours"] * 3_600 * SECOND,
        block_records=config["block_records"],
        fsync=bool(config.get("fsync", True)),
        gap_threshold_ns=config["gap_threshold_s"] * SECOND,
    )


def offload_of(config: dict[str, Any]) -> Offload | None:
    target = config.get("offload_dir")
    return DirectoryOffload(Path(target)) if target else None


def fake_source(pairs: Sequence[tuple[str, InstrumentKind]], clock: SimClock) -> tuple[QuoteSource, Any]:
    """A view of the fake broker with a moving book for each pair. Quotes are scripted a second at a time."""
    from hftlab.venues.fake import FakeConfig, build

    sizes = {"min_order_size": 20_000, "size_increment": 1_000, "price_increment": 5}
    instruments = [
        Instrument.from_pair(pair, price_scale=3 if pair.endswith(".JPY") else 5, kind=kind, **sizes)
        for pair, kind in pairs
    ]
    broker = build(clock, instruments, FakeConfig(), seed=1, auto_advance=True)
    return MarketDataView(broker), broker.market


def run_recorder(args: argparse.Namespace, config: dict[str, Any]) -> int:
    root = Path(args.root or config["root"])
    pairs: tuple[tuple[str, InstrumentKind], ...] = config["pairs"]
    settings = RecorderSettings(pairs, flush_ns=config["flush_ms"] * MS, heartbeat_ns=config["heartbeat_ms"] * MS)
    calendar = load_calendar(args.calendar)
    stopping: list[int] = []
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda received, _frame: stopping.append(received))

    clock: Clock
    if args.dry_run_seconds is not None:
        sim = SimClock(1_791_342_000 * SECOND)  # Wednesday 2026-10-07 03:00 UTC: an open market, in the past
        source, market = fake_source(pairs, sim)
        clock, until, scripted = sim, sim.now_ns() + args.dry_run_seconds * SECOND, [sim.now_ns()]

        def should_stop() -> bool:
            while scripted[0] <= sim.now_ns() + SECOND:  # keep one second of quotes ahead of the clock
                for number, (pair, _) in enumerate(pairs):
                    mid = 151_230 if pair.endswith(".JPY") else 108_505
                    walk = {"count": 100, "interval_ns": 10 * MS, "mid": mid, "spread": 10, "step": 5}
                    market.generate_quotes(pair, start_ns=scripted[0] + number * MS, **walk)
                scripted[0] += SECOND
            return bool(stopping) or sim.now_ns() >= until
    else:
        try:
            if not venue(args.venue).needs_paper_lock:
                raise ConfigError(f"{args.venue} has no market data of its own to record")
        except UnknownVenueError as exc:
            raise ConfigError(str(exc.args[0])) from exc
        from hftlab.venues.ibkr.feed import market_data

        clock = WallClock()
        source = market_data(clock, host=args.host, port=args.port, client_id=args.client_id)

        def should_stop() -> bool:
            return bool(stopping)

    names = [pair for pair, _ in pairs]
    store = Store(root, names, store_settings(config), calendar=calendar, offload=offload_of(config))
    recorder = Recorder(source, store, clock, settings, status_path=root / STATUS_FILE)
    print(f"record: recording {', '.join(names)} from {source.name} into {root}", file=sys.stderr, flush=True)
    code = recorder.run(should_stop)
    print(f"record: stopped after {recorder.records} records, next index {store.next_index}", file=sys.stderr)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.record", description="Record quotes into the store.")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    parser.add_argument("--root", help="where to record (default: the config's root)")
    parser.add_argument("--venue", default="ibkr_paper", help="a registered venue that has market data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=79)
    parser.add_argument("--dry-run-seconds", type=int, help="record this many simulated seconds of the fake broker")
    parser.add_argument("--housekeep", action="store_true", help="compress and offload closed hours, then exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.housekeep:
            root = Path(args.root or config["root"])
            report = Housekeeper(root, store_settings(config), offload_of(config)).run(WallClock().now_ns())
            print(f"record: compressed {report.compressed}, handed over {report.handed_over}, kept {report.refused}")
            return 0
        return run_recorder(args, config)
    except ConfigError as exc:
        print(f"record: {exc}", file=sys.stderr)
        return 2
    except PaperOnlyError as exc:
        print(f"record: refused by the paper-only lock: {exc}", file=sys.stderr)
        return 2
    except VenueError as exc:
        print(f"record: the venue failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
