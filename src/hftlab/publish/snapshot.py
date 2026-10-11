"""The status snapshot: what this desk tells the outside about itself (contracts/hft-snapshot.v1.schema.json).

It is built from the same reading as the status screen (hftlab.ops.status), so the screen on the host and the
page on the status site cannot disagree. What it holds to, each with a test:

  * `desk.mode` is "paper". Nothing here chooses it: there is no other value.
  * States, counts, times and codes only. A problem's sentence stays on the host; its code goes out.
  * A part that does not exist yet is absent, never zeros. There is no recorder part before something has
    been recorded on the paper venue, no sleeves or books part before a trader has run, no learning part
    before training has registered a candidate in the lineup journal, and no pairs part at all yet.
  * A model goes out as the first twelve characters of its hash: an id, and nothing of what it holds.
  * Money leaves as a number in one currency, from the shadow book for a sleeve. A result spread over several
    currencies is not added up: it goes out as null, "not worked out".
  * `expected_windows` lists the trading week that is open or the next one to open, and the week after it,
    so a host that falls silent over a weekend is still expected back on Sunday evening.
  * `as_of` is the time the snapshot was built, not the recorder's last beat: the site orders snapshots by
    it, and a stopped recorder must not make every later snapshot look older than the last.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.ops.status import VENUE, BookReading, Reading, TraderReading, stamp
from hftlab.publish.contract import SCHEMA_ID, SCHEMA_VERSION

PHASES: Final = ("F0", "F1", "F2", "F3", "F4", "F5", "F6", "F7")
PHASE_STATES: Final = ("done", "open", "not_started")
MAX_REASONS: Final = 20
SLEEVES: Final = ("control", "directional_change", "ml", "dl")  # the names the contract knows
ON_PAPER: Final = ("min_size", "normal", "stepped_down")
_COMMIT: Final = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class SnapshotError(ValueError):
    """A setting the snapshot needs could not be read."""


def load_phases(config: Path) -> dict[str, str]:
    """The state of each of the eight phases, from config/ops.yaml. All eight, and nothing else."""
    try:
        loaded: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SnapshotError(f"cannot read {config}: {exc}") from exc
    phases = loaded.get("phases") if isinstance(loaded, dict) else None
    if not isinstance(phases, dict) or tuple(phases) != PHASES or not set(phases.values()) <= set(PHASE_STATES):
        raise SnapshotError(f"{config}: `phases` must give F0 to F7, in order, each one of {', '.join(PHASE_STATES)}")
    return dict(phases)


def commit_of(root: Path) -> str | None:
    """The commit the code under `root` is checked out at, read from git's own files. None when it cannot be told."""
    git = root / ".git"
    try:
        if git.is_file():  # a worktree keeps a file that says where its git directory is
            pointer = git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git = root / pointer.removeprefix("gitdir:").strip()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.removeprefix("ref:").strip()
            common = git / "commondir"  # a worktree's branches are kept in the repository it belongs to
            shared = git / common.read_text(encoding="utf-8").strip() if common.is_file() else git
            loose = next((base / ref for base in (git, shared) if (base / ref).is_file()), None)
            if loose is not None:
                head = loose.read_text(encoding="utf-8").strip()
            else:
                packed = (shared / "packed-refs").read_text(encoding="utf-8").splitlines()
                head = next((line.split(" ")[0] for line in packed if line.endswith(f" {ref}")), "")
    except OSError:
        return None
    return head[:12] if _COMMIT.fullmatch(head) else None


def stage(reading: Reading) -> str:
    """How far the desk has come, by what is on the host: nothing, a recording, a shadow run, or paper orders."""
    trader = reading.trader
    if trader is not None and trader.mode == "paper" and any(sleeve.stage in ON_PAPER for sleeve in trader.sleeves):
        return "paper"
    if trader is not None or (reading.shadow is not None and reading.shadow.sessions):
        return "shadow"
    if reading.recorder is not None and reading.recorder.venue == VENUE:
        return "recording"
    return "foundation"


def _amount(nets: Mapping[str, int] | None, currency: str) -> float | None:
    """A result in `currency` as the contract's number; None when it is not all in that one currency."""
    if nets is None or any(amount for code, amount in nets.items() if code != currency):
        return None
    return round(nets.get(currency, 0) / 1_000_000, 2)


def _currency(trader: TraderReading) -> str:
    """The currency the desk's results are in: the one with anything in it, else the first by name."""
    totals = trader.shadow.net_total_micro or {}
    moved = sorted(code for code, amount in totals.items() if amount)
    return (moved or sorted(totals) or ["USD"])[0]


def _book(book: BookReading | None, currency: str) -> dict[str, object]:
    if book is None:
        return {"net_today": None, "net_total": None}
    return {"net_today": _amount(book.net_today_micro, currency), "net_total": _amount(book.net_total_micro, currency)}


def trader_parts(trader: TraderReading) -> dict[str, object]:
    """The `sleeves` and `books` parts, as the contract defines them, from what the trader last said."""
    currency = _currency(trader)
    sleeves = [
        {
            "name": sleeve.name.replace("-", "_"),
            "stage": sleeve.stage,
            "trades_today": sleeve.trades_today,
            "trades_total": sleeve.trades_total,
            "net_today": _amount(sleeve.net_today_micro, currency),
            "net_total": _amount(sleeve.net_total_micro, currency),
            "currency": currency,
        }
        for sleeve in trader.sleeves
        if sleeve.name.replace("-", "_") in SLEEVES
    ]
    books = {
        "shadow": _book(trader.shadow, currency),
        "broker": _book(trader.broker, currency),
        "currency": currency,
        "reconciliation_breaks_today": trader.breaks_today,
        "open_positions": trader.open_positions,
    }
    return {"sleeves": sleeves, "books": books}


def build(reading: Reading, *, phases: Mapping[str, str], version: str | None) -> dict[str, object]:
    """The snapshot for one reading. Pure: the same reading, phases and version give the same snapshot."""
    as_of = stamp(reading.at_ns)
    snapshot: dict[str, object] = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "run_id": f"{as_of.replace('-', '').replace(':', '')}-{version or 'unversioned'}",
        "as_of": as_of,
        "generated_at": as_of,
        "version": version,
        "expected_windows": [
            {"session": week.session, "start": stamp(week.start_ns), "end": stamp(week.end_ns)}
            for week in reading.weeks
        ],
        "desk": {
            "mode": "paper",
            "stage": stage(reading),
            "venue": VENUE,
            "market_open": reading.market_open,
            "kill": reading.kill,
            "flatten": reading.flatten,
        },
        "phases": [{"id": name, "state": phases[name]} for name in PHASES],
        "health": {
            "level": reading.level,
            "reasons": sorted({problem.code for problem in reading.problems})[:MAX_REASONS],
        },
    }
    recorder = reading.recorder
    if recorder is not None and recorder.venue == VENUE:
        snapshot["recorder"] = {
            "state": recorder.state,
            "events_today": recorder.records_today,
            "last_event_at": stamp(recorder.last_record_ns) if recorder.last_record_ns else None,
            "gaps_unexplained_today": recorder.gaps_unexplained_today,
            "disk_used_mb": round(recorder.used_bytes / 1_000_000, 1),
        }
    if reading.trader is not None:
        snapshot.update(trader_parts(reading.trader))
    learning = reading.learning
    if learning is not None:

        def when(at: int | None) -> str | None:
            return stamp(at) if at is not None else None

        snapshot["learning"] = {
            "champion": learning.champion,
            "challengers": learning.challengers,
            "last_training_at": when(learning.last_training_ns),
            "last_promotion_at": when(learning.last_promotion_ns),
            "last_rollback_at": when(learning.last_rollback_ns),
        }
    return snapshot
