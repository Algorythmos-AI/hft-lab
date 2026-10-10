"""The venues this platform may ever connect to. There are two, and neither is a live account.

The adapters live under `hftlab.venues`; this is the list they are checked against. Adding an entry is a
change to the paper-only contract and needs a decision record. tests/safety/test_isolation.py asserts the
exact contents and refuses any other module that rebinds `VENUES`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


class UnknownVenueError(KeyError):
    """A venue name that is not in the registry. There is no fallback."""


@dataclass(frozen=True, slots=True)
class Venue:
    name: str
    description: str
    needs_paper_lock: bool  # False only for a venue that never talks to a broker


VENUES: Final[Mapping[str, Venue]] = MappingProxyType(
    {
        "ibkr_paper": Venue("ibkr_paper", "Interactive Brokers paper account, through a paper gateway", True),
        "sim": Venue("sim", "The simulated venue behind the shadow book; no network, no broker", False),
    }
)


def venue(name: str) -> Venue:
    """The registered venue called `name`; anything else raises."""
    try:
        return VENUES[name]
    except (KeyError, TypeError):
        raise UnknownVenueError(f"no venue named {name!r}: the registry holds {sorted(VENUES)}") from None
