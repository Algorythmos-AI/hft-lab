"""The cost model of the shadow venue (ADR 0013): what a trade costs beyond the price on the screen.

Every parameter is dated configuration (config/cost_model.yaml). The arithmetic is exact: prices are integer
price units, money is integer millionths of the quote currency, and a fraction that has to be dropped is
always rounded against us, so the model can be too costly by less than a millionth and never too cheap.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from hftlab.calendar.week import MINUTE, in_rollover_window
from hftlab.core import CASH_SCALE, Instrument, Side, to_scaled

NORMAL: Final = "normal"
WIDENED: Final = "widened"
NO_FILL: Final = "no_fill"
_BPS_X100: Final = 1_000_000  # a rate in hundredths of a basis point is millionths of the notional
# placeholder: round figures, no result is a result. decided: set by a decision record, each value saying whether
# it was measured, published or chosen. measured: every value from fills and timings of our own.
PLACEHOLDER: Final = "placeholder"
STATUSES: Final = (PLACEHOLDER, "decided", "measured")
_DECISION = re.compile(r"HFT-DEC-[0-9]{4}")


class CostModelError(ValueError):
    """The cost model file is missing something, or says something that cannot be priced."""


@dataclass(frozen=True, slots=True)
class CostModel:
    effective_from: str
    placeholder: bool
    latency_min_ns: int
    latency_max_ns: int
    commission_rate_bps_x100: int
    commission_minimum: Mapping[str, int]
    min_order_size: int
    slippage_increments: int
    stale_after_ns: int
    no_fill_before_ns: int
    no_fill_after_ns: int
    widen_before_ns: int
    widen_after_ns: int
    spread_multiplier_x100: int
    # Where a pair's base currency has a minimum of its own (the venue's minimum for good pricing differs by
    # currency), it stands in for `min_order_size` in that pair.
    min_order_size_by_base: Mapping[str, int] = MappingProxyType({})

    def as_dict(self) -> dict[str, object]:
        """Every parameter as a plain value: what a journal keeps beside a result, so the two cannot part.

        `min_order_size_by_base` is left out when it is empty, so a model without one is written as it always
        was, and every hash taken over these values before the key existed still holds.
        """
        by_base = dict(sorted(self.min_order_size_by_base.items()))
        return {
            "effective_from": self.effective_from,
            "placeholder": self.placeholder,
            "latency_min_ns": self.latency_min_ns,
            "latency_max_ns": self.latency_max_ns,
            "commission_rate_bps_x100": self.commission_rate_bps_x100,
            "commission_minimum": dict(sorted(self.commission_minimum.items())),
            "min_order_size": self.min_order_size,
            "slippage_increments": self.slippage_increments,
            "stale_after_ns": self.stale_after_ns,
            "no_fill_before_ns": self.no_fill_before_ns,
            "no_fill_after_ns": self.no_fill_after_ns,
            "widen_before_ns": self.widen_before_ns,
            "widen_after_ns": self.widen_after_ns,
            "spread_multiplier_x100": self.spread_multiplier_x100,
            **({"min_order_size_by_base": by_base} if by_base else {}),
        }

    def notional_micro(self, price: int, quantity: int, instrument: Instrument) -> int:
        """Price times quantity, in millionths of the quote currency."""
        if instrument.price_scale > CASH_SCALE:
            raise CostModelError(f"{instrument.pair} quotes finer than a millionth; the cost model cannot price it")
        return price * quantity * int(10 ** (CASH_SCALE - instrument.price_scale))

    def commission_micro(self, notional_micro: int, currency: str) -> int:
        """The commission of an order with this much traded so far: the rate, rounded up, or the minimum."""
        if currency not in self.commission_minimum:
            raise CostModelError(f"the cost model has no commission minimum in {currency}")
        by_rate = -(-notional_micro * self.commission_rate_bps_x100 // _BPS_X100)
        return max(by_rate, self.commission_minimum[currency])

    def least(self, base: str) -> int:
        """The model's own minimum for a pair with this base currency, in base-currency units."""
        return self.min_order_size_by_base.get(base, self.min_order_size)

    def minimum_size(self, instrument: Instrument) -> int:
        """The smallest order it takes: the larger of the venue's and the model's minimum, on the size grid."""
        smallest = max(instrument.min_order_size, self.least(instrument.base))
        return -(-smallest // instrument.size_increment) * instrument.size_increment

    def phase(self, at_ns: int) -> str:
        """NO_FILL close to the daily rollover, WIDENED further out, NORMAL the rest of the day."""
        if in_rollover_window(at_ns, self.no_fill_before_ns, self.no_fill_after_ns):
            return NO_FILL
        if in_rollover_window(at_ns, self.widen_before_ns, self.widen_after_ns):
            return WIDENED
        return NORMAL

    def extra_half_spread(self, spread: int, instrument: Instrument) -> int:
        """What each side of the quote is moved away by in the widened window, rounded up to the price grid."""
        extra = max(spread, 0) * (self.spread_multiplier_x100 - 100)
        step = instrument.price_increment
        return -(-extra // (200 * step)) * step

    def taker_price(self, side: Side, bid: int, ask: int, instrument: Instrument, *, widened: bool) -> int:
        """The price a marketable order gets: the far side of the quote, plus slippage, plus the widening."""
        away = self.slippage_increments * instrument.price_increment
        if widened:
            away += self.extra_half_spread(ask - bid, instrument)
        return ask + away if side is Side.BUY else max(instrument.price_increment, bid - away)

    def round_trip_cost_micro(
        self, instrument: Instrument, quantity: int, entry: tuple[int, int], exit_: tuple[int, int]
    ) -> int:
        """What it costs to take `quantity` at the `entry` (bid, ask) and give it back at the `exit_` (bid, ask),

        measured against the mid at each end, in millionths of the quote currency TIMES TWO (a mid can fall
        between two price units; doubled, everything stays an integer). It is half the spread at each end, the
        slippage at each end, and both commissions. It does not include the move of the mid in between.
        """
        scale = int(10 ** (CASH_SCALE - instrument.price_scale))
        away = self.slippage_increments * instrument.price_increment
        spreads = (entry[1] - entry[0]) + (exit_[1] - exit_[0]) + 4 * away
        fees = sum(
            self.commission_micro(self.notional_micro(price, quantity, instrument), instrument.quote)
            for price in (entry[1] + away, max(1, exit_[0] - away))
        )
        return spreads * quantity * scale + 2 * fees


def _decimal(value: object, what: str, scale: int) -> int:
    if not isinstance(value, str):
        raise CostModelError(f"{what} must be a quoted decimal such as '0.20', not {value!r}")
    try:
        return to_scaled(Decimal(value), scale)
    except (ArithmeticError, ValueError) as exc:
        raise CostModelError(f"{what} {value!r} is not a decimal with at most {scale} places") from exc


def _whole(mapping: Mapping[str, Any], key: str, what: str) -> int:
    value = mapping.get(key)
    if type(value) is not int or value < 0:
        raise CostModelError(f"{what}.{key} must be a whole, non-negative number, not {value!r}")
    return value


def load_cost_model(path: Path) -> CostModel:
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CostModelError(f"cannot read {path}: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("version") != 1:
        raise CostModelError(f"{path} is not a version 1 cost model")
    latency, commission, rollover = (loaded.get(name) for name in ("latency_us", "commission", "rollover"))
    if not (isinstance(latency, dict) and isinstance(commission, dict) and isinstance(rollover, dict)):
        raise CostModelError(f"{path} needs latency_us, commission and rollover sections")
    minimum = commission.get("minimum")
    if not isinstance(minimum, dict) or not minimum:
        raise CostModelError("commission.minimum must give a minimum per quote currency")
    by_base = loaded.get("min_order_size_by_base", {})
    if not isinstance(by_base, dict) or not all(isinstance(code, str) and len(code) == 3 for code in by_base):
        raise CostModelError("min_order_size_by_base must map a base currency to a size")
    low, high = _whole(latency, "min", "latency_us"), _whole(latency, "max", "latency_us")
    multiplier = _decimal(rollover.get("spread_multiplier"), "rollover.spread_multiplier", 2)
    if high < low or multiplier < 100 or not isinstance(loaded.get("effective_from"), str):
        raise CostModelError("latency max below min, a spread multiplier below 1, or no effective_from date")
    status, decision = loaded.get("status"), loaded.get("decision")
    if status not in STATUSES:
        raise CostModelError(f"status must be one of {', '.join(STATUSES)}, not {status!r}")
    named = isinstance(decision, str) and _DECISION.fullmatch(decision) is not None
    if (status != PLACEHOLDER and not named) or (decision is not None and not named):
        raise CostModelError("a cost model that is not a placeholder names the decision record that set it")
    return CostModel(
        effective_from=loaded["effective_from"],
        placeholder=status == PLACEHOLDER,
        latency_min_ns=low * 1_000,
        latency_max_ns=high * 1_000,
        commission_rate_bps_x100=_decimal(commission.get("rate_bps"), "commission.rate_bps", 2),
        commission_minimum=MappingProxyType(
            {str(c): _decimal(amount, f"commission.minimum.{c}", CASH_SCALE) for c, amount in minimum.items()}
        ),
        min_order_size=_whole(loaded, "min_order_size", "cost model"),
        slippage_increments=_whole(loaded, "slippage_increments", "cost model"),
        stale_after_ns=_whole(loaded, "stale_after_ms", "cost model") * 1_000_000,
        no_fill_before_ns=_whole(rollover, "no_fill_before_min", "rollover") * MINUTE,
        no_fill_after_ns=_whole(rollover, "no_fill_after_min", "rollover") * MINUTE,
        widen_before_ns=_whole(rollover, "widen_before_min", "rollover") * MINUTE,
        widen_after_ns=_whole(rollover, "widen_after_min", "rollover") * MINUTE,
        spread_multiplier_x100=multiplier,
        min_order_size_by_base=MappingProxyType(
            {code: _whole(by_base, code, "min_order_size_by_base") for code in sorted(by_base)}
        ),
    )
