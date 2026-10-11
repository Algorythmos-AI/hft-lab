"""`python -m hftlab.history import`: read exported tick files from a folder into the store of imported history.

    python -m hftlab.history import --from exports/a --label vendor-a            import the folder
    python -m hftlab.history import --from exports/a --label vendor-a --dry-run  read and count; write nothing

The folder holds the files and an `import.yaml` that describes them (docs/runbooks/history-import.md). The
label is the owner's name for where the files came from; it goes into every imported day and into every count
made from one. The command prints a JSON report: every file with its SHA-256 and what was read, kept and
dropped by reason, and every day with what became of it.

It reads files and writes files. It downloads nothing. Exit codes: 0 done, 2 refused, and then nothing was
written: the description leaves something unstated, a file is not what it says, the store is the recorder's,
or a day is already in the store from other inputs.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from hftlab.calendar import CalendarError, load_calendar
from hftlab.history.importer import SECOND, Importer, ImportRefused
from hftlab.record.format import CorruptSegment

STORE = Path("var/history")
RECORDER = Path("config/recorder.yaml")
CALENDAR = Path("config/calendar")
EXIT_OK, EXIT_REFUSED = 0, 2


def recorder_root(config: Path) -> Path:
    """Where the recorder writes, as its own settings say. Without it the two stores cannot be told apart."""
    try:
        loaded: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ImportRefused(f"cannot read {config}, which says where the recorder writes: {exc}") from exc
    root = loaded.get("root") if isinstance(loaded, dict) else None
    if not isinstance(root, str) or not root:
        raise ImportRefused(f"{config} does not say where the recorder writes")
    return Path(root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.history", description="Imported history, from files.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("import", help="read a folder of exported tick files into the import store")
    run.add_argument("--from", dest="folder", type=Path, required=True, help="the folder: files and import.yaml")
    run.add_argument("--label", required=True, help="your name for the source: letters, digits, . - _ (no space)")
    run.add_argument("--store", type=Path, default=STORE, help="the store of imported history, never the recorder's")
    run.add_argument("--recorder-config", type=Path, default=RECORDER, help="says where the recorder writes")
    run.add_argument("--calendar", type=Path, default=CALENDAR, help="directory with holidays and macro events")
    run.add_argument("--gap-threshold-s", type=int, default=5, help="a silence longer than this is journalled")
    run.add_argument("--dry-run", action="store_true", help="read and count everything; write nothing")
    args = parser.parse_args(argv)
    try:
        if args.gap_threshold_s <= 0:
            raise ImportRefused("--gap-threshold-s is a positive number of seconds")
        importer = Importer(
            args.store, load_calendar(args.calendar), recorder_root=recorder_root(args.recorder_config),
            gap_threshold_ns=args.gap_threshold_s * SECOND,
        )  # fmt: skip
        report = importer.run(args.folder, args.label, dry_run=args.dry_run)
    except (ImportRefused, CalendarError, CorruptSegment) as exc:
        print(f"history: refused, nothing written: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    said = report.as_dict()
    print(json.dumps(said, indent=2, sort_keys=True))
    rows, days = said["rows"], said["days_by_status"]
    print(
        f"history: {'DRY RUN, nothing written. ' if args.dry_run else ''}{rows} rows; days {days}. "
        "source: imported, terms_verified: false. These are not this venue's quotes.",
        file=sys.stderr,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
