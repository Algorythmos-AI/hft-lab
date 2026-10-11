"""The learning loop's settings, read from config/learn.yaml by a strict loader (ADR 0023).

A key the file does not define is refused and a number is a whole number. What decides how a result is priced
or whether a model may be promoted has no default: the wait and the worst case of a stuck exit must be
written, and a promotion rule or a drift threshold left null is one that is not in force.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.core._checks import pair as check_pair
from hftlab.features import FeatureConfig
from hftlab.journal import canonical
from hftlab.learn.promote import Rule

MS: Final = 1_000_000
SECOND: Final = 1_000 * MS
MB: Final = 1_000_000
_TOP: Final = frozenset(
    {
        "version", "status", "state_dir", "lineup", "pairs", "horizons_seconds", "features", "label", "ridge_penalty",
        "validation", "memory_budget_mb", "promotion", "drift", "scorer", "model", "boosting",
    }
)  # fmt: skip
_FEATURES: Final = frozenset({"lookbacks_ms", "rate_window_ms", "range_window_ms", "stale_after_ms"})
_LABEL: Final = frozenset({"exit_wait_seconds", "stuck_increments"})
_VALIDATION: Final = frozenset(
    {"min_train_weeks", "hold_out_weeks", "seed", "bootstrap_draws", "level_ppm", "leak_checks", "summary_bins"}
)
_RULE: Final = ("decision", "min_trades", "min_days", "max_drawdown_micro", "draws", "level_ppm", "seed")
_PROMOTION: Final = frozenset({*_RULE, "baseline_sleeve"})
_DRIFT: Final = frozenset({"min_rows", "feature_shift_ppm", "calibration_gap_micro", "spread_ratio_x100"})
_SCORER: Final = frozenset({"timeout_ms", "restart_first_ms", "restart_last_ms"})
RIDGE: Final = "ridge"
GBM: Final = "gbm"
MODELS: Final = (RIDGE, GBM)
# The most a boosted model may be, whatever the file says: it is fitted on a small shared host and scored in the
# trader's loop (ADR 0025). The model file's own limits (`hftlab.models.trees`) are no tighter than these.
MOST_TREES: Final = 500
MOST_DEPTH: Final = 6
MOST_BINS: Final = 255
_SLEEVE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
NOT_IN_FORCE: Final = Rule(None, 1, 1, 0, 1, 500_000, 0)  # names no decision record, so it can pass nothing


class LearnConfigError(ValueError):
    """The learning settings are missing something, or hold something the file does not define."""


@dataclass(frozen=True, slots=True)
class Drift:
    """The drift thresholds. One left null is a monitor that is computed and reported, and steps nothing down."""

    min_rows: int | None
    feature_shift_ppm: int | None  # the population stability index of any one feature, in millionths
    calibration_gap_micro: int | None  # predicted less realised outcome per trade, over a bucket
    spread_ratio_x100: int | None  # the live median spread against the training median, in hundredths

    @property
    def in_force(self) -> bool:
        return None not in (self.min_rows, self.feature_shift_ppm, self.calibration_gap_micro, self.spread_ratio_x100)


@dataclass(frozen=True, slots=True)
class Boosting:
    """The numbers a gradient-boosted model is fitted with (ADR 0025). Fixed in the file: nothing searches them."""

    trees: int  # per target
    max_depth: int
    leaves: int  # per tree, at most
    learning_rate_ppm: int  # how much of each tree is added, in millionths
    min_leaf_rows: int  # a leaf holds at least this many training rows
    bins: int  # the values of a feature are grouped into at most this many before a split is looked for
    l2_ppm: int  # the penalty on a leaf's value, in millionths
    leaf_scale: int  # a leaf is kept as a whole number of 1 / leaf_scale millionths of the quote currency
    seed: int

    def __post_init__(self) -> None:
        if not (1 <= self.trees <= MOST_TREES and 1 <= self.max_depth <= MOST_DEPTH and 2 <= self.bins <= MOST_BINS):
            raise ValueError(
                f"boosting: at most {MOST_TREES} trees, a depth of at most {MOST_DEPTH} and 2 to {MOST_BINS} bins"
            )
        if not 2 <= self.leaves <= 2**self.max_depth:
            raise ValueError("boosting: a tree of this depth has between 2 leaves and 2 to the power of its depth")
        if not 0 < self.learning_rate_ppm <= 1_000_000 or not 1 <= self.leaf_scale <= 1_000_000:
            raise ValueError("boosting: a learning rate of 1 to 1,000,000 ppm and a leaf scale of 1 to 1,000,000")

    def as_dict(self) -> dict[str, int]:
        return {
            "trees": self.trees, "max_depth": self.max_depth, "leaves": self.leaves,
            "learning_rate_ppm": self.learning_rate_ppm, "min_leaf_rows": self.min_leaf_rows, "bins": self.bins,
            "l2_ppm": self.l2_ppm, "leaf_scale": self.leaf_scale, "seed": self.seed,
        }  # fmt: skip


_BOOSTING: Final = frozenset(field.name for field in fields(Boosting))


@dataclass(frozen=True, slots=True)
class Settings:
    state_dir: Path
    lineup: Path
    pairs: tuple[str, ...]
    horizons_ns: tuple[int, ...]
    features: FeatureConfig
    exit_wait_ns: int
    stuck_increments: int
    penalty: Fraction
    model: str  # the kind of model a run fits: `ridge` or `gbm`
    boosting: Boosting  # read by a run whose model is `gbm`, and by nothing else
    min_train_weeks: int
    hold_out_weeks: int
    seed: int
    draws: int
    level_ppm: int
    leak_checks: int
    summary_bins: int
    memory_budget_bytes: int
    rule: Rule  # `NOT_IN_FORCE` while any number of the promotion rule is null
    rule_in_force: bool  # every number is written AND a decision record is named
    baseline_sleeve: str
    drift: Drift
    scorer_timeout_ns: int
    scorer_restart_first_ns: int
    scorer_restart_last_ns: int
    placeholder: bool

    @property
    def models(self) -> Path:
        return self.state_dir / "models"

    @property
    def reports(self) -> Path:
        return self.state_dir / "reports"

    @property
    def ledger(self) -> Path:
        return self.state_dir / "trials.jsonl"

    def frozen(self, pair: str, horizon_ns: int, cost_model: dict[str, object]) -> dict[str, object]:
        """Everything an evaluation of one pair and horizon depends on, as plain values: what a trial is counted by."""
        features = self.features
        # What a kind of model is fitted with is part of its trial and of no other kind's: changing a number
        # the run does not read makes no new configuration.
        fitted: dict[str, object] = (
            {"penalty": str(self.penalty)} if self.model == RIDGE else {"boosting": self.boosting.as_dict()}
        )
        return {
            "kind": self.model,
            "pair": pair,
            "horizon_ns": horizon_ns,
            "horizons_ns": list(self.horizons_ns),
            "features": {
                "lookbacks_ns": list(features.lookbacks_ns),
                "rate_window_ns": features.rate_window_ns,
                "range_window_ns": features.range_window_ns,
                "stale_after_ns": features.stale_after_ns,
            },
            "exit_wait_ns": self.exit_wait_ns,
            "stuck_increments": self.stuck_increments,
            **fitted,
            "min_train_weeks": self.min_train_weeks,
            "hold_out_weeks": self.hold_out_weeks,
            "seed": self.seed,
            "draws": self.draws,
            "level_ppm": self.level_ppm,
            "cost_model": cost_model,
        }


def config_hash(frozen: dict[str, object]) -> str:
    return hashlib.sha256(canonical(frozen).encode("ascii")).hexdigest()


def _block(loaded: dict[str, Any], key: str, keys: frozenset[str]) -> dict[str, Any]:
    value = loaded.get(key)
    if not isinstance(value, dict) or set(value) != keys:
        raise LearnConfigError(f"{key} must have exactly the keys {', '.join(sorted(keys))}")
    return value


def _whole(block: dict[str, Any], key: str, *, least: int = 1) -> int:
    value = block.get(key)
    if type(value) is not int or value < least:
        raise LearnConfigError(f"{key} must be a whole number of at least {least}, not {value!r}")
    return value


def _or_null(block: dict[str, Any], key: str, *, least: int = 1) -> int | None:
    return None if block.get(key) is None else _whole(block, key, least=least)


def _rule(block: dict[str, Any]) -> Rule | None:
    """The promotion rule, when every number of it is written. A rule with a null number in it is no rule.

    Every number written and `decision` null is a rule that measures and is not in force: a week is weighed
    with its numbers, for a report, and it names no decision record, so it can pass nothing.
    """
    if block["decision"] is not None and not isinstance(block["decision"], str):
        raise LearnConfigError("promotion.decision must be null or a decision record id")
    if any(block[key] is None for key in _RULE[1:]):
        for key in _RULE[1:]:
            _or_null(block, key, least=0)
        return None
    try:
        return Rule(
            block["decision"], _whole(block, "min_trades"), _whole(block, "min_days"),
            _whole(block, "max_drawdown_micro", least=0), _whole(block, "draws"), _whole(block, "level_ppm"),
            _whole(block, "seed", least=0),
        )  # fmt: skip
    except ValueError as exc:
        raise LearnConfigError(f"promotion: {exc}") from exc


def parse(loaded: object, source: str = "the learning settings") -> Settings:
    if not isinstance(loaded, dict):
        raise LearnConfigError(f"{source} must be a mapping")
    unknown, missing = sorted(map(str, set(loaded) - _TOP)), sorted(_TOP - set(loaded))
    if unknown or missing or loaded["version"] != 1:
        raise LearnConfigError(f"{source}: version 1 only; not keys of this file: {unknown}; missing: {missing}")
    if loaded["status"] not in ("placeholder", "decided"):
        raise LearnConfigError(f"{source}: status must be placeholder or decided")
    if not isinstance(loaded["state_dir"], str) or not isinstance(loaded["lineup"], str):
        raise LearnConfigError(f"{source}: state_dir and lineup must be text")
    pairs, horizons = loaded["pairs"], loaded["horizons_seconds"]
    if not isinstance(pairs, list) or not pairs or not isinstance(horizons, list) or not horizons:
        raise LearnConfigError(f"{source}: at least one pair and one horizon")
    if any(type(h) is not int or h < 1 for h in horizons) or horizons != sorted(set(horizons)):
        raise LearnConfigError(f"{source}: horizons_seconds are whole seconds, each once, shortest first")
    features, label = _block(loaded, "features", _FEATURES), _block(loaded, "label", _LABEL)
    validation, promotion = _block(loaded, "validation", _VALIDATION), _block(loaded, "promotion", _PROMOTION)
    drift, scorer = _block(loaded, "drift", _DRIFT), _block(loaded, "scorer", _SCORER)
    boosting = _block(loaded, "boosting", _BOOSTING)
    if loaded["model"] not in MODELS:
        raise LearnConfigError(f"{source}: model is one of {', '.join(MODELS)}, not {loaded['model']!r}")
    lookbacks = features["lookbacks_ms"]
    if not isinstance(lookbacks, list) or any(type(value) is not int for value in lookbacks):
        raise LearnConfigError(f"{source}: features.lookbacks_ms is a list of whole milliseconds")
    baseline = promotion["baseline_sleeve"]
    if not isinstance(baseline, str) or not _SLEEVE.fullmatch(baseline):
        raise LearnConfigError(f"{source}: promotion.baseline_sleeve names the sleeve a first challenger must beat")
    try:
        windows = FeatureConfig(
            tuple(value * MS for value in lookbacks), _whole(features, "rate_window_ms") * MS,
            _whole(features, "range_window_ms") * MS, _whole(features, "stale_after_ms") * MS,
        )  # fmt: skip
        names = tuple(check_pair(pair) for pair in pairs)
        penalty = Fraction(loaded["ridge_penalty"]) if isinstance(loaded["ridge_penalty"], str) else None
        boosted = Boosting(
            _whole(boosting, "trees"), _whole(boosting, "max_depth"), _whole(boosting, "leaves", least=2),
            _whole(boosting, "learning_rate_ppm"), _whole(boosting, "min_leaf_rows"), _whole(boosting, "bins", least=2),
            _whole(boosting, "l2_ppm", least=0), _whole(boosting, "leaf_scale"), _whole(boosting, "seed", least=0),
        )  # fmt: skip
    except (ValueError, ZeroDivisionError) as exc:
        raise LearnConfigError(f"{source}: {exc}") from exc
    if penalty is None or penalty <= 0:
        raise LearnConfigError(f"{source}: ridge_penalty is a quoted fraction above zero, such as '1' or '1/2'")
    level = _whole(validation, "level_ppm")
    if not 0 < level < 1_000_000 or len(set(names)) != len(names):
        raise LearnConfigError(f"{source}: a level between 0 and 1,000,000 ppm, and each pair once")
    rule = _rule(promotion)
    first, last = _whole(scorer, "restart_first_ms"), _whole(scorer, "restart_last_ms")
    if last < first:
        raise LearnConfigError(f"{source}: scorer.restart_last_ms is not below restart_first_ms")
    return Settings(
        state_dir=Path(loaded["state_dir"]),
        lineup=Path(loaded["lineup"]),
        pairs=names,
        horizons_ns=tuple(h * SECOND for h in horizons),
        features=windows,
        exit_wait_ns=_whole(label, "exit_wait_seconds", least=0) * SECOND,
        stuck_increments=_whole(label, "stuck_increments", least=0),
        penalty=penalty,
        model=loaded["model"],
        boosting=boosted,
        min_train_weeks=_whole(validation, "min_train_weeks"),
        hold_out_weeks=_whole(validation, "hold_out_weeks", least=0),
        seed=_whole(validation, "seed", least=0),
        draws=_whole(validation, "bootstrap_draws"),
        level_ppm=level,
        leak_checks=_whole(validation, "leak_checks"),
        summary_bins=_whole(validation, "summary_bins", least=2),
        memory_budget_bytes=_whole(loaded, "memory_budget_mb") * MB,
        rule=rule or NOT_IN_FORCE,
        rule_in_force=rule is not None and rule.decision is not None,
        baseline_sleeve=baseline,
        drift=Drift(
            _or_null(drift, "min_rows"), _or_null(drift, "feature_shift_ppm"),
            _or_null(drift, "calibration_gap_micro"), _or_null(drift, "spread_ratio_x100", least=101),
        ),
        scorer_timeout_ns=_whole(scorer, "timeout_ms") * MS,
        scorer_restart_first_ns=first * MS,
        scorer_restart_last_ns=last * MS,
        placeholder=loaded["status"] != "decided",
    )


def load(path: Path) -> Settings:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LearnConfigError(f"cannot read {path}: {exc}") from exc
    return parse(loaded, str(path))
