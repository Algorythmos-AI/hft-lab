"""The rows a scorer process did not score, kept so that a replay never needs the process (ADR 0023, section 5).

A scorer in a process of its own can fail where the same arithmetic in the trader's process cannot: it can be
slow, hung, dead or starting. On such a row the sleeve abstains, and that is a decision the tape cannot
explain. So each one is written, as it happens, into a hash-chained journal beside the session's tape:

    session-003.tape.jsonl
    session-003.abstained.jsonl      {"type": "abstained", "sleeve": "ml", "row": 812, "known_ns": .., "why": "TIMEOUT"}

A replay scores in the trader's process (`Recorded`), where every row gets its answer, and abstains on exactly
the rows this file names, for the reason it gives. Nothing else differs between the two paths: the child runs
the same exact arithmetic, so a row it did answer is answered the same way here.

The file is created at the first such row. A session whose scorers never failed has none, and a replay that
finds none abstains nowhere.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from hftlab.features import FeatureRow
from hftlab.journal import Journal, JournalError, read
from hftlab.models.file import Model
from hftlab.models.scorer import Decision, Scorer

TAPE: Final = ".tape.jsonl"
SUFFIX: Final = ".abstained.jsonl"
ABSTAINED: Final = "abstained"
Rows = Mapping[tuple[int, int], str]  # (the row's number, when it was known) -> why it was not scored


def beside(tape: Path) -> Path:
    """Where the record of a session is kept: beside its tape, under the same number."""
    if not tape.name.endswith(TAPE):
        raise ValueError(f"{tape.name} is not a session's tape")
    return tape.with_name(tape.name.removesuffix(TAPE) + SUFFIX)


class Abstained:
    """Writes the record. Each line is in the file, for any reader, before the sleeve is told to abstain."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._journal: Journal | None = None

    def write(self, sleeve: str, row: FeatureRow, why: str) -> None:
        if self._journal is None:
            self._journal = Journal(self.path)
            self._journal.open()
        self._journal.write(ABSTAINED, sleeve=sleeve, row=row.n, known_ns=row.known_ns, why=why)
        self._journal.push()

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()
            self._journal = None


def recorded(path: Path) -> dict[str, dict[tuple[int, int], str]]:
    """The rows each sleeve abstained on by failure, from a session's record. No file: none.

    Raises `JournalError` when the chain does not verify or a line is not such a row: a record that cannot be
    believed replays nothing.
    """
    out: dict[str, dict[tuple[int, int], str]] = {}
    for entry in read(path)[1]:
        sleeve, row, known, why = (entry.get(key) for key in ("sleeve", "row", "known_ns", "why"))
        whole = type(row) is int and type(known) is int
        if entry.get("type") != ABSTAINED or not isinstance(sleeve, str) or not isinstance(why, str) or not whole:
            raise JournalError(f"{path.name}: line {entry.get('i')!r} is not a row a scorer abstained on")
        assert isinstance(row, int) and isinstance(known, int)
        out.setdefault(sleeve, {})[row, known] = why
    return out


class Recorded:
    """The in-process scorer of a replay: it abstains where the session's scorer process failed, and says so."""

    def __init__(self, inner: Scorer, rows: Rows, missed: Callable[[FeatureRow, str], None]) -> None:
        self._inner, self._rows, self._missed = inner, rows, missed

    @property
    def model(self) -> Model:
        return self._inner.model

    def score(self, row: FeatureRow) -> Decision | None:
        why = self._rows.get((row.n, row.known_ns))
        if why is None:
            return self._inner.score(row)
        self._missed(row, why)
        return None
