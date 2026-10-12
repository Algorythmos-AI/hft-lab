"""The trader's settings, read from config/trader.yaml by a strict loader.

A key the file does not define is refused, a number is a whole number, and nothing that decides whether a
sleeve may place a paper order has a default: a promotion rule with its number or its decision record left
null is a rule that is never met (hftlab.trader.stages).

What an order may be, what it costs in the shadow book and when the market is open are not here. They are
config/risk.yaml, config/cost_model.yaml and config/calendar/, each read by the loader that owns it.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.core import InstrumentKind
from hftlab.core._checks import pair as check_pair
from hftlab.core.ids import check_sleeve
from hftlab.lineup.journal import LineupError
from hftlab.lineup.serve import places
from hftlab.trader.stages import Rollout, Rule, StageError

MS: Final = 1_000_000
SECOND: Final = 1_000 * MS
MINUTE: Final = 60 * SECOND
_TOP: Final = frozenset(
    {
        "status", "state_dir", "venue", "seed", "pairs", "poll_ms", "beat_ms", "reconcile_seconds",
        "close_retry_seconds", "fsync", "sleeves", "rollout", "clean_day_minutes",
    }
)  # fmt: skip
# `registry`: where models and reports are kept, needed once a sleeve is of kind `ml`. `tape`: the size of a part
# of the event tape, the quota all tapes are counted against, the share of it at which entries stop, and how
# long the closing at the hard limit may take before the owner is paged (hftlab.trader.tape, hftlab.trader.room).
_OPTIONAL: Final = frozenset({"registry", "tape"})
_TAPE: Final = frozenset({"part_bytes", "quota_bytes"})
_TAPE_OPTIONAL: Final = frozenset({"soft_percent", "flatten_seconds"})
PART_BYTES: Final = 64_000_000  # ENGINEERING DEFAULT, where the file gives no `tape`
SOFT_PERCENT: Final = 90  # ENGINEERING DEFAULT, where the block gives no `soft_percent`
FLATTEN_SECONDS: Final = 300  # ENGINEERING DEFAULT, where the block gives no `flatten_seconds`
_LADDER: Final = frozenset({"start", "promote"})
_STEPS: Final = frozenset({"min_size", "normal"})
_RULE: Final = frozenset({"clean_days", "decision"})


class TraderConfigError(ValueError):
    """The trader's settings are missing something, or hold something the file does not define."""


@dataclass(frozen=True, slots=True)
class Settings:
    state_dir: Path
    venue: str
    seed: int
    pairs: tuple[tuple[str, InstrumentKind], ...]
    poll_ns: int
    beat_ns: int
    reconcile_ns: int  # how often the reconciler compares the venue with the broker book
    close_retry_ns: int  # the least time between two closes the trader itself proposes for one line
    fsync: bool
    sleeves: tuple[Mapping[str, Any], ...]  # as written; `hftlab.shadow` knows how to build them
    rollout: Mapping[str, Rollout]
    clean_day_ns: int | None  # how long the trader must have run in a trading day for it to count; null: none counts
    placeholder: bool
    registry: Path | None = None  # the folder with models/ and reports/, each file named by its hash (ADR 0023)
    tape_part_bytes: int = PART_BYTES  # a part of the tape is closed at the first turn after it has grown to this
    tape_quota_bytes: int | None = None  # all tapes together stay under this; None: the file sets no quota
    tape_soft_percent: int = SOFT_PERCENT  # at this share of the quota nothing more is opened: the soft limit
    tape_flatten_ns: int = FLATTEN_SECONDS * SECOND  # how long the closing at the hard limit may take, then a page

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(str(entry["name"]) for entry in self.sleeves)


def _whole(loaded: Mapping[str, Any], key: str, *, least: int = 1) -> int:
    value = loaded.get(key)
    if type(value) is not int or value < least:
        raise TraderConfigError(f"{key} must be a whole number of at least {least}, not {value!r}")
    return value


def _keys(value: object, where: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TraderConfigError(f"{where} must have exactly the keys {', '.join(sorted(keys))}")
    return value


def _pair(entry: object) -> tuple[str, InstrumentKind]:
    if not isinstance(entry, dict) or set(entry) != {"pair", "form"} or entry["form"] not in ("cash", "cfd"):
        raise TraderConfigError(f"a pair is written {{pair: EUR.USD, form: cash}} with form cash or cfd, not {entry!r}")
    try:
        return check_pair(entry["pair"]), InstrumentKind(entry["form"].upper())
    except ValueError as exc:
        raise TraderConfigError(str(exc)) from exc


def _rule(value: object, where: str) -> Rule:
    block = _keys(value, where, _RULE)
    try:
        return Rule(block["clean_days"], block["decision"])
    except StageError as exc:
        raise TraderConfigError(f"{where}: {exc}") from exc


def parse(loaded: object, source: str = "the trader's settings") -> Settings:
    if not isinstance(loaded, dict):
        raise TraderConfigError(f"{source} must be a mapping")
    unknown, missing = sorted(map(str, set(loaded) - _TOP - _OPTIONAL)), sorted(_TOP - set(loaded))
    if unknown or missing:
        raise TraderConfigError(f"{source}: not keys of this file: {unknown}; missing: {missing}")
    if loaded["status"] not in ("placeholder", "decided"):
        raise TraderConfigError(f"{source}: status must be placeholder or decided")
    if not isinstance(loaded["state_dir"], str) or not isinstance(loaded["venue"], str):
        raise TraderConfigError(f"{source}: state_dir and venue must be text")
    if type(loaded["fsync"]) is not bool:
        raise TraderConfigError(f"{source}: fsync must be true or false")
    pairs = tuple(_pair(entry) for entry in loaded["pairs"] or [])
    if not pairs:
        raise TraderConfigError(f"{source}: `pairs` must name at least one pair")
    sleeves = loaded["sleeves"]
    if not isinstance(sleeves, list) or not sleeves or not all(isinstance(entry, dict) for entry in sleeves):
        raise TraderConfigError(f"{source}: `sleeves` must list at least one sleeve")
    names = []
    for entry in sleeves:
        try:
            names.append(check_sleeve(entry.get("name")))
        except ValueError as exc:
            raise TraderConfigError(f"{source}: {exc}") from exc
    if "float" in names:
        raise TraderConfigError(f"{source}: `float` is the book's name for the account's float, not a sleeve")
    ladders = loaded["rollout"]
    if not isinstance(ladders, dict) or sorted(ladders) != sorted(names) or len(set(names)) != len(names):
        raise TraderConfigError(f"{source}: `rollout` must have one entry for each sleeve, and each sleeve one name")
    rollout: dict[str, Rollout] = {}
    for name in names:
        where = f"rollout.{name}"
        ladder = _keys(ladders[name], where, _LADDER)
        steps = _keys(ladder["promote"], f"{where}.promote", _STEPS)
        try:
            rollout[name] = Rollout(
                str(ladder["start"]),
                _rule(steps["min_size"], f"{where}.promote.min_size"),
                _rule(steps["normal"], f"{where}.promote.normal"),
            )
        except StageError as exc:
            raise TraderConfigError(f"{where}: {exc}") from exc
    registry = loaded.get("registry")
    if registry is not None and not isinstance(registry, str):
        raise TraderConfigError(f"{source}: registry must be a folder")
    if registry is None and any(entry.get("kind") == "ml" for entry in sleeves):
        raise TraderConfigError(f"{source}: a sleeve of kind ml needs `registry`, the folder its models are kept in")
    try:
        places(sleeves)
    except LineupError as exc:
        raise TraderConfigError(f"{source}: {exc}") from exc
    tape = loaded.get("tape")
    part_bytes, quota_bytes, soft_percent, flatten_seconds = PART_BYTES, None, SOFT_PERCENT, FLATTEN_SECONDS
    if tape is not None:
        if not isinstance(tape, dict) or not _TAPE <= set(tape) <= _TAPE | _TAPE_OPTIONAL:
            listed = f"{', '.join(sorted(_TAPE))} and, if wanted, {', '.join(sorted(_TAPE_OPTIONAL))}"
            raise TraderConfigError(f"{source}: tape must have the keys {listed}, and no other")
        block = {"soft_percent": SOFT_PERCENT, "flatten_seconds": FLATTEN_SECONDS, **tape}
        part_bytes, quota_bytes = _whole(block, "part_bytes"), _whole(block, "quota_bytes")
        soft_percent, flatten_seconds = _whole(block, "soft_percent"), _whole(block, "flatten_seconds")
        if quota_bytes < part_bytes:
            raise TraderConfigError(f"{source}: tape.quota_bytes must be at least one part, {part_bytes}")
        if soft_percent > 99:
            raise TraderConfigError(f"{source}: tape.soft_percent is a share of the quota below all of it: 1 to 99")
    minutes = loaded["clean_day_minutes"]
    if minutes is not None and (type(minutes) is not int or minutes < 1):
        raise TraderConfigError(f"{source}: clean_day_minutes must be null or a whole number of at least 1")
    return Settings(
        state_dir=Path(loaded["state_dir"]),
        venue=loaded["venue"],
        seed=_whole(loaded, "seed", least=0),
        pairs=pairs,
        poll_ns=_whole(loaded, "poll_ms") * MS,
        beat_ns=_whole(loaded, "beat_ms") * MS,
        reconcile_ns=_whole(loaded, "reconcile_seconds") * SECOND,
        close_retry_ns=_whole(loaded, "close_retry_seconds") * SECOND,
        fsync=loaded["fsync"],
        sleeves=tuple(dict(entry) for entry in sleeves),
        rollout=rollout,
        clean_day_ns=None if minutes is None else minutes * MINUTE,
        placeholder=loaded["status"] != "decided",
        registry=None if registry is None else Path(registry),
        tape_part_bytes=part_bytes,
        tape_quota_bytes=quota_bytes,
        tape_soft_percent=soft_percent,
        tape_flatten_ns=flatten_seconds * SECOND,
    )


def load(path: Path) -> Settings:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TraderConfigError(f"cannot read {path}: {exc}") from exc
    return parse(loaded, str(path))


def digest(paths: Sequence[Path]) -> str:
    """One hash of the files a session's decisions depend on: a replay under other settings is refused by it."""
    whole = hashlib.sha256()
    for path in sorted(paths):
        data = path.read_bytes() if path.is_file() else b""
        whole.update(f"{path.name}:{len(data)}:".encode() + data)
    return whole.hexdigest()
