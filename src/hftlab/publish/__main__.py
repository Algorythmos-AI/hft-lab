"""`python -m hftlab.publish`: build the status snapshot, check it against its contract, and print or send it.

    python -m hftlab.publish              print the snapshot. Nothing is sent: a dry run is the default
    python -m hftlab.publish --dry-run    the same, said out loud
    python -m hftlab.publish --send       send it to the status site, signed

The snapshot is what contracts/hft-snapshot.v1.schema.json defines, built from the reading the status command
shows (hftlab.ops.status). The address, the key id and the key come from the environment: HFT_PUBLISH_URL,
HFT_PUBLISH_KEY_ID, HFT_PUBLISH_KEY. With one of them missing nothing is sent, and it says which. Its runbook
is docs/runbooks/publish.md.

Exit codes: 0 printed, sent, or the site already holds a newer snapshot; 1 the send failed; 2 refused: a
setting or a file could not be read, the snapshot does not conform, or the address or a key is missing.
"""
from __future__ import annotations

import argparse
import functools
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from hftlab.calendar import CalendarError
from hftlab.core import Clock, WallClock
from hftlab.ops import host, status
from hftlab.ops.post import Poster, post
from hftlab.publish import contract
from hftlab.publish.send import ALREADY_NEWER, Refused, send
from hftlab.publish.snapshot import SnapshotError, build, commit_of, load_phases

SECOND = 1_000_000_000
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


def timeout_of(config: Path) -> int:
    loaded: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    seconds = loaded.get("publish", {}).get("timeout_seconds") if isinstance(loaded, dict) else None
    if type(seconds) is not int or seconds <= 0:
        raise SnapshotError(f"{config}: publish.timeout_seconds must be a positive whole number")
    return seconds


def run(
    args: argparse.Namespace, settings: Mapping[str, str], clock: Clock, poster: Poster | None = None
) -> int:
    """The command, with its clock, its settings and its transport handed in."""
    try:
        schema = contract.load(args.contract)
        sources = status.load_sources(args.config, args.recorder_config, args.shadow_config, args.calendar)
        phases = load_phases(args.config)
        poster = poster or functools.partial(post, timeout_s=timeout_of(args.config))
    except (contract.ContractError, status.StatusError, CalendarError, SnapshotError) as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    now_ns = clock.now_ns()
    snapshot = build(status.read(sources, now_ns), phases=phases, version=commit_of(Path.cwd()))
    if found := contract.problems(snapshot, schema):
        print(f"publish: the snapshot does not conform to its contract: {'; '.join(found)}", file=sys.stderr)
        return EXIT_REFUSED
    if not args.send:
        print("publish: DRY RUN, nothing is sent", file=sys.stderr)
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return EXIT_OK
    try:
        outcome = send(snapshot, schema, settings, poster, now_ns // SECOND)
    except Refused as exc:
        print(f"publish: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if outcome.ok:
        print(f"publish: sent {snapshot['run_id']} ({outcome})")
        return EXIT_OK
    if outcome.status == ALREADY_NEWER:
        print(f"publish: the site already holds a newer snapshot than {snapshot['run_id']} ({outcome})")
        return EXIT_OK
    print(f"publish: NOT sent: {outcome}", file=sys.stderr)
    return EXIT_FAILED


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    poster: Poster | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.publish", description="Print or send the snapshot.")
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--dry-run", action="store_true", help="print the snapshot and send nothing (the default)")
    how.add_argument("--send", action="store_true", help="send the snapshot to the status site")
    parser.add_argument("--config", type=Path, default=status.CONFIG)
    parser.add_argument("--recorder-config", type=Path, default=status.RECORDER_CONFIG)
    parser.add_argument("--shadow-config", type=Path, default=status.SHADOW_CONFIG)
    parser.add_argument("--calendar", type=Path, default=status.CALENDAR)
    parser.add_argument("--contract", type=Path, default=contract.CONTRACT)
    args = parser.parse_args(argv)
    return run(args, host.settings() if settings is None else settings, clock or WallClock(), poster)


if __name__ == "__main__":
    sys.exit(main())
