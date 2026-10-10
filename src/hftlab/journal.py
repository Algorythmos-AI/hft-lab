"""A hash-chained journal: one JSON line per entry, each chained to the one before by a hash.

    {"hash": "...", "i": 7, "prev": "...", "type": "fill", ...}

`hash` is the SHA-256 of the line without it, written in one canonical form (sorted keys, no spaces), and
`prev` is the hash of the line before; the first line's `prev` is sixty-four zeros. So a line cannot be
changed, dropped, inserted or moved without every later hash being wrong, and two runs over the same events
write the same bytes.

The file is only ever appended to. A process killed in the middle of a line leaves a last line without its
newline: on opening, that piece is cut off and the chain continues from the last whole line. A whole line that
does not verify is something else, and is never repaired: `JournalError`.

Every value is an integer, text, a list or a mapping of those. There is no float in a journal.

It is used by the shadow runner, the order manager's outbox, the risk gate's latches and the reconciler, each
with a file of its own. With `fsync=True` a flushed entry is on the disk, and so is the file's name in its
directory, before `flush` returns: that is what lets the outbox promise an intent is durable before it is sent.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

GENESIS: Final = "0" * 64


class JournalError(Exception):
    """The journal's chain is broken, or something was offered to it that it cannot keep exactly."""


def canonical(body: dict[str, object]) -> str:
    _exact(body)
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _exact(value: object) -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise JournalError(f"a journal key must be text, not {key!r}")
            _exact(inner)
    elif isinstance(value, list | tuple):
        for inner in value:
            _exact(inner)
    elif value is not None and not isinstance(value, str | int):
        raise JournalError(f"a journal holds integers and text only, not {value!r}")


@dataclass(frozen=True, slots=True)
class JournalState:
    """What reading a journal found: how much of it is whole, and where a new run continues from."""

    entries: int = 0
    last_hash: str = GENESIS
    good_bytes: int = 0
    torn: bool = False
    sessions: int = 0  # `start` entries so far
    last_event: int = 0  # the highest event number any entry carries


def read(path: Path) -> tuple[JournalState, list[dict[str, object]]]:
    """Verify the whole chain and return its entries. Raises `JournalError` at the first line that is wrong."""
    data = path.read_bytes() if path.exists() else b""
    entries: list[dict[str, object]] = []
    prev, good, sessions, last_event = GENESIS, 0, 0, 0
    lines = data.split(b"\n")
    torn = bool(lines[-1])  # bytes after the last newline: a line that was being written
    for number, raw in enumerate(lines[:-1]):
        try:
            entry = json.loads(raw)
        except ValueError as exc:
            raise JournalError(f"{path.name} line {number + 1}: not JSON") from exc
        if not isinstance(entry, dict):
            raise JournalError(f"{path.name} line {number + 1}: not a mapping")
        claimed = entry.pop("hash", None)
        if hashlib.sha256(canonical(entry).encode("ascii")).hexdigest() != claimed:
            raise JournalError(f"{path.name} line {number + 1}: the line does not match its own hash")
        if entry.get("prev") != prev:
            raise JournalError(f"{path.name} line {number + 1}: the line does not follow the one before it")
        if entry.get("i") != number:
            raise JournalError(f"{path.name} line {number + 1}: numbered {entry.get('i')!r}, expected {number}")
        prev, good = str(claimed), good + len(raw) + 1
        sessions += entry.get("type") == "start"
        event = entry.get("n")
        last_event = max(last_event, event) if isinstance(event, int) else last_event
        entries.append(entry)
    return JournalState(len(entries), prev, good, torn, sessions, last_event), entries


def verify(path: Path) -> JournalState:
    return read(path)[0]


class Journal:
    def __init__(self, path: Path, *, fsync: bool = False) -> None:
        self.path, self._fsync = path, fsync
        self._stream: IO[bytes] | None = None
        self._prev, self._count, self._dirty = GENESIS, 0, False

    def open(self) -> JournalState:
        """Verify what is there, cut off a torn last line, and continue the chain."""
        state = verify(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        self._stream = self.path.open("ab")
        if state.torn:
            self._stream.truncate(state.good_bytes)
        if self._fsync and (created or state.torn):
            self._sync_directory()
        self._prev, self._count = state.last_hash, state.entries
        return state

    def write(self, kind: str, **fields: object) -> str:
        """Append one entry and return its hash. It reaches the file at the next `flush`."""
        if self._stream is None:
            raise JournalError("the journal is not open")
        body: dict[str, object] = {**fields, "type": kind, "i": self._count, "prev": self._prev}
        text = canonical(body)
        digest = hashlib.sha256(text.encode("ascii")).hexdigest()
        self._stream.write(b'{"hash":"' + digest.encode("ascii") + b'",' + text.encode("ascii")[1:] + b"\n")
        self._prev, self._count, self._dirty = digest, self._count + 1, True
        return digest

    @property
    def entries(self) -> int:
        return self._count

    @property
    def last_hash(self) -> str:
        return self._prev

    def push(self) -> None:
        """Hand what was written to the operating system, without waiting for the disk.

        It is then in the file for any reader and survives this process being killed. It does not survive the
        power going: only `flush`, with fsync on, promises that.
        """
        if self._stream is not None:
            self._stream.flush()

    def flush(self) -> None:
        if self._stream is not None and self._dirty:
            self._stream.flush()
            if self._fsync:
                os.fsync(self._stream.fileno())
            self._dirty = False

    def _sync_directory(self) -> None:
        """Make the file's name, and a cut-off tail, durable: a file whose name was never synced can vanish."""
        assert self._stream is not None
        os.fsync(self._stream.fileno())
        handle = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
