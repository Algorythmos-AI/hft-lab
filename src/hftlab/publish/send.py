"""Sending the snapshot: one HTTPS POST, signed with a key that the host holds and the status site knows.

    x-wt-timestamp   Unix seconds, when it was signed
    x-wt-signature   "sha256=" and the hex of HMAC-SHA256(key, "<timestamp>." + body)
    x-wt-key-id      which key signed it

That is the form the status site's ingest checks. It is taken from the sibling repository's dashboard at
commit 1b23931 (dashboard/src/lib/hmac.ts and ingest.ts): the site compares the signature in constant time,
refuses a timestamp more than five minutes from its own clock, and takes the desk from the `schema` field
inside the signed body, never from a header. The header names are the receiving site's, which is why they do
not carry this platform's prefix.

The address, the key id and the key come from the environment and from nowhere else. With any of them missing
the snapshot is not sent, and the refusal says which one. A snapshot that does not conform to its contract is
not sent either. Neither the key nor the address is ever in a message.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from hftlab.ops.host import Secret
from hftlab.ops.post import Outcome, Poster, refusal
from hftlab.publish import contract

NEEDED: Final = ("HFT_PUBLISH_URL", "HFT_PUBLISH_KEY_ID", "HFT_PUBLISH_KEY")
ALREADY_NEWER: Final = 409  # the site holds a snapshot with a later as_of: nothing to do, and not a failure
_KEY_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,40}")
# The id the site gives a sender that names none. It belongs to another desk's host, so it is never ours.
_NOT_OURS: Final = "default"


class Refused(Exception):
    """The snapshot is not sent, and the message says why. Nothing has left the host."""


@dataclass(frozen=True, slots=True)
class Destination:
    url: str
    key_id: str
    key: Secret
    bypass: Secret  # the hosting provider's deployment-protection token, when the site is behind one


def destination(settings: Mapping[str, str]) -> Destination:
    """Where to send and what to sign with, or `Refused` naming what is missing."""
    missing = [name for name in NEEDED if not settings.get(name, "").strip()]
    if missing:
        raise Refused(f"not sending: {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set")
    key_id = settings["HFT_PUBLISH_KEY_ID"].strip()
    if not _KEY_ID.fullmatch(key_id) or key_id == _NOT_OURS:
        raise Refused("not sending: HFT_PUBLISH_KEY_ID must be this platform's own key id, as the site knows it")
    url = settings["HFT_PUBLISH_URL"].strip()
    if reason := refusal(url):
        raise Refused(f"not sending: HFT_PUBLISH_URL is {reason.removeprefix('refused: ')}")
    key, bypass = Secret(settings["HFT_PUBLISH_KEY"].strip()), Secret(settings.get("HFT_PUBLISH_BYPASS", "").strip())
    return Destination(url, key_id, key, bypass)


def body_of(snapshot: Mapping[str, object]) -> bytes:
    """The bytes that are signed and sent: one canonical form, so the same snapshot is the same body."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign(key: str, timestamp: int, body: bytes) -> str:
    """The signature of `body` at `timestamp`, in the form the site checks."""
    signed = f"{timestamp}.".encode("ascii") + body
    return "sha256=" + hmac.new(key.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def headers(where: Destination, timestamp: int, body: bytes) -> dict[str, str]:
    out = {
        "content-type": "application/json",
        "x-wt-timestamp": str(timestamp),
        "x-wt-signature": sign(where.key.reveal(), timestamp, body),
        "x-wt-key-id": where.key_id,
    }
    if where.bypass:
        out["x-vercel-protection-bypass"] = where.bypass.reveal()
    return out


def send(
    snapshot: Mapping[str, object], schema: dict[str, Any], settings: Mapping[str, str], poster: Poster, now_s: int
) -> Outcome:
    """Check, sign and POST one snapshot, once. Raises `Refused`, before anything leaves, when it may not go."""
    if found := contract.problems(snapshot, schema):
        raise Refused(f"not sending: the snapshot does not conform to its contract: {'; '.join(found[:5])}")
    where = destination(settings)
    body = body_of(snapshot)
    return poster(where.url, body, headers(where, now_s, body))
