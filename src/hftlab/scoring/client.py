"""The trader's side of a scorer in its own process (ADR 0023, section 5).

`ProcessScorer` is a scorer like any other (`hftlab.models.scorer.Scorer`): a row in, a decision or nothing
out. Behind it is a child process (`hftlab.scoring.child`) and a pipe each way. What it promises the trader:

  * **It never waits longer than the timeout, and never twice for one row.** A row is sent once. No reply in
    time, a closed pipe, a reply that is not one, a reply from another model, a refusal: the row is not scored
    (`score` returns None, the sleeve abstains), the child is killed, and the caller goes on. The row is never
    asked again.
  * **It never blocks on a child that is down.** While the child is dead, starting or refused, `score`
    answers at once. A child is started again no sooner than a back-off that doubles from the first wait to
    the last, and is the first wait again after a row was scored. Starting is not waited for either:
    `tend` asks for the model and looks for the answer on a later turn.
  * **It never raises into the trader.** Everything a child can do wrong ends in an abstention.
  * **It serves the lineup's model or none.** The child answers a load with the hash of what it holds. At the
    first start any answer but the lineup's hash is `ScorerRefused`, and the place gets no scorer. Later, it
    is a refusal for the rest of the session: every row is abstained on and nothing is started again.
  * **It stops cleanly.** `close` ends the child's input, which is how the child is told; one that does not
    leave is killed. A child whose trader dies sees the same end of input.

Every row that was not scored is handed to `missed` with the reason, before `score` returns: the session
writes it down, and a replay abstains on exactly those rows without any child (`hftlab.scoring.record`).

Time is injected. The back-off and the time a start may take are measured on the clock handed in, which is
the session's. The one wait that is real is the wait for a reply, and it is bounded by the timeout.

The trader's loop stays on one thread. Each child has a second one that does a single thing: it moves whole
frames from the child's output into a queue. It decides nothing and touches nothing else.

The pipe is written without blocking. At most one question is ever unanswered (the child is killed at the
first that is), so a write that does not fit means a child that is not reading, and is a failure like the rest.
"""
from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import hftlab
from hftlab.core import Clock
from hftlab.features import FeatureRow
from hftlab.journal import JournalError
from hftlab.models.file import Model
from hftlab.models.scorer import Decision, ScoreError, Scorer, ask, heard, scorer_for
from hftlab.scoring import wire
from hftlab.scoring.record import Abstained, Recorded, recorded

SECOND: Final = 1_000_000_000
READY, STARTING, DOWN, REFUSED = "ready", "starting", "down", "refused"  # where a scorer stands
# Why a row was not scored. The first six also end the child.
TIMEOUT, CLOSED, MALFORMED, OTHER_MODEL, NOT_SENT, ROW_REFUSED = (
    "TIMEOUT", "CLOSED", "MALFORMED", "OTHER_MODEL", "NOT_SENT", "ROW_REFUSED",
)  # fmt: skip
NOT_A_MESSAGE, WRONG_MODEL = "NOT_A_MESSAGE", "WRONG_MODEL"
_REPLY: Final = frozenset({"model", "side", "expected_micro"})


class ScorerRefused(Exception):
    """The scorer process cannot serve this model, and starting it again would not help."""


@dataclass(frozen=True, slots=True)
class Times:
    """How long things may take. Settings: `scorer:` in config/learn.yaml."""

    timeout_ns: int  # the wait for the reply to one row
    start_ns: int  # the time a started child may take to load its model
    restart_first_ns: int  # the wait before a failed child is started again ...
    restart_last_ns: int  # ... which doubles up to this

    def __post_init__(self) -> None:
        numbers = (self.timeout_ns, self.start_ns, self.restart_first_ns, self.restart_last_ns)
        if any(type(number) is not int or number < 1 for number in numbers):
            raise ValueError("a scorer's four times are whole numbers of nanoseconds above zero")
        if self.restart_last_ns < self.restart_first_ns:
            raise ValueError("the last wait before a restart is not shorter than the first")


@dataclass(frozen=True, slots=True)
class _Ended:
    why: str


class _Gone(Exception):
    """The child's output ended, or was not frames."""


def _environment() -> dict[str, str]:
    """What a child is started with: where the package is, and nothing of the trader's own environment."""
    package = Path(hftlab.__file__ or "").resolve().parent.parent
    return {"PYTHONPATH": str(package), "PYTHONDONTWRITEBYTECODE": "1"}


class _Child:
    """One started process, its two pipes, and the thread that reads its replies into a queue."""

    def __init__(self, command: Sequence[str], environment: Mapping[str, str]) -> None:
        self.process = subprocess.Popen(
            list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=dict(environment), bufsize=0,
        )  # fmt: skip
        assert self.process.stdin is not None and self.process.stdout is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        self._replies: queue.SimpleQueue[str | _Ended] = queue.SimpleQueue()
        self._reader = threading.Thread(target=self._read, name="scorer-replies", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        try:
            while True:
                line = wire.read_frame(stream.read)
                if line is None:
                    self._replies.put(_Ended(CLOSED))
                    return
                self._replies.put(line)
        except wire.WireError:
            self._replies.put(_Ended(MALFORMED))
        except (OSError, ValueError):
            self._replies.put(_Ended(CLOSED))
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def send(self, data: bytes) -> bool:
        """Write one frame without waiting. False when the pipe did not take all of it."""
        stream = self.process.stdin
        assert stream is not None
        try:
            return os.write(stream.fileno(), data) == len(data)
        except (OSError, ValueError):  # full, broken or already closed
            return False

    def take(self, wait_ns: int) -> str | None:
        """The next reply, waiting at most `wait_ns`; None when there is none yet. Raises `_Gone`."""
        try:
            got = self._replies.get(timeout=wait_ns / SECOND) if wait_ns > 0 else self._replies.get_nowait()
        except queue.Empty:
            return None
        if isinstance(got, _Ended):
            raise _Gone(got.why)
        return got

    def _close_input(self) -> None:
        if self.process.stdin is not None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()

    def kill(self) -> None:
        """End it now. It is not waited for: `reaped` is asked later."""
        with contextlib.suppress(OSError):
            self.process.kill()
        self._close_input()

    def reaped(self) -> bool:
        """Has it ended? Asked without waiting."""
        try:
            self.process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return False
        return True

    def stop(self, wait_ns: int) -> None:
        """End its input and wait for it to leave; kill it if it does not."""
        self._close_input()
        try:
            self.process.wait(timeout=wait_ns / SECOND)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._reader.join(timeout=wait_ns / SECOND)


class ProcessScorer:
    def __init__(
        self,
        model: Model,
        models: Path,
        times: Times,
        *,
        clock: Clock,
        missed: Callable[[FeatureRow, str], None],
        command: Sequence[str] | None = None,
    ) -> None:
        self._model, self._hash = model, model.content_hash
        child = (sys.executable, "-m", "hftlab.scoring.child", "--models", str(models))
        self._command = tuple(command) if command is not None else child
        self._times, self._clock, self._missed = times, clock, missed
        self._child: _Child | None = None
        self._ready = False
        self._asked_at = 0
        self._wait_ns = times.restart_first_ns
        self._retry_at = 0
        self._why = ""  # why it is refused for good; empty while it is not
        self._dead: list[_Child] = []
        self.starts = 0
        self.failures = 0
        self.last_failure = ""
        self._start(clock.now_ns())
        self._look(times.start_ns)
        if self._why:
            self.close()
            raise ScorerRefused(f"the scorer process does not hold model {self._hash[:12]}: {self._why}")

    @property
    def model(self) -> Model:
        return self._model

    @property
    def pid(self) -> int | None:
        """The child's process id while there is one."""
        return None if self._child is None else self._child.process.pid

    @property
    def health(self) -> str:
        if self._why:
            return REFUSED
        if self._child is None:
            return DOWN
        return READY if self._ready else STARTING

    @property
    def retry_at_ns(self) -> int:
        """While it is down: the earliest moment it is started again."""
        return self._retry_at

    # ---- one row ------------------------------------------------------------------------------------------------

    def score(self, row: FeatureRow) -> Decision | None:
        """The child's decision on this row, or None: an abstention of the model's, or a row it did not score."""
        self.tend()
        child = self._child
        if child is None or not self._ready:
            return self._miss(row, self.health.upper())
        now = self._clock.now_ns()
        try:
            data = wire.frame(ask(row))
        except (ValueError, JournalError):
            # The row cannot be written as one message: too long, or not whole numbers. The child is not at fault.
            return self._miss(row, NOT_A_MESSAGE)
        if not child.send(data):
            return self._miss(row, self._fail(now, NOT_SENT))
        try:
            line = child.take(self._times.timeout_ns)
        except _Gone as gone:
            return self._miss(row, self._fail(now, str(gone)))
        if line is None:
            return self._miss(row, self._fail(now, TIMEOUT))
        try:
            answer = wire.said(line)
            if "refused" in answer:
                return self._miss(row, self._fail(now, ROW_REFUSED))
            if set(answer) != _REPLY:
                return self._miss(row, self._fail(now, MALFORMED))
            if answer["model"] != self._hash:
                return self._miss(row, self._fail(now, OTHER_MODEL))
            found = heard(line)
        except (wire.WireError, ScoreError):
            return self._miss(row, self._fail(now, MALFORMED))
        self._wait_ns = self._times.restart_first_ns
        return found

    def _miss(self, row: FeatureRow, why: str) -> Decision | None:
        self._missed(row, why)
        return None

    # ---- the child ----------------------------------------------------------------------------------------------

    def tend(self) -> None:
        """Start a child that is due, and take a starting one's answer if it has come. It never waits."""
        self._dead = [child for child in self._dead if not child.reaped()]
        if self._why:
            return
        if self._child is None:
            now = self._clock.now_ns()
            if now >= self._retry_at:
                self._start(now)
        elif not self._ready:
            self._look(0)

    def _start(self, now: int) -> None:
        """Start a child and ask it for the model. The answer is looked for later."""
        self.starts += 1
        self._ready, self._asked_at = False, now
        try:
            self._child = _Child(self._command, _environment())
        except OSError:
            self._fail(now, NOT_SENT)
            return
        if not self._child.send(wire.frame(wire.load(self._hash))):
            self._fail(now, NOT_SENT)

    def _look(self, wait_ns: int) -> None:
        """Take a starting child's answer to the load: ready, failed, or refused for good."""
        child = self._child
        if child is None or self._ready:
            return
        now = self._clock.now_ns()
        try:
            line = child.take(wait_ns)
        except _Gone as gone:
            self._fail(now, str(gone))
            return
        if line is None:
            if wait_ns > 0 or now - self._asked_at >= self._times.start_ns:
                self._fail(now, TIMEOUT)
            return
        try:
            answer = wire.said(line)
        except wire.WireError:
            self._fail(now, MALFORMED)
            return
        if answer == {"loaded": self._hash}:
            self._ready = True
            return
        # It answered, and not with this model: the file is missing or is another model, or the child holds
        # another. Starting it again would ask the same file the same thing.
        self._drop()
        self._why = WRONG_MODEL

    def _fail(self, now: int, why: str) -> str:
        """The child failed: kill it, and wait before the next one. Returns `why`."""
        self.failures += 1
        self.last_failure = why
        self._drop()
        self._retry_at = now + self._wait_ns
        self._wait_ns = min(2 * self._wait_ns, self._times.restart_last_ns)
        return why

    def _drop(self) -> None:
        if self._child is not None:
            self._child.kill()
            self._dead.append(self._child)
        self._child, self._ready = None, False

    def close(self) -> None:
        """Stop the child: its input ends, it leaves, and it is waited for. One that stays is killed."""
        if self._child is not None:
            self._child.stop(self._times.timeout_ns + self._times.start_ns)
            self._child, self._ready = None, False
        for child in self._dead:
            child.stop(self._times.timeout_ns)
        self._dead = []


class Scorers:
    """The process scorers of one session; in a replay, the in-process scorers that stand for them.

    `heard(sleeve, row, known_ns, why)` is told of every row that was not scored, in both: the session
    journals it, so the journal of a replay holds the same lines as the journal of the day.
    """

    def __init__(
        self,
        *,
        times: Times | None,
        record: Path,
        replayed: Path | None,
        clock: Clock,
        heard: Callable[[str, int, int, str], None],
    ) -> None:
        self._times, self._clock, self._heard = times, clock, heard
        self._rows = None if replayed is None else recorded(replayed)
        self._record = Abstained(record)
        self._made: dict[str, ProcessScorer] = {}

    def make(self, name: str, model: Model, models: Path) -> Scorer:
        """The scorer of one place. Raises `ScorerRefused` when no process can serve this model."""
        if self._rows is not None:
            again = self._rows.get(name, {})
            return Recorded(scorer_for(model), again, lambda row, why: self._heard(name, row.n, row.known_ns, why))
        if self._times is None:
            raise ScorerRefused("the settings ask for a scorer process and the trader was handed no times for one")

        def missed(row: FeatureRow, why: str) -> None:
            self._record.write(name, row, why)  # in the file first: a replay must find every row the journal has
            self._heard(name, row.n, row.known_ns, why)

        made = ProcessScorer(model, models, self._times, clock=self._clock, missed=missed)
        self._made[name] = made
        return made

    def tend(self) -> None:
        for scorer in self._made.values():
            scorer.tend()

    def health(self) -> dict[str, str]:
        """Where each scorer process stands, by sleeve. Empty in a replay, which has none."""
        return {name: scorer.health for name, scorer in self._made.items()}

    def pid(self, name: str) -> int | None:
        return self._made[name].pid

    def close(self) -> None:
        for scorer in self._made.values():
            scorer.close()
        self._made = {}
        self._record.close()
