"""From the lineup to sleeves: the models the trader serves, loaded by hash and by nothing else (ADR 0023).

The trader's configuration names places:

    - {name: ml, kind: ml, slot: champion, margin_micro: 0, retry_seconds: 10, scorer: in_process}
    - {name: ml-challenger-1, kind: ml, slot: challenger-1, margin_micro: 0, retry_seconds: 10, scorer: in_process}

`scorer` says where a row is scored: `in_process`, in the trader's own process, or `process`, by a child the
trader talks to over a pipe (ADR 0023, section 5). A place that says `process` is handed its scorer by whoever
runs the session (`scoring`); with nobody to hand one over the place is refused rather than quietly served in
process. A replay hands over in-process scorers that abstain where the day's child failed, so a recorded day
never needs a child.

Which model stands in a place is the lineup journal's to say. For each place with a model, the evaluation
report and the model file are read from the registry under their SHA-256, and each is refused unless its bytes
hash to its name and the two agree with one another. The feature windows and the feasibility map the sleeve
needs are the report's: they are the ones the model was evaluated with. A place the lineup leaves empty gets an
idle sleeve under the place's name, so the books, the stages and the journals keep one name per place. So does
a place whose model cannot be served; the reason is handed back, and no other sleeve waits for it.

Nothing here places an order or holds a venue. A sleeve is a pure function of what it is shown (ADR 0014).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from hftlab.calendar import Calendar
from hftlab.features import FeatureBuilder, FeatureConfig
from hftlab.lineup.journal import Lineup, LineupError, Named, read
from hftlab.models import Model, ModelError, Scorer, parse, scorer_for
from hftlab.scoring.client import ScorerRefused
from hftlab.strategy import IdleSleeve, Sleeve
from hftlab.strategy.ml import MlSleeve

ML: Final = "ml"
CHAMPION: Final = "champion"
IN_PROCESS: Final = "in_process"
PROCESS: Final = "process"
Scoring = Callable[[str, Model, Path], Scorer]  # (the sleeve, its model, the folder of model files) -> its scorer
SECOND: Final = 1_000_000_000
_KEYS: Final = frozenset({"name", "kind", "slot", "margin_micro", "retry_seconds", "scorer"})


def slot_of(entry: Mapping[str, Any]) -> tuple[str, int]:
    """A place as (`champion`, 0) or (`challenger`, its number from 0)."""
    if set(entry) != _KEYS:
        raise LineupError(f"an ml sleeve has exactly the keys {', '.join(sorted(_KEYS))}, not {sorted(entry)}")
    slot, numbers = entry["slot"], (entry["margin_micro"], entry["retry_seconds"])
    if any(type(number) is not int or number < 0 for number in numbers):
        raise LineupError(f"sleeve {entry['name']}: margin_micro and retry_seconds are two whole numbers")
    if entry["scorer"] not in (IN_PROCESS, PROCESS):
        raise LineupError(f"sleeve {entry['name']}: the scorer is {IN_PROCESS} or {PROCESS}, not {entry['scorer']!r}")
    if slot == CHAMPION:
        return CHAMPION, 0
    head, _, number = str(slot).partition("-")
    if head != "challenger" or not number.isdecimal() or int(number) < 1:
        raise LineupError(f"sleeve {entry['name']}: a slot is champion or challenger-N, not {slot!r}")
    return "challenger", int(number) - 1


def places(entries: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, int]]:
    """Every ml sleeve's place, by sleeve name. Each place is held by one sleeve."""
    found = {str(entry["name"]): slot_of(entry) for entry in entries if entry.get("kind") == ML}
    if len(set(found.values())) != len(found):
        raise LineupError("two sleeves are configured for the same place of the lineup")
    return found


def standing(lineup: Lineup, place: tuple[str, int]) -> Named | None:
    """The model the lineup puts in a place, or None for an empty one."""
    kind, number = place
    if kind == CHAMPION:
        return lineup.champion
    return lineup.challengers[number] if number < len(lineup.challengers) else None


def _pinned(path: Path, digest: str, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LineupError(f"the lineup names {what} {digest[:12]}, and {path} cannot be read: {exc}") from exc


def load(named: Named, registry: Path) -> tuple[Model, dict[str, Any], Path]:
    """The model and its report, each read under its hash and checked against it and against the other."""
    raw = _pinned(registry / "reports" / f"{named.report}.json", named.report, "report")
    if hashlib.sha256(raw).hexdigest() != named.report:
        raise LineupError(f"reports/{named.report}.json is not the report it is named for")
    path = registry / "models" / f"{named.model}.json"
    try:
        report = json.loads(raw)
        model = parse(_pinned(path, named.model, "model"))
    except (ValueError, ModelError) as exc:
        raise LineupError(f"model {named.model[:12]} or its report cannot be read: {exc}") from exc
    if model.content_hash != named.model:
        raise LineupError(f"{path.name} holds model {model.content_hash[:12]}, not the one it is named for")
    said = report.get("model") if isinstance(report, dict) else None
    if not isinstance(said, dict) or said.get("hash") != named.model or report.get("pair") != model.pair:
        raise LineupError(f"report {named.report[:12]} is not the evaluation of model {named.model[:12]}")
    return model, report, path


def windows(report: Mapping[str, Any]) -> tuple[FeatureConfig, Mapping[str, object]]:
    """The feature windows and the feasibility map a report says its model is served with."""
    serving = report.get("serving")
    features = serving.get("features") if isinstance(serving, dict) else None
    feasibility = serving.get("feasibility") if isinstance(serving, dict) else None
    if not isinstance(features, dict) or not isinstance(feasibility, dict):
        raise LineupError("the report does not say how its model is served")
    try:
        config = FeatureConfig(
            tuple(features["lookbacks_ns"]), features["rate_window_ns"], features["range_window_ns"],
            features["stale_after_ns"],
        )  # fmt: skip
    except (KeyError, TypeError, ValueError) as exc:
        raise LineupError(f"the report does not say how its model is served: {exc!r}") from exc
    return config, feasibility


@dataclass(frozen=True, slots=True)
class Served:
    """The sleeves of a session, and what each place of the lineup got."""

    sleeves: list[Sleeve[Any]]
    serving: dict[str, str | None]  # an ml sleeve -> the hash of the model it serves, or None for an idle place
    refused: dict[str, str]  # an ml sleeve whose model could not be served -> why; the place runs idle


def lineup_or_none(path: Path) -> tuple[Lineup, str]:
    """The lineup in force, or the empty one and why, when the journal cannot be used."""
    try:
        return read(path), ""
    except LineupError as exc:
        return Lineup(), str(exc)


def _scorer(entry: Mapping[str, Any], model: Model, registry: Path, scoring: Scoring | None) -> Scorer:
    """Where the place says its rows are scored. A scorer process that cannot serve the model is a refusal."""
    if entry["scorer"] == IN_PROCESS:
        return scorer_for(model)
    if scoring is None:
        raise LineupError(f"sleeve {entry['name']}: its scorer is a process, and this run was handed none")
    try:
        return scoring(str(entry["name"]), model, registry / "models")
    except ScorerRefused as exc:
        raise LineupError(f"sleeve {entry['name']}: {exc}") from exc


def _one(
    entry: Mapping[str, Any], named: Named, registry: Path | None, calendar: Calendar, scoring: Scoring | None
) -> Sleeve[Any]:
    name = str(entry["name"])
    if registry is None:
        raise LineupError(f"sleeve {name} has a model in the lineup and the settings name no registry")
    model, report, _ = load(named, registry)
    config, feasibility = windows(report)
    scorer = _scorer(entry, model, registry, scoring)
    try:
        return MlSleeve(
            name, scorer=scorer, features=FeatureBuilder(model.pair, config, calendar),
            feasibility=feasibility, margin_micro=entry["margin_micro"], retry_ns=entry["retry_seconds"] * SECOND,
        )  # fmt: skip
    except (ValueError, KeyError, TypeError) as exc:
        raise LineupError(f"sleeve {name}: {exc}") from exc


def make(
    entries: Sequence[Mapping[str, Any]], lineup: Lineup, *, registry: Path | None, calendar: Calendar,
    others: Callable[[Mapping[str, Any]], Sleeve[Any]], problem: str = "", scoring: Scoring | None = None,
) -> Served:  # fmt: skip
    """The sleeves in the order they are configured. An ml place gets the model the lineup puts there.

    A place whose model cannot be served (a file that is missing or is not what its name says, a lineup journal
    that does not verify: `problem`) runs idle and is listed with the reason. The other sleeves are not held
    up by it: the trader goes on, the place makes no entry, and the owner is paged by whoever called.
    """
    held = places(entries)
    sleeves: list[Sleeve[Any]] = []
    serving: dict[str, str | None] = {}
    refused: dict[str, str] = {}
    for entry in entries:
        name = str(entry.get("name"))
        if name not in held:
            sleeves.append(others(entry))
            continue
        named = None if problem else standing(lineup, held[name])
        made: Sleeve[Any] | None = None
        if problem:
            refused[name] = problem
        elif named is not None:
            try:
                made = _one(entry, named, registry, calendar, scoring)
            except LineupError as exc:
                refused[name] = str(exc)
        sleeves.append(made if made is not None else IdleSleeve(name))
        serving[name] = named.model if named is not None and made is not None else None
    return Served(sleeves, serving, refused)
