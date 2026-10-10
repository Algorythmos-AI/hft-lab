"""The global trial count, read from the append-only registry config/trial_registry.yaml.

Every configuration evaluated on development data is a trial, whatever experiment ran it, and the count
deflates every later performance statistic. Prose in decision records cannot be read reliably by code, so the
YAML file holds the one number code uses; the registry lint ties each row to a decision that exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

REGISTRY: Final = Path(__file__).resolve().parents[3] / "config" / "trial_registry.yaml"
STATUSES: Final = ("accepted", "proposed")
_DECISION = re.compile(r"HFT-DEC-[0-9]{4}")


@dataclass(frozen=True, slots=True)
class TrialRow:
    decision: str
    total: int
    status: str


def trial_history(path: Path | None = None) -> list[TrialRow]:
    """The registry rows, validated: a decision id, a known status, integer totals that never decrease."""
    loaded: Any = yaml.safe_load((path or REGISTRY).read_text(encoding="utf-8"))
    raw = loaded.get("history") if isinstance(loaded, dict) else None
    if not isinstance(raw, list):
        raise ValueError("the trial registry needs a `history` list (it may be empty)")
    rows: list[TrialRow] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"decision", "total", "status"}:
            raise ValueError(f"bad trial registry row (needs decision, total, status): {item!r}")
        decision, total, status = item["decision"], item["total"], item["status"]
        if not isinstance(decision, str) or not _DECISION.fullmatch(decision):
            raise ValueError(f"bad trial registry row: {decision!r} is not an HFT-DEC-NNNN id")
        if type(total) is not int or total < 0:
            raise ValueError(f"bad trial registry row: total {total!r} is not a non-negative integer")
        if status not in STATUSES:
            raise ValueError(f"bad trial registry row: status {status!r} is not one of {STATUSES}")
        if rows and total < rows[-1].total:
            raise ValueError(f"trial registry total decreases at {decision}: {total} < {rows[-1].total}")
        rows.append(TrialRow(decision, total, status))
    return rows


def global_trial_count(path: Path | None = None) -> int:
    """The trial count in force: the newest accepted row, or 0 before any trial has been run."""
    accepted = [row for row in trial_history(path) if row.status == "accepted"]
    return accepted[-1].total if accepted else 0
