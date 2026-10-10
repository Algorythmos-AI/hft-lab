"""Exact prices and quantities (ADR 0002).

A price is an integer count of the pair's smallest quote unit: EUR.USD 1.08512 at scale 5 is 108512, USD.JPY
151.234 at scale 3 is 151234. A quantity is a whole number of base-currency units. A float never enters: these
helpers take decimal strings, `Decimal` and `int`, and refuse `float` and `bool`, because one binary rounding
in a price is a wrong order or a book that does not add up. Nothing here rounds; a value the scale cannot hold
exactly is an error.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

MAX_SCALE: Final = 9
_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_WHOLE = re.compile(r"[0-9]+")


class InexactError(ValueError):
    """The value has a digit the scale cannot hold; rounding it would be a silent price change."""


def _pow10(exponent: int) -> int:
    return int(10**exponent)  # typeshed types int ** int as Any


def require_int(name: str, value: object) -> int:
    """`value` if it is exactly an `int`. `bool` is refused too: `True` is not a price."""
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    return value


def require_scale(scale: object) -> int:
    checked = require_int("scale", scale)
    if not 0 <= checked <= MAX_SCALE:
        raise ValueError(f"scale must be between 0 and {MAX_SCALE}, not {checked}")
    return checked


def to_scaled(value: str | Decimal | int, scale: int) -> int:
    """`value` as an integer count of 10**-scale units: `to_scaled("1.08512", 5) == 108512`.

    A string must be a plain decimal number: digits, an optional sign, an optional fraction. No exponent, no
    spaces, no digit separators, no NaN.
    """
    require_scale(scale)
    if isinstance(value, bool | float):
        raise TypeError(f"{type(value).__name__} is not an exact number: pass a decimal string, a Decimal or an int")
    if isinstance(value, int):
        return value * _pow10(scale)
    if isinstance(value, str):
        if not _DECIMAL.fullmatch(value):
            raise ValueError(f"not a plain decimal number: {value!r}")
        value = Decimal(value)
    elif not isinstance(value, Decimal):
        raise TypeError(f"cannot scale a {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"not a finite number: {value}")
    # Integer arithmetic on the digits, so the decimal context's precision can never round the result.
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    magnitude = int("".join(map(str, digits)))
    shift = exponent + scale
    if shift >= 0:
        magnitude *= _pow10(shift)
    else:
        magnitude, lost = divmod(magnitude, _pow10(-shift))
        if lost:
            raise InexactError(f"{value} has more than {scale} decimal places")
    return -magnitude if sign else magnitude


def from_scaled(value: int, scale: int) -> str:
    """The canonical decimal string: exactly `scale` decimals, no exponent. `from_scaled(108512, 5) == "1.08512"`."""
    require_int("value", value)
    require_scale(scale)
    digits = str(abs(value)).rjust(scale + 1, "0")
    text = digits if scale == 0 else f"{digits[:-scale]}.{digits[-scale:]}"
    return f"-{text}" if value < 0 else text


def to_decimal(value: int, scale: int) -> Decimal:
    """The exact `Decimal` of a scaled integer, for reports. Never the way back into a price."""
    return Decimal(from_scaled(value, scale))


def parse_quantity(value: str | int) -> int:
    """A whole, non-negative number of base-currency units: `"25000"` or `25000`, never `25000.0` or `"2.5e4"`."""
    if isinstance(value, bool | float):
        raise TypeError(f"{type(value).__name__} is not a quantity: pass a string of digits or an int")
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, str):
        if not _WHOLE.fullmatch(value):
            raise ValueError(f"not a whole number of units: {value!r}")
        quantity = int(value)
    else:
        raise TypeError(f"cannot read a quantity from a {type(value).__name__}")
    if quantity < 0:
        raise ValueError(f"a quantity cannot be negative: {quantity}")
    return quantity


def notional(price: int, quantity: int) -> int:
    """`price` x `quantity`: quote currency at the price's scale. Exact for any size."""
    return require_int("price", price) * require_int("quantity", quantity)
