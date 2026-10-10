"""The one HTTPS request this layer makes: a POST, for a page to the owner and for the status snapshot.

This module and the broker adapter are the only places in the package that may open a connection, and this
one may import the standard library's HTTP client and nothing else (tests/safety/test_isolation.py). It cannot
be pointed at the gateway: it sends to `https://` addresses only, never to this machine, and it follows no
redirect, so a signed body reaches the address it was signed for or nowhere.

Nothing here knows what an address or a header means, and nothing here writes a log. The outcome is a status
code or the name of the error, never with the address in it: the address of a page is a secret.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TIMEOUT_S: Final = 15
_THIS_MACHINE: Final = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


@dataclass(frozen=True, slots=True)
class Outcome:
    """What became of a POST. `status` is the HTTP status, or None when no answer came; `error` says why not."""

    status: int | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def __str__(self) -> str:
        return f"HTTP {self.status}" if self.status is not None else self.error


Poster = Callable[[str, bytes, Mapping[str, str]], Outcome]


class _Answer(Protocol):
    status: int

    def __enter__(self) -> _Answer: ...

    def __exit__(self, *details: object) -> object: ...


class Opener(Protocol):
    def open(self, request: Request, *, timeout: float) -> _Answer: ...


class _NoRedirect(HTTPRedirectHandler):
    """A redirect is an answer, not an instruction: the body and its signature are not sent on."""

    def redirect_request(self, *_details: object) -> None:
        return None


def refusal(url: str) -> str:
    """Why this address is not sent to, or an empty string. The address itself is never in the answer."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        has_login = parts.username is not None or parts.password is not None
    except ValueError:
        return "refused: not an address"
    if parts.scheme != "https":
        return "refused: not an https address"
    if not host:
        return "refused: no host in the address"
    if has_login:
        return "refused: a user name or password in the address"
    try:
        local = ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_unspecified
    except ValueError:
        local = host in _THIS_MACHINE or host.endswith(".localhost")
    return "refused: an address on this machine" if local else ""


def post(
    url: str, body: bytes, headers: Mapping[str, str], *, timeout_s: int = TIMEOUT_S, opener: Opener | None = None
) -> Outcome:
    """POST `body` to `url` once. No retry, no redirect, and never an exception for a failure of the network."""
    if refused := refusal(url):
        return Outcome(None, refused)
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with (opener or build_opener(_NoRedirect)).open(request, timeout=timeout_s) as answer:
            return Outcome(int(answer.status))
    except HTTPError as exc:
        return Outcome(exc.code)
    except URLError as exc:
        return Outcome(None, type(exc.reason).__name__ if isinstance(exc.reason, BaseException) else "URLError")
    except (OSError, ValueError) as exc:
        return Outcome(None, type(exc).__name__)
