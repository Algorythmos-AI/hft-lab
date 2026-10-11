"""Replay a day of the trader from its tapes, and compare the decisions with the ones it took (ADR 0005, ADR 0019).

Each session of a day left a tape (hftlab.trader.tape) and its part of the day's decision journal. A session
is replayed from where it began: every journal it read at its start is copied, up to the mark the tape holds,
into a scratch folder, and the same `Trader` runs there on the recording instead of the venue. Nothing is sent
anywhere, nobody is paged, and the real journals are only read.

What must hold:

  * a session that said it stopped is reproduced byte for byte;
  * a session that was killed is reproduced as far as it got: what it wrote is the beginning of what the
    replay writes, because the replay goes on to the end of the turn the process died in.

A replay under other settings than the day ran with is refused, not compared: the tape holds a hash of the
files the decisions depend on.

A session that began from the sleeves' saved states begins from them again: that file is copied by its mark
like the others, and whether its last entry matched the tapes is read from the tape's head, because a scratch
folder has no tapes to look at (hftlab.trader.resume).

A place whose scorer was a process of its own is scored in this process: the day's record of the rows the
child did not score lies beside the tape, and the replay abstains on exactly those (hftlab.scoring.record).
"""
from __future__ import annotations

import contextlib
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from hftlab.calendar import Calendar
from hftlab.journal import verify
from hftlab.lineup import FILE as LINEUP
from hftlab.risk import RiskConfig
from hftlab.scoring.record import beside
from hftlab.trader import resume
from hftlab.trader.config import Settings
from hftlab.trader.session import STATE, UNVERIFIED, Silent, Trader
from hftlab.trader.tape import ReplayEdge, TapeEnded, TapeError, read_tape
from hftlab.venues.sim import CostModel


@dataclass(frozen=True, slots=True)
class Replayed:
    tape: str
    stopped: bool  # the session said it ended; False: the process was killed
    identical: bool
    live_bytes: int
    replayed_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "tape": self.tape, "ended": "stopped" if self.stopped else "killed", "identical": self.identical,
            "live_bytes": self.live_bytes, "replayed_bytes": self.replayed_bytes,
        }  # fmt: skip


def _prefix(source: Path, target: Path, mark: Sequence[object]) -> None:
    """Copy a journal as it stood at a mark, and make sure that is what it was."""
    entries, last_hash, size = mark
    assert isinstance(size, int)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes()[:size] if source.is_file() else b"")
    if entries == UNVERIFIED:
        return  # it did not verify when the session read it, and the session served nothing from it
    state = verify(target)
    if (state.entries, state.last_hash) != (entries, last_hash):
        raise TapeError(f"{source.name} is not the journal this session started from: it was changed or replaced")


def replay_day(
    day: Path,
    scratch: Path,
    settings: Settings,
    *,
    model: CostModel,
    calendar: Calendar,
    risk: RiskConfig | None,
    config_digest: str,
) -> list[Replayed]:
    """Replay every session of the day folder `day`, each in a folder of its own under `scratch`."""
    state_dir = day.parent.parent
    taken = day / "decisions.jsonl"
    tapes = sorted(day.glob("session-*.tape.jsonl"))
    if not tapes:
        raise TapeError(f"{day} holds no tape")
    recordings = [read_tape(tape) for tape in tapes]
    whole = taken.read_bytes()[: verify(taken).good_bytes] if taken.is_file() else b""
    starts = [int(rows[1][1]["journals"]["decisions"][2]) for rows in recordings]
    out = []
    for number, (tape, rows) in enumerate(zip(tapes, recordings, strict=True)):
        marks = rows[1][1]
        if marks["config"] != config_digest or marks["paper"] != (risk is not None):
            raise TapeError(f"{tape.name} was recorded under other settings than the ones given; it is not compared")
        folder = scratch / tape.name.removesuffix(".tape.jsonl")
        if folder.exists():
            shutil.rmtree(folder)
        for name in STATE:
            _prefix(state_dir / f"{name}.jsonl", folder / f"{name}.jsonl", marks["journals"][name])
        _prefix(state_dir / LINEUP, folder / LINEUP, marks["journals"]["lineup"])  # the models it ran with
        kept = marks["journals"].get(resume.KIND)  # the sleeves' saved states it started from (ADR 0026)
        if kept is not None and kept[0] == UNVERIFIED:
            # It did not verify then, and may have been set aside since. What the session saw is a file of that
            # length that does not verify, and that is what it is given.
            (folder / resume.FILE).write_bytes(b"?" * max(0, int(kept[2]) - 1) + b"\n")
        elif kept is not None:
            _prefix(state_dir / resume.FILE, folder / resume.FILE, kept)
        copy = folder / "days" / day.name / "decisions.jsonl"
        _prefix(taken, copy, marks["journals"]["decisions"])
        edge = ReplayEdge(rows, paper=risk is not None)
        trader = Trader(
            edge, settings, edge.open(), model=model, calendar=calendar, risk=risk, notify=Silent(),
            config_digest=config_digest, folder=folder, status=False, abstained=beside(tape),
            resumed=marks.get("resumed") == 1,
        )  # fmt: skip
        with contextlib.suppress(TapeEnded):  # the process was gone there; what the replay wrote so far is compared
            trader.run()
        if edge.stopped:
            trader.close()
        else:
            trader.abandon()
        begun = starts[number]
        ended = starts[number + 1] if number + 1 < len(starts) else len(whole)
        was, now = whole[begun:ended], copy.read_bytes()[begun:]
        same = now == was if edge.stopped else now.startswith(was)
        out.append(Replayed(tape.name, edge.stopped, same, len(was), len(now)))
    return out
