"""What the capability file says the account may open, read for the trader (config/venues/<venue>.yaml).

The capability probe writes down what the broker did with an order that would open a position from flat, per
pair and per side (`spot_position_opening`). A sleeve enters long or short, so it needs both sides of every
pair it trades. This module answers one question: for these pairs, in these forms, which can the account not
be shown to open on both sides, and why. Anything but `accepted` on a side is a reason, a side never tested
among them: a stage above shadow is earned on what was measured, not on what was not refused.

It reads a permission, never a cost or a timing, so it reads the file whatever its `status`. No network, no
broker library: a file and nothing else.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.core import InstrumentKind

ACCEPTED: Final = "accepted"
SIDES: Final = ("buy", "sell")  # a sleeve enters long or short: both are needed
FOLDER: Final = Path("config/venues")


class CapabilityError(ValueError):
    """The capability file is missing or cannot be read as one."""


def path_for(venue: str, folder: Path = FOLDER) -> Path:
    """The capability file of a venue: `ibkr_paper` is described by ibkr.yaml."""
    return folder / f"{venue.removesuffix('_paper')}.yaml"


def load(path: Path) -> Mapping[str, Any]:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CapabilityError(f"cannot read the capability file {path}: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("pairs"), list):
        raise CapabilityError(f"{path} is not a capability file: it lists no pairs")
    return loaded


def _why(entry: object, kind: InstrumentKind) -> str | None:
    """Why this pair cannot be shown to open on both sides in this form, or None: both sides were accepted."""
    if not isinstance(entry, dict):
        return "the capability file does not describe the pair"
    if kind is not InstrumentKind.CASH:
        return f"nothing is measured about opening a position as {kind.value}"
    opening = entry.get("spot_position_opening")
    if not isinstance(opening, dict):
        return "nothing is measured about opening a spot position"
    found = []
    for side in SIDES:
        verdict, refusal = opening.get(side), opening.get(f"{side}_refusal")
        if verdict == ACCEPTED:
            continue
        if verdict is None:
            found.append(f"{side} not tested")
        else:
            found.append(f"{side} {verdict}" + (f" ({refusal})" if isinstance(refusal, str) else ""))
    return ", ".join(found) or None


def cannot_open(capability: Mapping[str, Any], pairs: Iterable[tuple[str, InstrumentKind]]) -> dict[str, str]:
    """pair -> why the account cannot be shown to open a position in it on both sides. Empty: it can, in all."""
    described = {entry.get("pair"): entry for entry in capability["pairs"] if isinstance(entry, dict)}
    record = capability.get("probe_record")
    cited = f", by {record}" if isinstance(record, str) else ""
    out = {}
    for pair, kind in pairs:
        why = _why(described.get(pair), kind)
        if why is not None:
            out[pair] = f"{why}{cited}"
    return out


def said(blocked: Mapping[str, str]) -> str:
    """One line for a refusal or a journal: every pair and its reason."""
    return "; ".join(f"{pair}: {why}" for pair, why in sorted(blocked.items()))
