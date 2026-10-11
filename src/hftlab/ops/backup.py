"""`python -m hftlab.ops.backup`: hand the platform's journals to somewhere that is not this disk. It never deletes.

    python -m hftlab.ops.backup list                    what would be backed up, one file a line
    python -m hftlab.ops.backup run                     hand it over
    python -m hftlab.ops.backup verify --folder DIR     check a backup folder against its own manifest

What has to outlive this disk is small and cannot be made again: the journals of the recording (every gap and
its reason, the starts and stops, the instruments), the store's run counter, the shadow journals, the alert
state, the record of the deploys, and everything the trader and the learning loop keep:

  * the trader's folder: the order outbox (which is also the broker book's journal: every fill it booked), the
    shadow book's journal, the risk gate's latches, the reconciler's journal, the stage journal, the lineup
    journal (which model is champion) and every day's decision journal;
  * the learning folder: the model registry (`models/`), the evaluation reports (`reports/`), the trial ledger
    and the word the last training run left.

Those two folders are taken whole: a file nobody has listed is backed up, not dropped. What is left out is
named in `LEFT_OUT`, each with its reason, and a test holds a running trader and a training run to it. The
recordings themselves are not in the backup: they are large and leave through the store's own offload, which
removes a segment only after a confirmed copy (ADR 0012).

Two ways out:

  * A directory: HFT_BACKUP_DIR, which on the host is a place the owner mounts or syncs elsewhere. Each run
    writes a new folder `<tag>/<UTC time>/` holding the files and a MANIFEST.json with their sizes and
    SHA-256. Every copy is read back and compared, and the manifest is written last: a folder with a manifest
    is a whole backup.
  * A tool: `backup.tool` in config/ops.yaml, a command that is handed the tag and a file listing what to back
    up. It is used instead of the directory when it is set.

It never deletes: not a source, not an older backup, not even its own half-written copy. A tool command that
would forget, prune or delete is refused. The tag is this platform's own, so its backups cannot be taken for
another desk's.

This job has no network and no address to page. It leaves what became of the run in the state directory
(`backup/last.json`), and the status check, which does page, reads it there.

Exit codes: 0 handed over, or the folder matches its manifest; 1 it failed, or the folder does not match;
2 nowhere to hand it to, or a setting could not be read.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from hftlab.core import WallClock
from hftlab.ops import host, status
from hftlab.record.store import JOURNAL, days

SECOND: Final = 1_000_000_000
MANIFEST: Final = "MANIFEST.json"
EXIT_OK, EXIT_FAILED, EXIT_NOWHERE = 0, 1, 2
LEARN_CONFIG: Final = Path("config/learn.yaml")
TRADER, LEARN, REGISTRY = "trader", "learn", "registry"  # the folders taken whole, by the name they get in a backup
_DESTROYS: Final = ("forget", "prune", "delete", "remove", "rm", "purge", "expire")
Runner = Callable[[Sequence[str]], int]


@dataclass(frozen=True, slots=True)
class LeftOut:
    """A kind of file in a folder that is otherwise taken whole, and why the backup does not hold it."""

    where: str  # TRADER, LEARN, or "*" for any folder
    pattern: str  # matched against the path below that folder; `*` crosses a `/`
    why: str


LEFT_OUT: Final = (
    LeftOut(
        TRADER, "days/*/session-*",
        "the event tapes. Not rebuilt: a tape cannot be made again. It is left out because it is large, as the "
        "recordings are, and is evidence for one check only, the replay of its day. It stays on the host under "
        "the tape quota; losing it loses that check for those days and no record of what was decided or booked",
    ),
    LeftOut(TRADER, "status.json", "rebuilt: the running trader replaces it on every beat"),
    LeftOut(TRADER, "trader.lock", "rebuilt: an empty file that only marks that a trader holds the folder"),
    LeftOut(
        TRADER, "requests/*",
        "rebuilt: a request the trader has not taken yet. What came of a request is in the decision journal; one "
        "that was lost is asked again",
    ),
    LeftOut("*", "*.partial", "rebuilt: a file half written, which its writer renames or writes again"),
)  # fmt: skip


class BackupError(Exception):
    """The hand-over did not complete. Nothing was deleted, here or there."""


@dataclass(frozen=True, slots=True)
class Entry:
    path: Path
    name: str  # where it goes in the backup: record/2026-10-14/journal.jsonl


def left_out(where: str, below: str) -> LeftOut | None:
    """The rule that leaves this file of a whole folder out of the backup, or None: it is backed up."""
    for rule in LEFT_OUT:
        if rule.where in ("*", where) and fnmatch.fnmatchcase(below, rule.pattern):
            return rule
    return None


def whole(folder: Path, where: str, name: str) -> list[tuple[Path, str]]:
    """Every file under `folder` that no rule leaves out, as (path, its name in the backup), in a fixed order."""
    if not folder.is_dir():
        return []
    found = sorted(path for path in folder.rglob("*") if path.is_file())
    kept = [(path, path.relative_to(folder).as_posix()) for path in found]
    return [(path, f"{name}/{below}") for path, below in kept if left_out(where, below) is None]


def manifest(
    record_root: Path,
    shadow_dir: Path,
    state: Path,
    *,
    trader: Path | None = None,
    learn: Sequence[tuple[str, Path]] = (),
    lineup: Path | None = None,
) -> list[Entry]:
    """What must be backed up, as it is on disk now. A file that is not there yet is simply not listed.

    `trader` is the trader's folder and `learn` the learning folders by the name each gets in the backup; all
    are taken whole but for `LEFT_OUT`. `lineup` is the lineup journal where the learning settings put it: it
    is listed by itself when that is not inside the trader's folder.
    """
    wanted = [(record_root / "state.json", "record/state.json")]
    wanted += [(day / JOURNAL, f"record/{day.name}/{JOURNAL}") for day in days(record_root)]
    if shadow_dir.is_dir():
        wanted += [(path, f"shadow/{path.name}") for path in sorted(shadow_dir.glob("*.jsonl"))]
    wanted.append((state / "alerts" / "state.json", "alerts/state.json"))
    wanted.append((state / "deploy" / "deploys.jsonl", "deploy/deploys.jsonl"))  # which code was in service when
    if trader is not None:
        wanted += whole(trader, TRADER, TRADER)
    for name, folder in learn:
        wanted += whole(folder, LEARN, name)
    if lineup is not None and lineup.resolve() not in {path.resolve() for path, _ in wanted}:
        wanted.append((lineup, f"lineup/{lineup.name}"))
    return [Entry(path, name) for path, name in wanted if path.is_file()]


def learning_places(trader_config: Path, learn_config: Path) -> tuple[list[tuple[str, Path]], Path | None]:
    """Where the models, the reports and the ledger are, and the lineup journal, as the shipped settings say.

    The learning settings name the folder training writes (`state_dir`) and the lineup journal; the trader's
    name the folder models are served from (`registry`). They are one folder as shipped. When they are two,
    both are taken. A settings file that is not there names nothing.
    """
    said = status._mapping(learn_config) if learn_config.is_file() else {}
    serving = status._mapping(trader_config) if trader_config.is_file() else {}
    folders: list[tuple[str, Path]] = []
    for name, value in ((LEARN, said.get("state_dir")), (REGISTRY, serving.get("registry"))):
        if isinstance(value, str) and Path(value).resolve() not in {folder.resolve() for _, folder in folders}:
            folders.append((name, Path(value)))
    journal = said.get("lineup")
    return folders, Path(journal) if isinstance(journal, str) else None


def _digest(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def to_directory(entries: Sequence[Entry], target: Path, tag: str, stamp: str) -> Path:
    """Copy every entry into a new folder `target/tag/stamp` and write its manifest. Returns the folder.

    A journal may be appended to while it is copied. The copy is then a whole, shorter journal, and the
    manifest records the copy that was taken, which is read back and compared.
    """
    if not target.is_dir():
        raise BackupError(f"the backup directory {target} is not there. It is the owner's to create or mount")
    folder = target / tag / stamp
    if folder.exists():
        raise BackupError(f"{folder} exists already: a backup is never written over")
    listed = []
    for entry in entries:
        destination = folder / entry.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with entry.path.open("rb") as read, destination.open("xb") as write:
            while chunk := read.read(1 << 20):
                write.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            write.flush()
            os.fsync(write.fileno())
        if _digest(destination) != (digest.hexdigest(), size):
            raise BackupError(f"the copy of {entry.name} does not read back the same. Nothing was deleted")
        listed.append({"name": entry.name, "sha256": digest.hexdigest(), "size": size})
    document = {"tag": tag, "taken_at": stamp, "files": listed}
    with (folder / MANIFEST).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=1, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return folder


def verify(folder: Path) -> list[str]:
    """What is wrong with a backup folder, by its own manifest. An empty list: every file is there and matches."""
    try:
        files = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return [f"{folder} has no manifest that can be read: it is not a whole backup"]
    out = []
    for item in files if isinstance(files, list) else []:
        name = str(item.get("name")) if isinstance(item, dict) else ""
        path = folder / name
        if not name or not path.is_file():
            out.append(f"{name or 'an entry without a name'}: not in the folder")
        elif _digest(path) != (item.get("sha256"), item.get("size")):
            out.append(f"{name}: does not match its manifest")
    return out


def tool_command(template: Sequence[str], tag: str, listing: Path) -> list[str]:
    """The tool's command with the tag and the file list filled in. Refused when it could destroy a backup."""
    words = [str(word) for word in template]
    for word in words:
        bare = word.lower().lstrip("-")
        if bare in _DESTROYS or bare.startswith(tuple(f"{verb}-" for verb in _DESTROYS)):
            raise BackupError(f"the backup tool's command contains {word!r}: this command only ever adds a backup")
    joined = " ".join(words)
    if "{tag}" not in joined or "{list}" not in joined:
        raise BackupError("the backup tool's command must take {tag} and {list}")
    return [word.replace("{tag}", tag).replace("{list}", str(listing)) for word in words]


def to_tool(entries: Sequence[Entry], template: Sequence[str], tag: str, listing: Path, run: Runner) -> None:
    """Write the list of files and hand it, with the tag, to the configured tool."""
    command = tool_command(template, tag, listing)
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text("".join(f"{entry.path.resolve()}\n" for entry in entries), encoding="utf-8")
    code = run(command)
    if code != 0:
        raise BackupError(f"the backup tool {command[0]} ended with status {code}. Nothing was deleted")


def leave_word(state: Path, at_ns: int, ok: bool, files: int) -> None:
    """Write what became of this run where the status check looks. A full disk here must not fail the run twice."""
    path = state / status.BACKUP_WORD
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at_ns": at_ns, "ok": ok, "files": files}, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        print(f"backup: could not write {path}", file=sys.stderr)


def _run(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def load_tool(config: Path) -> list[str] | None:
    try:
        loaded: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BackupError(f"cannot read {config}: {exc}") from exc
    tool = loaded.get("backup", {}).get("tool") if isinstance(loaded, dict) else None
    if tool is not None and not (isinstance(tool, list) and tool and all(isinstance(word, str) for word in tool)):
        raise BackupError(f"{config}: backup.tool is null or a command as a list of words")
    return tool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.backup", description="Hand the journals over.")
    parser.add_argument("command", choices=["list", "run", "verify"])
    parser.add_argument("--folder", type=Path, help="verify: the backup folder to check")
    parser.add_argument("--config", type=Path, default=status.CONFIG)
    parser.add_argument("--recorder-config", type=Path, default=status.RECORDER_CONFIG)
    parser.add_argument("--shadow-config", type=Path, default=status.SHADOW_CONFIG)
    parser.add_argument("--calendar", type=Path, default=status.CALENDAR)
    parser.add_argument("--trader-config", type=Path, default=status.TRADER_CONFIG)
    parser.add_argument("--learn-config", type=Path, default=LEARN_CONFIG)
    args = parser.parse_args(argv)
    if args.command == "verify":
        if args.folder is None:
            parser.error("verify needs --folder")
        wrong = verify(args.folder)
        fine = f"backup: every file in {args.folder} matches its manifest"
        print("\n".join(f"backup: {line}" for line in wrong) or fine)
        return EXIT_FAILED if wrong else EXIT_OK
    settings, clock = host.settings(), WallClock()
    try:
        sources = status.load_sources(
            args.config, args.recorder_config, args.shadow_config, args.calendar, trader_config=args.trader_config
        )
        tool = load_tool(args.config)
        learn, lineup = learning_places(args.trader_config, args.learn_config)
    except (status.StatusError, BackupError, ValueError) as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return EXIT_NOWHERE
    trader = sources.trader_status.parent if sources.trader_status is not None else None
    entries = manifest(
        sources.record_root, sources.shadow_journal.parent, sources.state, trader=trader, learn=learn, lineup=lineup
    )
    if args.command == "list":
        print("\n".join(f"{entry.name}\t{entry.path.stat().st_size}" for entry in entries) or "backup: nothing yet")
        return EXIT_OK
    target = settings["HFT_BACKUP_DIR"]
    if tool is None and not target:
        print("backup: nowhere to hand it to: set HFT_BACKUP_DIR, or backup.tool in the config", file=sys.stderr)
        return EXIT_NOWHERE
    now_ns = clock.now_ns()
    stamp = datetime.fromtimestamp(now_ns // SECOND, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        if tool is not None:
            to_tool(entries, tool, host.BACKUP_TAG, sources.state / "backup" / f"files-{stamp}.txt", _run)
            where = f"the tool {tool[0]}"
        else:
            where = str(to_directory(entries, Path(target), host.BACKUP_TAG, stamp))
    except (BackupError, OSError) as exc:
        print(f"backup: FAILED: {exc}", file=sys.stderr)
        leave_word(sources.state, now_ns, False, len(entries))
        return EXIT_FAILED
    leave_word(sources.state, now_ns, True, len(entries))
    print(f"backup: {len(entries)} file(s) handed to {where}, tagged {host.BACKUP_TAG}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
