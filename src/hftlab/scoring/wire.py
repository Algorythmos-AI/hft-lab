"""The message format between the trader and a scorer in its own process (ADR 0023, section 5).

A message is a frame:

    1 byte    the version of this format: 1
    4 bytes   the length of what follows, big-endian
    N bytes   one line of canonical JSON in ASCII: sorted keys, no spaces, integers and text only

A frame of another version, an empty one, or one longer than `LIMIT` ends the conversation: the length cannot
be believed, so what follows it cannot be found. `read_frame` raises before it reads a byte of such a message.

There are three questions and their answers, and each is told from the others by its keys:

    {"load": "<hash>"}                                  ->  {"loaded": "<hash of what was loaded>"}
    {"known_ns": .., "n": .., "pair": .., "values": [..]}   ->  {"expected_micro": .., "model": .., "side": ..}
    anything else, or a question that cannot be answered    ->  {"refused": "<why>"}

The second pair is the request and the reply line of the scorer (`hftlab.models.scorer.ask` and `answer`,
ADR 0022), unchanged. A number is a whole number: a line that holds a float, a NaN or an infinity is not a
message. Nothing is pickled and nothing that arrives is run.

Only the standard library is used, and nothing here opens anything: a frame is read from whatever `read` is
handed in and returned as bytes for whoever writes it.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final, NoReturn

from hftlab.journal import canonical

VERSION: Final = 1
LIMIT: Final = 1 << 16  # bytes of one message: a row of some hundred features, each of a hundred digits, fits
HEAD: Final = 5


class WireError(ValueError):
    """Bytes that are not a frame of this version, or a line that is not a message."""


def frame(line: str) -> bytes:
    """One line as the bytes that go down the pipe."""
    try:
        payload = line.encode("ascii")
    except UnicodeEncodeError as exc:
        raise WireError("a message is written in ASCII") from exc
    if not 0 < len(payload) <= LIMIT:
        raise WireError(f"a message is between 1 and {LIMIT} bytes, not {len(payload)}")
    return bytes([VERSION]) + len(payload).to_bytes(4, "big") + payload


def _exactly(read: Callable[[int], bytes], count: int) -> bytes:
    """`count` bytes, or fewer when the stream ended first."""
    got = b""
    while len(got) < count:
        more = read(count - len(got))
        if not more:
            break
        got += more
    return got


def read_frame(read: Callable[[int], bytes]) -> str | None:
    """The next line, or None when the stream ended between two frames. Raises `WireError` on anything else."""
    head = _exactly(read, HEAD)
    if not head:
        return None
    if len(head) < HEAD:
        raise WireError("the stream ended inside the head of a frame")
    if head[0] != VERSION:
        raise WireError(f"a frame of version {head[0]}, and this side speaks version {VERSION}")
    length = int.from_bytes(head[1:], "big")
    if not 0 < length <= LIMIT:
        raise WireError(f"a frame of {length} bytes: a message is between 1 and {LIMIT}")
    payload = _exactly(read, length)
    if len(payload) < length:
        raise WireError("the stream ended inside a frame")
    try:
        return payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise WireError("a message is written in ASCII") from exc


def _no_float(text: str) -> NoReturn:
    raise WireError(f"a message holds whole numbers only, not {text}")


def said(line: str) -> dict[str, object]:
    """A line as the mapping it holds. Raises `WireError` unless it is one JSON object without a float in it."""
    try:
        loaded = json.loads(line, parse_float=_no_float, parse_constant=_no_float)
    except (ValueError, RecursionError) as exc:
        raise WireError(f"not a message: {type(exc).__name__}") from exc
    if not isinstance(loaded, dict):
        raise WireError("a message is one JSON object")
    return loaded


def load(digest: str) -> str:
    """The question that asks the child to load a model by its hash."""
    return canonical({"load": digest})


def loaded(digest: str) -> str:
    """The answer to it: the hash of the model the child now holds, as the child worked it out."""
    return canonical({"loaded": digest})


def refused(why: str) -> str:
    """The answer to a question that is not answered. `why` is cut to what is safe to send."""
    text = "".join(char if " " <= char <= "~" else "?" for char in why)[:200]
    return canonical({"refused": text})
