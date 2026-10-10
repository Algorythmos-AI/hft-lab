"""Client order ids: `hft-<sleeve>-<20 hex>`.

The id is a pure function of what the order is for: the sleeve, the pair, the side, the sequence number of the
event the decision was taken on, the leg and the attempt. The same decision always yields the same id, so a
retry after a crash or a lost acknowledgement cannot create a second order: the venue already holds that id.
A deliberate new attempt passes a new `attempt` number and gets a new id.

The sleeve is readable in the id, so reconciliation can tell whose order it is without a lookup.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Final

from hftlab.core import _checks
from hftlab.core.enums import Side

PREFIX: Final = "hft"
DIGEST_HEX: Final = 20
MAX_SLEEVE: Final = 24
_SLEEVE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ID = re.compile(rf"{PREFIX}-(?P<sleeve>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<digest>[0-9a-f]{{{DIGEST_HEX}}})")


def check_sleeve(sleeve: object) -> str:
    """A sleeve name: lower-case words joined by hyphens, at most 24 characters. `directional-change`."""
    if not isinstance(sleeve, str) or not _SLEEVE.fullmatch(sleeve) or len(sleeve) > MAX_SLEEVE:
        raise ValueError(f"not a sleeve name (lower-case words joined by '-', max {MAX_SLEEVE} chars): {sleeve!r}")
    return sleeve


def client_order_id(
    *, sleeve: str, pair: str, side: Side, decision_seq: int, leg: str = "entry", attempt: int = 0
) -> str:
    """The id for one order. Same arguments, same id; any different argument, a different id."""
    check_sleeve(sleeve)
    _checks.pair(pair)
    _checks.member("side", side, Side)
    _checks.non_negative("decision_seq", decision_seq)
    _checks.token("leg", leg)
    _checks.non_negative("attempt", attempt)
    # JSON keeps the fields apart: no choice of strings can make two different tuples serialise alike.
    key = json.dumps(["v1", sleeve, pair, side.value, decision_seq, leg, attempt], separators=(",", ":"))
    return f"{PREFIX}-{sleeve}-{hashlib.sha256(key.encode('ascii')).hexdigest()[:DIGEST_HEX]}"


def sleeve_of(order_id: object) -> str | None:
    """The sleeve an id of ours belongs to; None for an id this platform did not make."""
    match = _ID.fullmatch(order_id) if isinstance(order_id, str) else None
    return match["sleeve"] if match else None


def is_ours(order_id: object, sleeve: str | None = None) -> bool:
    """Did this platform (or, with `sleeve`, that sleeve) make this id?"""
    found = sleeve_of(order_id)
    return found is not None and (sleeve is None or found == sleeve)
