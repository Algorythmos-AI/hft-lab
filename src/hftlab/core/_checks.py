"""Field checks shared by the domain types. Each returns the value or raises; none repairs it."""
from __future__ import annotations

import re
from enum import Enum

from hftlab.core.money import require_int

_PAIR = re.compile(r"[A-Z]{3}\.[A-Z]{3}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


def positive(name: str, value: object) -> int:
    checked = require_int(name, value)
    if checked <= 0:
        raise ValueError(f"{name} must be positive, not {checked}")
    return checked


def non_negative(name: str, value: object) -> int:
    checked = require_int(name, value)
    if checked < 0:
        raise ValueError(f"{name} cannot be negative: {checked}")
    return checked


def currency(name: str, value: object) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValueError(f"{name} must be a three-letter currency code, not {value!r}")
    return value


def pair(value: object) -> str:
    """A pair in the form the venue uses: base, a dot, quote. `EUR.USD`."""
    if not isinstance(value, str) or not _PAIR.fullmatch(value) or value[:3] == value[4:]:
        raise ValueError(f"not a currency pair like 'EUR.USD': {value!r}")
    return value


def token(name: str, value: object) -> str:
    """An identifier that is safe in a log line, a file name and an order reference."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be a non-empty identifier of letters, digits and . _ : -, not {value!r}")
    return value


def member[E: Enum](name: str, value: object, kind: type[E]) -> E:
    if not isinstance(value, kind):
        raise TypeError(f"{name} must be a {kind.__name__}, not {type(value).__name__}")
    return value
