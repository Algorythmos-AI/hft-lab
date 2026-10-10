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
`preconnect_violations` is the part of it that can be judged before a socket is opened, when no account id
exists yet; `hftlab.safety.gate` applies the two in stages over the life of a connection. The simulated venue
is exempt because it never talks to a broker (hftlab.safety.venues).

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
USERNAME_VAR: Final = "HFT_IBKR_USERNAME"
LIVE_USERNAME_VAR: Final = "HFT_IBKR_LIVE_USERNAME"
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


def _environment_violations(environ: Mapping[str, str]) -> list[str]:
    out = []
    mode = environ.get(MODE_VAR)
    if mode != "paper":
        out.append(f"{MODE_VAR} is {mode!r}, not 'paper'")
    flag = environ.get(LIVE_FLAG_VAR, "")
    if not isinstance(flag, str) or flag.strip().lower() not in _FALSE:
        out.append(f"{LIVE_FLAG_VAR} is set to something other than an explicit false")
    return out


def _port_violations(port: object) -> list[str]:
    if type(port) is not int:
        return ["the gateway port is not an integer"]
    if port in LIVE_PORTS:
        return [f"port {port} is a live trading port"]
    if port not in PAPER_PORTS:
        return [f"port {port} is not a paper port ({', '.join(map(str, sorted(PAPER_PORTS)))})"]
    return []


def _account_violations(accounts: object) -> list[str]:
    if isinstance(accounts, str | bytes) or not isinstance(accounts, Sequence):
        return ["the managed accounts are not a list of account ids"]
    if len(accounts) == 0:
        return ["the broker reported no managed accounts"]
    out = []
    for position, account in enumerate(accounts, 1):
        if isinstance(account, str) and _LIVE_ACCOUNT.fullmatch(account.strip()):
            out.append(f"managed account {position} of {len(accounts)} is a live account id")
        if not isinstance(account, str) or not _PAPER_ACCOUNT.fullmatch(account):
            out.append(f"managed account {position} of {len(accounts)} is not a paper account id (DU and digits)")
    return out


def _username_violations(username: object, live_username: object) -> list[str]:
    if live_username is None:
        return []
    if not isinstance(live_username, str) or not live_username.strip():
        return ["a live username is declared but empty"]
    if not isinstance(username, str) or not username.strip():
        return ["a live username is declared but the configured username is unknown"]
    if _same_login(username, live_username):
        return ["the configured username is the declared live username"]
    return []


def preconnect_violations(
    environ: Mapping[str, str], *, port: int, username: str | None = None, live_username: str | None = None
) -> list[str]:
    """Stage one: everything that can be judged before a socket is opened. No account id exists yet."""
    return _environment_violations(environ) + _port_violations(port) + _username_violations(username, live_username)


def paper_violations(environ: Mapping[str, str], facts: ConnectionFacts) -> list[str]:
    """Every reason this connection is not provably paper; empty when it is. Messages never echo an account id."""
    return (
        _environment_violations(environ)
        + _port_violations(facts.port)
        + _account_violations(facts.managed_accounts)
        + _username_violations(facts.username, facts.live_username)
    )


def process_environment(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """The mapping to judge: the one given, or the process environment. Only this module reads the latter."""
    return os.environ if environ is None else environ


def declared_usernames(environ: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """(the gateway login the owner says is configured, the live login to refuse by name), each None if unset.

    Neither is a credential and neither is sent anywhere: the gateway is logged in by the owner, and the API
    connection carries no username. They exist so the lock can refuse a known live login by name.
    """
    env = process_environment(environ)
    return env.get(USERNAME_VAR) or None, env.get(LIVE_USERNAME_VAR) or None


def assert_paper(facts: ConnectionFacts, environ: Mapping[str, str] | None = None) -> None:
    """Raise `PaperOnlyError` unless every rule holds. Reads the process environment unless one is passed."""
    if found := paper_violations(process_environment(environ), facts):
        raise PaperOnlyError("; ".join(found))
