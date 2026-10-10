"""Lint for the research registry (research/README.md).

Everything here is a pure function of a tree state: a mapping from repository path to content hash. Comparing
two tree states is how the append-only rule is checked, with no need for a clock or a history walk.

  id           a record under research/{hypotheses,experiments,decisions}/ is named HFT-<KIND>-NNNN[-slug]
  location     a record sits in the folder of its kind, and nowhere else in the repository
  unique       no two records share an id
  append-only  an experiment in the base tree is unchanged in the head tree: not edited, deleted or added to
  reference    a research id of another repository is written `repo:ID`
  trials       every row of config/trial_registry.yaml names a decision that exists
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from hftlab.registry.trials import TrialRow

PREFIX: Final = "HFT"
REPO: Final = "hft-lab"
RESEARCH: Final = "research"
KIND_DIRS: Final[Mapping[str, str]] = {"HYP": "hypotheses", "EXP": "experiments", "DEC": "decisions"}
ID: Final = re.compile(r"HFT-(HYP|EXP|DEC)-\d{4}")
_RECORD = re.compile(r"(?P<id>HFT-(?P<kind>HYP|EXP|DEC)-[0-9]{4})(?:-[a-z0-9]+)*(?:\.[a-z0-9]+)?")
_LOOKS_LIKE_ID = re.compile(r"(?i)(?:[a-z]{2,5}-)?(?:HYP|EXP|DEC)-[0-9]")
_REFERENCE = re.compile(
    r"(?:(?P<repo>[a-z0-9][a-z0-9-]*):)?(?P<id>(?:(?P<prefix>[A-Z]{2,5})-)?(?:HYP|EXP|DEC)-[0-9]{4})(?![0-9])"
)
_NOT_RECORDS: Final = frozenset({".gitkeep", "README.md"})
_SKIP_DIRS: Final = frozenset({"var", "data", "logs", "models", "build", "__pycache__"})

TreeState = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.path}: {self.message}"


def _record_entry(path: str) -> tuple[str, str] | None:
    """(kind folder, record name) when `path` is inside one of the three record folders."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == RESEARCH and parts[1] in KIND_DIRS.values():
        if len(parts) == 3 and parts[2] in _NOT_RECORDS:
            return None
        return parts[1], parts[2]
    return None


def records(tree: TreeState) -> dict[str, set[str]]:
    """Record id -> the well-named records that carry it, as `folder/name`. More than one is a duplicate."""
    out: dict[str, set[str]] = {}
    for path in tree:
        entry = _record_entry(path)
        match = _RECORD.fullmatch(entry[1]) if entry else None
        if entry and match:
            out.setdefault(match["id"], set()).add(f"{entry[0]}/{entry[1]}")
    return out


def lint_layout(tree: TreeState) -> list[Finding]:
    out = []
    for path in sorted(tree):
        entry = _record_entry(path)
        if entry is None:
            if any(_LOOKS_LIKE_ID.match(part) for part in path.split("/")):
                where = "research/{hypotheses,experiments,decisions}/"
                out.append(Finding("location", path, f"named like a research record but not under {where}"))
            continue
        folder, name = entry
        match = _RECORD.fullmatch(name)
        if match is None:
            out.append(Finding("id", path, f"{name!r} is not HFT-<HYP|EXP|DEC>-NNNN with an optional lower-case slug"))
        elif KIND_DIRS[match["kind"]] != folder:
            out.append(Finding("location", path, f"{match['id']} belongs in research/{KIND_DIRS[match['kind']]}/"))
    for record_id, names in sorted(records(tree).items()):
        if len(names) > 1:
            out.append(Finding("unique", f"{RESEARCH}/{min(names)}", f"{record_id} is used by {sorted(names)}"))
    return out


def lint_append_only(base: TreeState, head: TreeState) -> list[Finding]:
    """Experiments that existed in `base` must be byte-identical in `head`. A re-run gets a new id."""
    folder = f"{RESEARCH}/{KIND_DIRS['EXP']}/"
    frozen = {entry[1] for path in base if path.startswith(folder) and (entry := _record_entry(path))}
    out = []
    for path in sorted(base):
        if not path.startswith(folder) or _record_entry(path) is None:
            continue
        if path not in head:
            out.append(Finding("append-only", path, "an experiment file was deleted or renamed"))
        elif head[path] != base[path]:
            out.append(Finding("append-only", path, "an experiment file was modified; a re-run gets a new id"))
    for path in sorted(head):
        entry = _record_entry(path)
        if path.startswith(folder) and entry and path not in base and entry[1] in frozen:
            out.append(Finding("append-only", path, "a file was added to an experiment that already exists"))
    return out


def lint_references(texts: Mapping[str, str]) -> list[Finding]:
    """In record text, an id that is not this repository's must name its repository: `options-lab:DEC-0011`."""
    out = []
    for path in sorted(texts):
        for number, line in enumerate(texts[path].splitlines(), 1):
            for match in _REFERENCE.finditer(line):
                if match["prefix"] != PREFIX and match["repo"] is None:
                    message = f"line {number}: {match['id']} is another repository's id; write it as repo:{match['id']}"
                    out.append(Finding("reference", path, message))
    return out


def lint_trials(rows: Iterable[TrialRow], tree: TreeState) -> list[Finding]:
    known = records(tree)
    return [
        Finding("trials", "config/trial_registry.yaml", f"{row.decision} is not a record in research/decisions/")
        for row in rows
        if row.decision not in known
    ]


def lint(
    head: TreeState,
    *,
    base: TreeState | None = None,
    texts: Mapping[str, str] | None = None,
    trials: Iterable[TrialRow] = (),
) -> list[Finding]:
    """Every finding for `head`; with `base`, the append-only rule as well."""
    out = lint_layout(head)
    if base is not None:
        out += lint_append_only(base, head)
    out += lint_references(texts or {})
    out += lint_trials(trials, head)
    return out


# ---- tree states ----------------------------------------------------------------------------------------------


def blob_hash(data: bytes) -> str:
    """The id git gives this content as a blob, so a working tree can be compared with a commit."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data, usedforsecurity=False).hexdigest()


def worktree_state(root: Path) -> dict[str, str]:
    """The files under `root` (hidden and runtime folders skipped) with their blob hashes. No git needed."""
    out = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        for name in files:
            path = Path(folder) / name
            out[path.relative_to(root).as_posix()] = blob_hash(path.read_bytes()) if path.is_file() else ""
    return out


def commit_state(root: Path, rev: str) -> dict[str, str]:
    """The files of commit `rev` with their blob ids."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", rev], cwd=root, capture_output=True, check=False
    )
    if proc.returncode:
        raise ValueError(f"cannot read commit {rev!r}: {proc.stderr.decode(errors='replace').strip()}")
    out = {}
    for record in proc.stdout.split(b"\x00"):
        if record:
            meta, _, path = record.partition(b"\t")
            out[path.decode("utf-8", errors="replace")] = meta.split()[2].decode()
    return out


def record_texts(root: Path, tree: TreeState) -> dict[str, str]:
    """The text of every record file in `tree`, read from the working directory."""
    return {
        path: (root / path).read_text(encoding="utf-8", errors="replace")
        for path in tree
        if _record_entry(path) is not None and (root / path).is_file()
    }
