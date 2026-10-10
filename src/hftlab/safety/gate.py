"""The paper-only lock over the life of one connection, in three stages (ADR 0001, ADR 0009).

  1. Before a socket is opened: mode, live flag, gateway port and username. No account id exists yet.
  2. After the handshake and before anything else is asked of the broker: the full lock, now with the account
     ids the broker itself reported. Passing it mints the permit for that one connection.
  3. Before every order and every cancel: the full lock again, with the permit of the connection in use.

A refusal at any stage is terminal for the gate. It is a statement about where the platform is pointed, not a
network error, so nothing retries it: a new gate needs a new process and a person. A permit dies with its
connection, so a reconnect starts unchecked and has to pass stage two again.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from hftlab.safety.lock import (
    ConnectionFacts,
    PaperOnlyError,
    paper_violations,
    preconnect_violations,
    process_environment,
)

_MINT: Final = object()


class Permit:
    """Proof that one connection passed the full lock. Only `PaperGate.after_handshake` can make one."""

    __slots__ = ("_epoch", "_gate")

    def __init__(self, gate: PaperGate, epoch: int, mint: object) -> None:
        if mint is not _MINT:
            raise PaperOnlyError("a permit is minted by the gate after the handshake check, never built by hand")
        self._gate = gate
        self._epoch = epoch

    def __repr__(self) -> str:
        return f"Permit(connection {self._epoch})"


class PaperGate:
    """One gate per venue object. It holds which connection, if any, has been shown to be paper."""

    def __init__(
        self,
        *,
        port: int,
        username: str | None = None,
        live_username: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._port = port
        self._username = username
        self._live_username = live_username
        self._env = env  # None: the process environment, read afresh at every check
        self._epoch = 0
        self._permit: Permit | None = None
        self._refusal: str | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def refusal(self) -> str | None:
        """Why this gate refused, or None while it has not. Once set it never clears."""
        return self._refusal

    def holds(self, permit: object) -> bool:
        """Is `permit` the one minted for the connection that is open now?"""
        return self._refusal is None and permit is not None and permit is self._permit

    def before_connect(self) -> None:
        """Stage one. Raises unless the environment, the port and the username allow a paper connection."""
        self._still_open()
        env = process_environment(self._env)
        self._judge(
            preconnect_violations(env, port=self._port, username=self._username, live_username=self._live_username)
        )

    def after_handshake(self, managed_accounts: Sequence[str]) -> Permit:
        """Stage two. Raises unless the broker's own account list is all paper; otherwise mints the permit."""
        self._still_open()
        self._permit = None
        self._judge(paper_violations(process_environment(self._env), self._facts(managed_accounts)))
        self._epoch += 1
        self._permit = Permit(self, self._epoch, _MINT)
        return self._permit

    def before_order(self, permit: Permit | None, managed_accounts: Sequence[str]) -> None:
        """Stage three. Raises unless `permit` is current and the full lock still holds."""
        self._still_open()
        if not self.holds(permit):
            raise PaperOnlyError("this connection has not passed the paper-only lock since it was opened")
        self._judge(paper_violations(process_environment(self._env), self._facts(managed_accounts)))

    def connection_closed(self) -> None:
        """The connection is gone, and its permit with it. The next one starts unchecked."""
        self._permit = None

    def _facts(self, managed_accounts: Sequence[str]) -> ConnectionFacts:
        return ConnectionFacts(self._port, managed_accounts, self._username, self._live_username)

    def _still_open(self) -> None:
        if self._refusal is not None:
            raise PaperOnlyError(f"the paper-only lock refused earlier and stays refused: {self._refusal}")

    def _judge(self, violations: list[str]) -> None:
        if violations:
            self._permit = None
            self._refusal = "; ".join(violations)
            raise PaperOnlyError(self._refusal)
