"""The risk gate's limits, read from config/risk.yaml.

The loader is strict because a limit that was mistyped is a limit that is not there:

  * a key the file does not define is refused, at every level, with its full name;
  * a value of the wrong kind is refused: whole numbers are `int` (never a float, never a bool), and money is
    quoted decimal text, converted exactly to millionths of its currency;
  * while `in_force` is false the file is checked for its shape and then refused as not in force, so nothing
    can be built from placeholders;
  * with `in_force` true every limit must have a value, the status must be `decided` and the decision record
    that set the values must be named. Real values need that record; none is invented here.

Money in different currencies is never added, so a loss latch is written per currency, and the file must set
one for the quote currency of every pair it configures, for the desk and for every sleeve.

An order that can only reduce a position has a collar of its own, `exit_collar_ticks`, which the loader refuses
when it is narrower than the pair's `price_collar_ticks`: wider, never absent (ADR 0015, addendum).

Two keys are kept and must stay null: `desk.max_gross_exposure` and `desk.max_leverage`. Both need a position
valued in the account currency, and nothing in the platform converts a currency. A value there would be a
limit nobody enforces, so the loader refuses one.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from hftlab.core import CASH_SCALE, to_scaled
from hftlab.core._checks import currency as check_currency
from hftlab.core._checks import pair as check_pair
from hftlab.core._checks import token as check_token
from hftlab.core.ids import check_sleeve

MS: Final = 1_000_000
SECOND: Final = 1_000 * MS
MINUTE: Final = 60 * SECOND
DECIDED: Final = "decided"
_DECISION = re.compile(r"HFT-DEC-[0-9]{4}")
_NOT_ENFORCED: Final = ("max_gross_exposure", "max_leverage")
_RESERVED: Final = ("desk", "unattributed")  # the book's names for the sum of sleeves and for fills nobody here ordered

_TOP: Final = frozenset(
    {"status", "in_force", "decision", "desk", "per_pair", "per_sleeve", "market_guards", "reconciliation", "switches"}
)
_DESK: Final = frozenset(
    {
        *_NOT_ENFORCED, "max_open_pairs", "max_orders_per_minute", "max_order_to_trade_ratio",
        "order_to_trade_window_seconds", "reject_streak_breaker", "loss_latches",
    }
)  # fmt: skip
_PAIR: Final = frozenset(
    {"max_position", "max_order_size", "price_collar_ticks", "exit_collar_ticks", "max_spread_ticks", "stale_quote_ms"}
)
_SLEEVE: Final = frozenset({"max_position", "max_orders_per_minute", "loss_latches"})
_LATCH: Final = frozenset({"daily_loss", "drawdown"})
_GUARDS: Final = frozenset(
    {"rollover_no_entry_minutes_before", "rollover_no_entry_minutes_after", "weekly_close_no_entry_minutes"}
)
_RECONCILIATION: Final = frozenset({"grace_seconds"})
_SWITCHES: Final = frozenset({"kill", "flatten", "flatten_retry_seconds"})
_SWITCH_FILES: Final = ("flatten", "kill")


class RiskConfigError(ValueError):
    """The risk file is missing something, holds something it does not define, or cannot be enforced as written."""


class RiskNotInForce(RiskConfigError):
    """The file is well formed and says its limits are not in force. Nothing may be built from it."""


@dataclass(frozen=True, slots=True)
class LossLatch:
    """The two loss limits of one scope in one currency, in millionths of that currency. Both are positive."""

    daily_loss_micro: int
    drawdown_micro: int


@dataclass(frozen=True, slots=True)
class PairLimits:
    max_position: int  # base-currency units, long or short; holds for the desk and for each sleeve
    max_order_size: int
    price_collar_ticks: int  # price increments from the mid
    exit_collar_ticks: int  # the same, for an order that can only reduce the sleeve's position; never the narrower
    max_spread_ticks: int  # price increments
    stale_quote_ns: int


@dataclass(frozen=True, slots=True)
class SleeveLimits:
    max_position: int  # the sleeve's own cap in any one pair; the pair's cap holds as well
    max_orders_per_minute: int
    loss_latches: Mapping[str, LossLatch]


@dataclass(frozen=True, slots=True)
class DeskLimits:
    max_open_pairs: int
    max_orders_per_minute: int
    max_order_to_trade_ratio: int
    order_to_trade_window_ns: int
    reject_streak_breaker: int
    loss_latches: Mapping[str, LossLatch]


@dataclass(frozen=True, slots=True)
class MarketGuards:
    rollover_before_ns: int
    rollover_after_ns: int
    weekly_close_no_entry_ns: int


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Limits that are in force. One exists only when every value was set and a decision record is named."""

    decision: str
    desk: DeskLimits
    pairs: Mapping[str, PairLimits]
    sleeves: Mapping[str, SleeveLimits]
    guards: MarketGuards
    reconciliation_grace_ns: int
    kill_switch: str
    flatten_switch: str
    flatten_retry_ns: int  # the least time between two closes proposed for a line whose last one was refused


class _Reader:
    """Reads values out of the loaded file and keeps every complaint, so one run names them all."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.nulls: list[str] = []

    def section(self, value: object, where: str, keys: Set[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.problems.append(f"{where} must be a mapping")
            return {}
        unknown = sorted(map(str, set(value) - keys))
        self.problems += [f"{where}.{key} is not a key this file defines" for key in unknown]
        self.problems += [f"{where}.{key} is missing" for key in sorted(keys - set(value))]
        return value

    def named(self, value: object, where: str) -> dict[str, Any]:
        """A mapping whose keys are names (pairs, sleeves, currencies) rather than a fixed list."""
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            self.problems.append(f"{where} must be a mapping of names")
            return {}
        return value

    def whole(self, section: Mapping[str, Any], where: str, key: str, *, least: int = 1) -> int:
        value = section.get(key)
        if value is None:
            self.nulls.append(f"{where}.{key}")
        elif type(value) is not int or value < least:
            self.problems.append(f"{where}.{key} must be a whole number of at least {least}, not {value!r}")
        else:
            return value
        return least

    def money(self, section: Mapping[str, Any], where: str, key: str) -> int:
        value = section.get(key)
        if value is None:
            self.nulls.append(f"{where}.{key}")
            return 1
        try:
            micro = to_scaled(value, CASH_SCALE) if isinstance(value, str) else 0  # plain digits: no exponent
        except ValueError:
            micro = 0
        if micro <= 0:
            self.problems.append(f"{where}.{key} must be a quoted positive decimal such as '50.00', not {value!r}")
        return max(micro, 1)

    def latches(self, value: object, where: str) -> Mapping[str, LossLatch]:
        out = {}
        for code, block in self.named(value, where).items():
            try:
                check_currency(where, code)
            except ValueError:
                self.problems.append(f"{where}.{code} is not a three-letter currency code")
                continue
            block = self.section(block, f"{where}.{code}", _LATCH)
            at = f"{where}.{code}"
            out[code] = LossLatch(self.money(block, at, "daily_loss"), self.money(block, at, "drawdown"))
        return MappingProxyType(out)


def parse_risk_config(loaded: object, source: str = "the risk file") -> RiskConfig:
    """The limits in a loaded risk file. Raises `RiskNotInForce` for a well-formed file that is not in force."""
    read = _Reader()
    top = read.section(loaded, "risk", _TOP)
    in_force, status, decision = top.get("in_force"), top.get("status"), top.get("decision")
    if type(in_force) is not bool:
        read.problems.append(f"risk.in_force must be true or false, not {in_force!r}")
    if status not in ("placeholder", DECIDED):
        read.problems.append(f"risk.status must be placeholder or {DECIDED}, not {status!r}")
    if decision is not None and not (isinstance(decision, str) and _DECISION.fullmatch(decision)):
        read.problems.append(f"risk.decision must be null or a decision record id like HFT-DEC-0001, not {decision!r}")

    desk = read.section(top.get("desk"), "desk", _DESK)
    for key in _NOT_ENFORCED:
        if desk.get(key) is not None:
            read.problems.append(f"desk.{key} is not enforced yet (it needs a currency conversion): leave it null")
    desk_limits = DeskLimits(
        max_open_pairs=read.whole(desk, "desk", "max_open_pairs"),
        max_orders_per_minute=read.whole(desk, "desk", "max_orders_per_minute"),
        max_order_to_trade_ratio=read.whole(desk, "desk", "max_order_to_trade_ratio"),
        order_to_trade_window_ns=read.whole(desk, "desk", "order_to_trade_window_seconds") * SECOND,
        reject_streak_breaker=read.whole(desk, "desk", "reject_streak_breaker"),
        loss_latches=read.latches(desk.get("loss_latches"), "desk.loss_latches"),
    )

    pairs: dict[str, PairLimits] = {}
    for name, block in read.named(top.get("per_pair"), "per_pair").items():
        where = f"per_pair.{name}"
        try:
            check_pair(name)
        except ValueError:
            read.problems.append(f"{where} is not a currency pair like EUR.USD")
            continue
        block = read.section(block, where, _PAIR)
        pairs[name] = PairLimits(
            max_position=read.whole(block, where, "max_position"),
            max_order_size=read.whole(block, where, "max_order_size"),
            price_collar_ticks=read.whole(block, where, "price_collar_ticks", least=0),
            exit_collar_ticks=read.whole(block, where, "exit_collar_ticks", least=0),
            max_spread_ticks=read.whole(block, where, "max_spread_ticks", least=0),
            stale_quote_ns=read.whole(block, where, "stale_quote_ms") * MS,
        )
        both_set = not {f"{where}.exit_collar_ticks", f"{where}.price_collar_ticks"} & set(read.nulls)
        if both_set and pairs[name].exit_collar_ticks < pairs[name].price_collar_ticks:
            read.problems.append(f"{where}.exit_collar_ticks is narrower than price_collar_ticks: wider, never tighter")
    if not pairs:
        read.problems.append("per_pair must configure at least one pair")

    sleeves: dict[str, SleeveLimits] = {}
    for name, block in read.named(top.get("per_sleeve"), "per_sleeve").items():
        where = f"per_sleeve.{name}"
        try:
            check_sleeve(name)
        except ValueError:
            read.problems.append(f"{where} is not a sleeve name (lower-case words joined by '-')")
            continue
        if name in _RESERVED:
            read.problems.append(f"{where} is a name the book keeps for itself, not a sleeve")
            continue
        block = read.section(block, where, _SLEEVE)
        sleeves[name] = SleeveLimits(
            max_position=read.whole(block, where, "max_position"),
            max_orders_per_minute=read.whole(block, where, "max_orders_per_minute"),
            loss_latches=read.latches(block.get("loss_latches"), f"{where}.loss_latches"),
        )

    quoted_in = sorted({name[4:] for name in pairs})
    scopes = [("desk", desk_limits.loss_latches)]
    scopes += [(f"per_sleeve.{name}", sleeve.loss_latches) for name, sleeve in sleeves.items()]
    for where, latches in scopes:
        read.problems += [
            f"{where}.loss_latches sets no latch in {code}, the quote currency of a configured pair"
            for code in quoted_in
            if code not in latches
        ]

    guards = read.section(top.get("market_guards"), "market_guards", _GUARDS)
    market = MarketGuards(
        rollover_before_ns=read.whole(guards, "market_guards", "rollover_no_entry_minutes_before", least=0) * MINUTE,
        rollover_after_ns=read.whole(guards, "market_guards", "rollover_no_entry_minutes_after", least=0) * MINUTE,
        weekly_close_no_entry_ns=read.whole(guards, "market_guards", "weekly_close_no_entry_minutes", least=0) * MINUTE,
    )
    reconciliation = read.section(top.get("reconciliation"), "reconciliation", _RECONCILIATION)
    grace_ns = read.whole(reconciliation, "reconciliation", "grace_seconds", least=0) * SECOND

    switches = read.section(top.get("switches"), "switches", _SWITCHES)
    names = []
    retry_ns = read.whole(switches, "switches", "flatten_retry_seconds") * SECOND
    for key in _SWITCH_FILES:
        try:
            names.append(check_token(f"switches.{key}", switches.get(key)))
        except ValueError:
            read.problems.append(f"switches.{key} must be a plain file name, not {switches.get(key)!r}")
    if len(names) == 2 and names[0] == names[1]:
        read.problems.append("switches.kill and switches.flatten must be two different files")

    if in_force is True:
        read.problems += [f"{name} is null: a limit in force needs a value" for name in read.nulls]
        if status != DECIDED or decision is None:
            read.problems.append(f"limits in force need `status: {DECIDED}` and the decision record that set them")
    if read.problems:
        raise RiskConfigError(f"{source}: " + "; ".join(read.problems))
    if in_force is not True:
        raise RiskNotInForce(f"{source}: the limits are not in force ({len(read.nulls)} of them are still null)")
    assert isinstance(decision, str)
    flatten, kill = names
    return RiskConfig(
        decision=decision,
        desk=desk_limits,
        pairs=MappingProxyType(pairs),
        sleeves=MappingProxyType(sleeves),
        guards=market,
        reconciliation_grace_ns=grace_ns,
        kill_switch=kill,
        flatten_switch=flatten,
        flatten_retry_ns=retry_ns,
    )


def load_risk_config(path: Path) -> RiskConfig:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RiskConfigError(f"cannot read {path}: {exc}") from exc
    return parse_risk_config(loaded, str(path))
