"""Sleeve state across a restart of the trader: what is kept, and when it may be believed (ADR 0026).

A sleeve's state is everything it remembers (ADR 0014). Without this file every start begins from nothing: a
Directional Change machine is back in its warm-up, a feature history is empty, and a position a sleeve was
holding is closed by the trader because nobody remembers why it was taken.

    <state dir>/sleeves.jsonl      a hash-chained journal; one entry for each session that ended on purpose

An entry is written when a session ends on purpose: at the day's close and at a clean stop. It holds, for each
sleeve that can save its state (`hftlab.strategy.Saves`), what the sleeve wrote, and what the entry is tied to:

  * where every journal the books and the stages are built from stood once the session had closed them;
  * the session's tape: its day, its name, its length, and the hash of its last line, which is the session's
    last word (`["stop", at]`). The line is never read as anything; it is compared as bytes.

At the next start the last entry is restored from ONLY when all of this still holds:

  1. the file verifies, and its last entry is whole (`UNVERIFIED`, `NOT_A_STATE`; a file that is not there
     is `NONE`);
  2. every one of those journals stands exactly where the entry says: nothing was written since, by a
     session that died or by anything else (`JOURNALS`);
  3. the tape the entry names is the last tape there is, and it ends as the entry says (`TAPE`): the saved
     state's last event is the tape's last event.

Otherwise every sleeve starts cold, warm-up flags and all, and the reason is journalled and paged. A session
that was killed wrote no entry, so the entry before it fails 2 and 3 and is never read as current. Then, for
each sleeve:

  * a sleeve that had an order on its way when the entry was written has no state in it (`IN_FLIGHT`: the
    order's end is news the new session cannot give it);
  * a sleeve whose stage is not the one it ran under starts cold (`STAGE`): a stage decides which book its
    positions are read from, and a state reached on one book says nothing true about the other;
  * a state written under other settings, another model among them, is refused by the sleeve itself
    (`SETTINGS`).

Nothing here is repaired. A file whose chain is broken is set aside under another name when the next entry is
due, and a new chain begins.

A replay cannot look at tapes: it runs in a scratch folder that has none. So the session writes what it found
into the head of its own tape (`resumed`), and a replay is handed that instead of looking.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from hftlab.journal import Journal, JournalError, read, verify

FILE: Final = "sleeves.jsonl"
KIND: Final = "sleeves"
VERSION: Final = 1
TAPES: Final = "session-*.tape.jsonl"
SET_ASIDE: Final = ".unverified"
NOT_VERIFIED: Final = -1  # in a mark, where the number of entries would be
# Why nothing is restored from the file ...
NONE, UNVERIFIED, NOT_A_STATE, JOURNALS, TAPE = "NONE", "UNVERIFIED", "NOT_A_STATE", "JOURNALS", "TAPE"
# ... and why one sleeve is not, when the file may be believed.
IN_FLIGHT, ABSENT, STAGE, SETTINGS = "IN_FLIGHT", "ABSENT", "STAGE", "SETTINGS"
QUIET: Final = (NONE,)  # reasons that are nobody's fault and page nobody: there was nothing to restore from
_FIELDS: Final = frozenset(
    {
        "type", "i", "prev", "version", "day", "session", "tape", "tape_end", "n", "at_ns", "marks", "states", "stages",
        "in_flight",
    }
)  # fmt: skip
_TAIL: Final = 1 << 16


def tape_end(path: Path) -> list[object]:
    """How a tape ends: its length in bytes and the SHA-256 of its last whole line. `[0, ""]` when it has none.

    The line is not parsed. What a tape's lines mean is the tape's own business (hftlab.trader.tape).
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(max(0, size - _TAIL))
            tail = stream.read()
    except OSError:
        return [0, ""]
    lines = tail.split(b"\n")
    if len(lines) < 2 or lines[-1] or (len(lines) < 3 and size > _TAIL):
        return [size, ""]  # no whole last line: nothing, a torn one, or one longer than a tape's last word is
    return [size, hashlib.sha256(lines[-2]).hexdigest()]


def latest_tape(days: Path) -> Path | None:
    """The tape of the session that began last: the highest number in the latest day folder that holds one."""
    if not days.is_dir():
        return None
    for day in sorted((path for path in days.iterdir() if path.is_dir()), reverse=True):
        tapes = sorted(day.glob(TAPES))
        if tapes:
            return tapes[-1]
    return None


def standing(path: Path) -> list[object]:
    """Where the state file stands: its entries, its last hash and its length, as the session's tape keeps them.

    A file that does not verify, or cannot be read at all, is marked as such, by its length. The trader starts
    without it: nothing is restored from it, and no start is refused for it.
    """
    try:
        state = verify(path)
    except JournalError:
        return [NOT_VERIFIED, "", path.stat().st_size]
    except OSError:
        return [NOT_VERIFIED, "", 0]
    return [state.entries, state.last_hash, state.good_bytes]


def last(path: Path) -> tuple[dict[str, Any] | None, str]:
    """The last entry of the state file and "", or None and why there is none to restore from."""
    try:
        entries = read(path)[1]
    except (JournalError, OSError):
        return None, UNVERIFIED
    if not entries:
        return None, NONE
    entry = entries[-1]
    whole = (
        set(entry) == _FIELDS
        and entry["type"] == KIND
        and entry["version"] == VERSION
        and isinstance(entry["day"], str)
        and isinstance(entry["tape"], str)
        and isinstance(entry["tape_end"], list)
        and isinstance(entry["marks"], dict)
        and isinstance(entry["states"], dict)
        and isinstance(entry["stages"], dict)
        and isinstance(entry["in_flight"], list)
    )
    return (entry, "") if whole else (None, NOT_A_STATE)


def journals_stand(entry: Mapping[str, Any], marks: Mapping[str, object], day: str, names: Sequence[str]) -> bool:
    """Does every journal stand where the entry left it? `marks` is where each stands now, before this session wrote.

    The day's decision journal is compared when the entry is of the same day. On another day the file in hand
    is another one; the journals that outlive a day each get a line from every session, so they tell.
    """
    kept = entry["marks"]
    if any(name not in kept or kept[name] != marks.get(name) for name in names):
        return False
    return bool(entry["day"] != day or kept.get("decisions") == marks.get("decisions"))


def tape_stands(entry: Mapping[str, Any], days: Path) -> bool:
    """Is the tape the entry names the last one there is, and does it end as the entry says?"""
    found = latest_tape(days)
    if found is None or (found.parent.name, found.name) != (entry["day"], entry["tape"]):
        return False
    ended = tape_end(found)
    return bool(ended[1]) and bool(ended == entry["tape_end"])


def write(
    path: Path,
    *,
    fsync: bool,
    day: str,
    session: int,
    tape: Path,
    n: int,
    at_ns: int,
    marks: Mapping[str, object],
    states: Mapping[str, object],
    stages: Mapping[str, str],
    in_flight: Sequence[str],
) -> None:
    """Append one entry. A file that does not verify is set aside first, as it is, and a new chain begins.

    Raises `JournalError` or `OSError` when it cannot be written. Whoever calls decides what that costs: an
    entry that was not written is a cold start, and nothing worse.
    """
    journal = Journal(path, fsync=fsync)
    try:
        journal.open()
    except JournalError:
        os.replace(path, path.with_name(path.name + SET_ASIDE))
        journal = Journal(path, fsync=fsync)
        journal.open()
    journal.write(
        KIND, version=VERSION, day=day, session=session, tape=tape.name, tape_end=tape_end(tape), n=n, at_ns=at_ns,
        marks=dict(marks), states=dict(states), stages=dict(stages), in_flight=sorted(in_flight),
    )  # fmt: skip
    journal.close()
