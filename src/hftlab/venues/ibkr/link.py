"""The gateway's own link to the broker, read from the status messages the gateway sends.

Our socket reaches the gateway, a program on this machine. The gateway in turn holds connections of its own to
the broker: one to the broker's servers and one to each data service ("farm"). Any of those can be down while
our socket is fine, and then the gateway accepts every request and answers none that needs the far side.
It says so only in status messages. This module is the one table of the messages that speak of that link, and
the small piece of state that says whether, by the latest message on each part, it is down.

No broker library is imported here: the codes arrive as notices (`ib:<code>`) through the venue port.

The meanings are in our own words. On the real paper gateway, in October 2026, 2104, 2106 and 2158 were seen
in a probe report, and the texts of the "cut" messages were seen without their numbers: those numbers, and
1100, 1101 and 1102, are from the broker's published list and are UNVERIFIED until a report shows them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

SERVERS: Final = "servers"
QUOTES: Final = "quotes"
HISTORY: Final = "history"
CONTRACTS: Final = "contracts"

DOWN_REASON: Final = "the gateway is connected to us but not to the broker's servers"


@dataclass(frozen=True, slots=True)
class LinkCode:
    part: str  # which of the gateway's connections the message speaks of
    down: bool  # True: that connection is cut; False: it is there (again)
    meaning: str


GATEWAY_LINK_CODES: Final[dict[int, LinkCode]] = {
    1100: LinkCode(SERVERS, True, "the gateway lost its connection to the broker's servers; our socket stays up"),
    1101: LinkCode(SERVERS, False, "the gateway is back, and the market-data subscriptions have to be made again"),
    1102: LinkCode(SERVERS, False, "the gateway is back, and the market-data subscriptions were kept"),
    2110: LinkCode(SERVERS, True, "the gateway cannot reach the broker's servers and is retrying by itself"),
    2103: LinkCode(QUOTES, True, "the connection to a quote service is cut: no streaming prices from it"),
    2104: LinkCode(QUOTES, False, "the connection to a quote service is there"),
    2105: LinkCode(HISTORY, True, "the connection to a history service is cut: no past quotes from it"),
    2106: LinkCode(HISTORY, False, "the connection to a history service is there"),
    2157: LinkCode(CONTRACTS, True, "the connection to the contract-description service is cut: lookups go unanswered"),
    2158: LinkCode(CONTRACTS, False, "the connection to the contract-description service is there"),
}
# Left out on purpose, because they do not say that a connection is cut: 2107 and 2108 (a service is idle and
# is connected on demand) and 2119 (a service is being connected).

UPSTREAM_DOWN_CODES: Final = frozenset(code for code, entry in GATEWAY_LINK_CODES.items() if entry.down)


def gateway_code(notice_code: str) -> int | None:
    """The gateway's number in a notice code such as `ib:2110`, or None for any other notice."""
    prefix, _, number = notice_code.partition(":")
    return int(number) if prefix == "ib" and number.isdecimal() else None


class UpstreamLink:
    """Whether the gateway's link to the broker is down, by the latest message about each of its parts.

    A service is told apart by the name the gateway puts after the last colon of its message, so one service
    that is cut is not hidden by another that reports itself well. A service that reports itself connected
    also shows that the broker's servers are reachable.
    """

    def __init__(self) -> None:
        self._down: dict[tuple[str, str], int] = {}

    def hear(self, notice_code: str, message: str) -> None:
        code = gateway_code(notice_code)
        entry = GATEWAY_LINK_CODES.get(code) if code is not None else None
        if code is None or entry is None:
            return
        service = message.rpartition(":")[2].strip() if entry.part != SERVERS and ":" in message else ""
        if entry.down:
            self._down[(entry.part, service)] = code
            return
        self._down.pop((entry.part, service), None)
        self._down.pop((SERVERS, ""), None)
        if entry.part == SERVERS:  # back as a whole: what was said of the services before no longer holds
            self._down.clear()

    @property
    def down(self) -> bool:
        return bool(self._down)

    @property
    def codes(self) -> list[int]:
        """The codes that say what is down now, lowest first."""
        return sorted(set(self._down.values()))
