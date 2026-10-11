"""The lineup journal: a small hash-chained file that says which model serves in which place (ADR 0023).

    {"type": "lineup", "action": "promote", "champion": {...}, "previous": [...], "challengers": [...],
     "candidates": [...], "reasons": [...], "decision": "HFT-DEC-0007", "week": "2026-10-18", "at_ns": ...}

  champion      the model the ML sleeve trades with. None until one has been promoted.
  previous      the champions before it, the latest last: what a rollback restores.
  challengers   the models that run in shadow beside the champion this week.
  candidates    models that training has registered and that start as challengers after the next decision.

A model is named by the SHA-256 of its file, and with it the SHA-256 of the evaluation report it rests on, so
that neither can be exchanged under a name. Every line holds the whole lineup after the change, the action
that made it and why; the lineup in force is the last line. The file is only appended to (`hftlab.journal`): a
line cannot be changed, dropped or moved without every later hash being wrong, and a file whose chain does not
verify is refused, never repaired. The trader reads its models from here and from nowhere else.

The changes are functions of a lineup. They read nothing and write nothing; `append` writes the result down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from hftlab.journal import Journal, JournalError
from hftlab.journal import read as read_journal

FILE: Final = "lineup.jsonl"
REGISTER, PROMOTE, KEEP, ROLL_BACK, DRILL = "register", "promote", "keep", "roll_back", "drill"
ACTIONS: Final = (REGISTER, PROMOTE, KEEP, ROLL_BACK, DRILL)
_HASH = re.compile(r"[0-9a-f]{64}")


class LineupError(ValueError):
    """The lineup journal says something that cannot be acted on, or a change was asked for that cannot be made."""


@dataclass(frozen=True, slots=True)
class Named:
    """A model, and the evaluation report it was registered with, each by the SHA-256 of its file."""

    model: str
    report: str

    def __post_init__(self) -> None:
        if not (isinstance(self.model, str) and isinstance(self.report, str)):
            raise LineupError("a model and its report are named by text")
        if not _HASH.fullmatch(self.model) or not _HASH.fullmatch(self.report):
            raise LineupError("a model and its report are each named by a SHA-256, and by nothing else")

    def plain(self) -> dict[str, object]:
        return {"model": self.model, "report": self.report}


@dataclass(frozen=True, slots=True)
class Lineup:
    champion: Named | None = None
    previous: tuple[Named, ...] = ()
    challengers: tuple[Named, ...] = ()
    candidates: tuple[Named, ...] = ()
    entries: int = 0  # lines of the journal this was read from
    head: str = ""  # the hash of the last of them: what a decision says it was made on

    def plain(self) -> dict[str, object]:
        return {
            "champion": self.champion.plain() if self.champion else None,
            "previous": [one.plain() for one in self.previous],
            "challengers": [one.plain() for one in self.challengers],
            "candidates": [one.plain() for one in self.candidates],
        }

    def same_models(self, other: Lineup) -> bool:
        return self.plain() == other.plain()

    # ---- the changes ----------------------------------------------------------------------------------------------

    def registered(self, candidate: Named) -> Lineup:
        """Training registered a candidate. It serves nowhere yet: it starts in shadow after the next decision."""
        known = (*self.challengers, *self.candidates, *self.previous, *((self.champion,) if self.champion else ()))
        if any(candidate.model == one.model for one in known):
            return self
        return replace(self, candidates=(*self.candidates, candidate))

    def kept(self) -> Lineup:
        """Nothing is promoted. The week's challengers have had their week; the candidates take their places."""
        return replace(self, challengers=self.candidates, candidates=())

    def promoted(self, challenger: Named) -> Lineup:
        """A challenger becomes the champion. The champion it replaces is kept as the one to go back to."""
        if challenger not in self.challengers:
            raise LineupError("only a model that ran its week as a challenger can be promoted")
        before = (*self.previous, self.champion) if self.champion else self.previous
        return replace(self, champion=challenger, previous=before, challengers=self.candidates, candidates=())

    def rolled_back(self) -> Lineup:
        """The champion is taken out and the one before it restored; with none before it, the place is empty."""
        if self.champion is None:
            raise LineupError("there is no champion to roll back")
        restored = self.previous[-1] if self.previous else None
        return replace(self, champion=restored, previous=self.previous[:-1], challengers=self.candidates, candidates=())


def _named(value: object, where: str) -> Named:
    if not isinstance(value, dict) or set(value) != {"model", "report"}:
        raise LineupError(f"{where} is not a model with its report")
    return Named(value["model"], value["report"])


def _several(value: object, where: str) -> tuple[Named, ...]:
    if not isinstance(value, list):
        raise LineupError(f"{where} is not a list of models")
    return tuple(_named(one, where) for one in value)


def _lineup(entry: dict[str, object], entries: int, head: str) -> Lineup:
    champion = entry.get("champion")
    return Lineup(
        None if champion is None else _named(champion, "the champion"),
        _several(entry.get("previous"), "previous"),
        _several(entry.get("challengers"), "challengers"),
        _several(entry.get("candidates"), "candidates"),
        entries,
        head,
    )


def history(path: Path) -> list[dict[str, object]]:
    """Every line of the journal, oldest first, after its chain has been verified."""
    try:
        return [entry for entry in read_journal(path)[1] if entry.get("type") == "lineup"]
    except JournalError as exc:
        raise LineupError(f"the lineup journal does not verify, and is not used: {exc}") from exc


def read(path: Path) -> Lineup:
    """The lineup in force: the last line of a journal that verifies. An absent journal is the empty lineup."""
    try:
        state, entries = read_journal(path)
    except JournalError as exc:
        raise LineupError(f"the lineup journal does not verify, and is not used: {exc}") from exc
    if state.torn:
        raise LineupError("the lineup journal ends in the middle of a line: a change was being written")
    lines = [entry for entry in entries if entry.get("type") == "lineup"]
    if not lines:
        return Lineup(entries=state.entries, head=state.last_hash if state.entries else "")
    return _lineup(lines[-1], state.entries, state.last_hash)


def append(
    path: Path, lineup: Lineup, *, action: str, at_ns: int, reasons: tuple[str, ...] = (), decision: str | None = None,
    week: str = "", report: str | None = None, evidence: dict[str, object] | None = None,
) -> Lineup:  # fmt: skip
    """Write the lineup down as the one in force from now on, durably, with what made it so. Returns it as read."""
    if action not in ACTIONS:
        raise LineupError(f"a lineup changes by one of {ACTIONS}, not by {action!r}")
    journal = Journal(path, fsync=True)
    journal.open()
    try:
        journal.write(
            "lineup", action=action, at_ns=at_ns, reasons=list(reasons), decision=decision, week=week, report=report,
            evidence=evidence or {}, **lineup.plain(),
        )  # fmt: skip
        journal.flush()
    finally:
        journal.close()
    return read(path)
