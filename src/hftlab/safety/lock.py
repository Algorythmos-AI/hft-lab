"""Paper-only lock for Interactive Brokers (ADR 0001).

Real-money trading is never enabled from this repository. The lock is the single place that decides whether a
connection may be used, and it is checked when a venue adapter connects and again before every order. All of
these must hold:

  * `MODE` is exactly "paper"
  * `LIVE_TRADING_ENABLED` is unset, empty or an explicit false; any other value refuses
  * the gateway port is a paper port: IB Gateway 4002 or TWS 7497
  * the broker reported at least one managed account, and every one of them is a paper id (`DU` and digits)
  * if a live username is declared, the configured username is known and is not that one

`paper_violations` is pure: it reads nothing but its arguments, so every rule is testable without a broker.
The simulated venue is exempt because it never talks to a broker (hftlab.safety.venues).

This module is the only one in the package that may name a live port or read the environment variables above;
tests/safety/test_isolation.py scans every other module for both.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

PAPER_PORTS: Final = frozenset({4002, 7497})
# Never accepted. Named only so the refusal can say what the port is.
LIVE_PORTS: Final = frozenset({4001, 7496})
MODE_VAR: Final = "MODE"
LIVE_FLAG_VAR: Final = "LIVE_TRADING_ENABLED"
_FALSE: Final = frozenset({"", "0", "false", "no", "off"})
_PAPER_ACCOUNT = re.compile(r"DU[0-9]+")
_LIVE_ACCOUNT = re.compile(r"U[0-9]+")


class PaperOnlyError(RuntimeError):
    """Raised instead of doing anything that could reach a live account."""


@dataclass(frozen=True, slots=True)
class ConnectionFacts:
    """What is known about one broker connection.

    `managed_accounts` is the list the broker itself reported after connecting, not a configured value.
    `username` is the login configured for the gateway. `live_username` is optional and discouraged: the
    host is meant to hold a paper-only login and nothing else, so there should be no live name to declare.
    Declare one only to make the lock refuse a known live login by name as well.
    """

    port: int
    managed_accounts: Sequence[str]
    username: str | None = None
    live_username: str | None = None


def _same_login(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def paper_violations(environ: Mapping[str, str], facts: ConnectionFacts) -> list[str]:
    """Every reason this connection is not provably paper; empty when it is. Messages never echo an account id."""
    out = []

    mode = environ.get(MODE_VAR)
    if mode != "paper":
        out.append(f"{MODE_VAR} is {mode!r}, not 'paper'")
    flag = environ.get(LIVE_FLAG_VAR, "")
    if not isinstance(flag, str) or flag.strip().lower() not in _FALSE:
        out.append(f"{LIVE_FLAG_VAR} is set to something other than an explicit false")

    port = facts.port
    if type(port) is not int:
        out.append("the gateway port is not an integer")
    elif port in LIVE_PORTS:
        out.append(f"port {port} is a live trading port")
    elif port not in PAPER_PORTS:
        out.append(f"port {port} is not a paper port ({', '.join(map(str, sorted(PAPER_PORTS)))})")

    accounts = facts.managed_accounts
    if isinstance(accounts, str | bytes) or not isinstance(accounts, Sequence):
        out.append("the managed accounts are not a list of account ids")
    elif len(accounts) == 0:
        out.append("the broker reported no managed accounts")
    else:
        for position, account in enumerate(accounts, 1):
            if isinstance(account, str) and _LIVE_ACCOUNT.fullmatch(account.strip()):
                out.append(f"managed account {position} of {len(accounts)} is a live account id")
            if not isinstance(account, str) or not _PAPER_ACCOUNT.fullmatch(account):
                out.append(f"managed account {position} of {len(accounts)} is not a paper account id (DU and digits)")

    if facts.live_username is not None:
        if not isinstance(facts.live_username, str) or not facts.live_username.strip():
            out.append("a live username is declared but empty")
        elif not isinstance(facts.username, str) or not facts.username.strip():
            out.append("a live username is declared but the configured username is unknown")
        elif _same_login(facts.username, facts.live_username):
            out.append("the configured username is the declared live username")
    return out


def assert_paper(facts: ConnectionFacts, environ: Mapping[str, str] | None = None) -> None:
    """Raise `PaperOnlyError` unless every rule holds. Reads the process environment unless one is passed."""
    if found := paper_violations(os.environ if environ is None else environ, facts):
        raise PaperOnlyError("; ".join(found))
