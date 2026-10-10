"""The pre-flight gate: may the platform be installed on this host at all? (ADR 0007, decision 3)

    python -m hftlab.ops.preflight --check FILE     judge a day of samples; exit 0 pass, 1 fail, 2 unreadable

The host already runs two other trading desks. Before anything of ours is installed, the owner runs
`deploy/bin/hft-preflight-sample` there for a day. It appends one line a minute:

    2026-10-12T14:31:05Z mem_available_kb=1843200 mem_total_kb=4028000 swap_free_kb=0 load1=0.42 load5=0.40
        load15=0.38 disk_free_kb=14200000                                            (one line, no break)

This reads those lines and answers four questions, with the numbers:

  * Is it a day? Enough samples, over a long enough span, with no long hole, and not an old file.
  * Is there room in memory? The least memory available at any sample is the headroom at the other desks'
    peak. Under the threshold the platform is NOT installed.
  * Is there room on the disk? The least free space, less what this platform may take, must stay above the
    other desks' floor.
  * Swap and load are reported and decide nothing: no threshold for them was agreed.

The thresholds are in config/preflight.json, each with where it comes from. This module needs only the
standard library, because it runs on the host's own Python before the platform's environment exists; a test
runs it without any installed package to keep it so. It reads its two files and prints. It changes nothing.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from hftlab.core.clock import WallClock

CONFIG: Final = Path("config/preflight.json")
THRESHOLDS: Final = (
    "headroom_min_kb", "disk_floor_kb", "disk_budget_kb", "span_min_seconds", "samples_min", "gap_max_seconds",
    "age_max_seconds",
)  # fmt: skip
EXIT_PASS, EXIT_FAIL, EXIT_UNREADABLE = 0, 1, 2
_LINE: Final = re.compile(
    r"(?P<at>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})Z"
    r" mem_available_kb=(?P<available>[0-9]+) mem_total_kb=(?P<total>[0-9]+) swap_free_kb=(?P<swap>[0-9]+)"
    r" load1=(?P<load>[0-9]+\.[0-9]{2}) load5=[0-9]+\.[0-9]{2} load15=[0-9]+\.[0-9]{2}"
    r" disk_free_kb=(?P<disk>[0-9]+)"
)


class PreflightError(ValueError):
    """The samples or the thresholds could not be read as what they claim to be."""


@dataclass(frozen=True, slots=True)
class Sample:
    at_s: int  # UTC seconds
    mem_available_kb: int
    mem_total_kb: int
    swap_free_kb: int
    load_centi: int  # the one-minute load average, in hundredths
    disk_free_kb: int


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    found: str
    needed: str


@dataclass(frozen=True, slots=True)
class Report:
    checks: tuple[Check, ...]
    information: tuple[str, ...]
    numbers: Mapping[str, int | None]

    @property
    def passed(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        checks = [{"name": c.name, "ok": c.ok, "found": c.found, "needed": c.needed} for c in self.checks]
        return {"result": "pass" if self.passed else "fail", "checks": checks, "numbers": dict(self.numbers)}


def load_thresholds(path: Path) -> dict[str, int]:
    """The gate's numbers. Every one must be there, be a whole number and say who decided it."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreflightError(f"cannot read {path}: {exc}") from exc
    entries = loaded.get("thresholds") if isinstance(loaded, dict) else None
    if not isinstance(entries, dict) or set(entries) != set(THRESHOLDS):
        raise PreflightError(f"{path} must hold exactly these thresholds: {', '.join(THRESHOLDS)}")
    out = {}
    for name, entry in entries.items():
        well_formed = isinstance(entry, dict) and set(entry) == {"value", "decided_by", "source"}
        if not well_formed or type(entry["value"]) is not int or entry["value"] <= 0:
            raise PreflightError(f"{path}: {name} needs a positive whole `value`, a `decided_by` and a `source`")
        if entry["decided_by"] not in ("plan", "engineering") or not str(entry["source"]).strip():
            raise PreflightError(f"{path}: {name} must say whether the plan or engineering decided it, and where")
        out[name] = entry["value"]
    return out


def parse(text: str) -> tuple[list[Sample], int]:
    """(the samples, how many lines were not one). A line starting with `#` is the sampler's own comment."""
    samples, unreadable = [], 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = _LINE.fullmatch(line.strip())
        if match is None:
            unreadable += 1
            continue
        try:
            at = datetime.strptime(match["at"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            unreadable += 1
            continue
        whole, hundredths = match["load"].split(".")
        samples.append(
            Sample(
                int(at.timestamp()), int(match["available"]), int(match["total"]), int(match["swap"]),
                int(whole) * 100 + int(hundredths), int(match["disk"]),
            )
        )  # fmt: skip
    return samples, unreadable


def stamp(at_s: int) -> str:
    return datetime.fromtimestamp(at_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(seconds: int) -> str:
    return f"{seconds // 3_600} h {seconds % 3_600 // 60:02d} min"


def check(samples: Sequence[Sample], unreadable: int, thresholds: Mapping[str, int], now_s: int) -> Report:
    """Judge the samples. Pure: the same samples, thresholds and time give the same report."""
    need = thresholds
    if not samples:
        nothing = Check("samples", False, f"0 samples ({unreadable} unreadable lines)", f"{need['samples_min']}")
        return Report((nothing,), (), {"samples": 0, "unreadable_lines": unreadable})
    in_order = all(later.at_s > earlier.at_s for earlier, later in itertools.pairwise(samples))
    first, last = samples[0], samples[-1]
    span = last.at_s - first.at_s
    hole = max((b.at_s - a.at_s for a, b in itertools.pairwise(samples)), default=0)
    age = now_s - last.at_s
    tightest = min(samples, key=lambda s: (s.mem_available_kb, s.at_s))
    fullest = min(samples, key=lambda s: (s.disk_free_kb, s.at_s))
    left = fullest.disk_free_kb - need["disk_budget_kb"]
    busiest = max(samples, key=lambda s: (s.load_centi, -s.at_s))
    checks = (
        Check(
            "samples", len(samples) >= need["samples_min"] and in_order,
            f"{len(samples)}, {unreadable} unreadable lines" + ("" if in_order else ", NOT IN TIME ORDER"),
            f"at least {need['samples_min']}, each later than the one before",
        ),
        Check(
            "a full day", span >= need["span_min_seconds"],
            f"{_duration(span)}, {stamp(first.at_s)} to {stamp(last.at_s)}",
            f"at least {_duration(need['span_min_seconds'])}",
        ),
        Check("longest hole", hole <= need["gap_max_seconds"], f"{hole} s", f"at most {need['gap_max_seconds']} s"),
        Check(
            "how old", 0 <= age <= need["age_max_seconds"],
            f"the last sample is {age // 3_600} h old" if age >= 0 else "the last sample is in the future",
            f"at most {need['age_max_seconds'] // 3_600} h",
        ),
        Check(
            "memory headroom", tightest.mem_available_kb >= need["headroom_min_kb"],
            f"least available {tightest.mem_available_kb:,} kB of {tightest.mem_total_kb:,} kB, "
            f"at {stamp(tightest.at_s)}",
            f"at least {need['headroom_min_kb']:,} kB",
        ),
        Check(
            "disk floor", left >= need["disk_floor_kb"],
            f"least free {fullest.disk_free_kb:,} kB at {stamp(fullest.at_s)}; "
            f"less this platform's {need['disk_budget_kb']:,} kB leaves {left:,} kB",
            f"at least {need['disk_floor_kb']:,} kB left for the other desks",
        ),
    )  # fmt: skip
    weekdays = {datetime.fromtimestamp(s.at_s, tz=UTC).strftime("%A") for s in samples}
    information = (
        f"swap: least free {min(s.swap_free_kb for s in samples):,} kB",
        f"load average over one minute: highest {busiest.load_centi // 100}.{busiest.load_centi % 100:02d} "
        f"at {stamp(busiest.at_s)}",
        f"days sampled, in UTC: {', '.join(sorted(weekdays))}. The headroom is only the headroom at the other "
        "desks' peak if their heaviest jobs ran inside this window: check that they did",
    )
    numbers: dict[str, int | None] = {
        "samples": len(samples), "unreadable_lines": unreadable, "span_seconds": span, "longest_hole_seconds": hole,
        "age_seconds": age, "headroom_kb": tightest.mem_available_kb, "headroom_at_s": tightest.at_s,
        "mem_total_kb": tightest.mem_total_kb, "disk_free_min_kb": fullest.disk_free_kb,
        "disk_left_after_budget_kb": left, "swap_free_min_kb": min(s.swap_free_kb for s in samples),
        "load_max_centi": busiest.load_centi,
    }  # fmt: skip
    return Report(checks, information, numbers)


def render(report: Report) -> str:
    """One screen. The first line is the verdict."""
    verdict = "PASS: the platform may be installed" if report.passed else "FAIL: the platform is NOT installed"
    lines = [f"pre-flight: {verdict}"]
    for item in report.checks:
        lines.append(f"  [{'ok' if item.ok else 'NO'}] {item.name}: {item.found}")
        lines.append(f"       needed: {item.needed}")
    lines += [f"  for information, {line}" for line in report.information]
    if not report.passed:
        lines.append("  What now: a larger host, or the route without a gateway process (ADR 0007, ADR 0010).")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.preflight", description="Judge a day of samples.")
    parser.add_argument("--check", type=Path, required=True, metavar="FILE", help="what hft-preflight-sample wrote")
    parser.add_argument("--config", type=Path, default=CONFIG, help="the thresholds (config/preflight.json)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON instead of a screen")
    args = parser.parse_args(argv)
    try:
        thresholds = load_thresholds(args.config)
        try:
            text = args.check.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise PreflightError(f"cannot read {args.check}: {exc}") from exc
    except PreflightError as exc:
        print(f"pre-flight: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    samples, unreadable = parse(text)
    report = check(samples, unreadable, thresholds, WallClock().now_ns() // 1_000_000_000)
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True) if args.json else render(report))
    return EXIT_PASS if report.passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
