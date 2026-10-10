"""`python -m hftlab.replay --day <dir>`: summarise a recorded day, or verify it with --check.

    python -m hftlab.replay --day var/record/2026-10-14 --check

--check re-reads every block and verifies checksums, that the index has no hole, that the venue's sequence
never goes back within a run, and that the gap journal matches the records. It prints a JSON report. Exit
codes: 0 the day is intact, 1 it is not, 2 it could not be read. Unexplained gaps are counted in the report
and do not by themselves fail the check; with --strict they do.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from hftlab.calendar import load_calendar
from hftlab.record.format import CorruptSegment
from hftlab.replay.reader import check, events

SECOND = 1_000_000_000


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.replay", description="Read a recorded day back.")
    parser.add_argument("--day", type=Path, required=True, help="a day folder of the recording")
    parser.add_argument("--check", action="store_true", help="verify checksums, sequence and the gap journal")
    parser.add_argument("--strict", action="store_true", help="with --check: an unexplained gap is a failure too")
    parser.add_argument("--calendar", type=Path, default=Path("config/calendar"))
    parser.add_argument("--gap-threshold-s", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.day.is_dir():
        print(f"replay: {args.day} is not a day folder", file=sys.stderr)
        return 2
    if args.check:
        report = check(args.day, load_calendar(args.calendar), args.gap_threshold_s * SECOND)
        print(json.dumps(report.as_dict(), indent=2))
        return 0 if report.ok and not (args.strict and report.unexplained) else 1
    try:
        kinds = Counter(type(event).__name__ for event in events(args.day))
    except CorruptSegment as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"day": args.day.name, "events": dict(sorted(kinds.items()))}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
