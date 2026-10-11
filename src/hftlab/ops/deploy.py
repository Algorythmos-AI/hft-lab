"""`python -m hftlab.ops.deploy`: the one way the code on the host changes, run by the owner (ADR 0027).

    python -m hftlab.ops.deploy run --commit SHA --dry-run     say what a deploy would do, and do none of it
    python -m hftlab.ops.deploy run --commit SHA               deploy that commit of main
    python -m hftlab.ops.deploy back                           put the checkout before this one in service again
    python -m hftlab.ops.deploy smoke                          the checks a staged checkout must pass, by itself
    python -m hftlab.ops.deploy adopt                          once: turn the install script's folder into the layout
    python -m hftlab.ops.deploy prune --dry-run                say which old checkouts and environments would go
    python -m hftlab.ops.deploy prune                          remove them

The layout. The install root is a symbolic link, and a deploy changes what it points to in one rename:

    /home/hft/hft-lab                    -> hft-lab.d/releases/<commit>      the checkout in service
    /home/hft/hft-lab.d/repo.git         a mirror of origin's main: what is fetched into
    /home/hft/hft-lab.d/releases/<commit>/     one whole checkout per commit, never changed after it is made;
                                               its .venv and var are links to the two folders below
    /home/hft/hft-lab.d/envs/<hash>/     an environment, named by the hash of the lock files it was built from
    /home/hft/hft-lab.d/var/             the state directory: the recordings, the journals, the switches

So a process that was started before a deploy goes on reading the checkout it was started in, whole, and one
started after reads the new one, whole. Nothing is ever a mixture, whenever this command is killed.

`run` refuses, with nothing changed but the mirror, unless all of these hold:

  * the commit is on `main`, and its required checks, `test` and `leakcheck`, are green. The look-up is the
    `gh` program when the host has it. When it has not, or cannot be asked, the owner says they looked, with
    `--checks-verified-by-owner`. A check that was looked up and is not green is refused whatever is said;
  * every sleeve is flat in the broker book and no order is on its way, by the order outbox, which is that
    book's journal. An outbox that does not verify is not "flat": it is refused;
  * the trader is stopped, or the market is shut;
  * the recorder is not restarted inside the rollover window, when spreads are widest and a hole in the
    recording costs most;
  * the unit files of the commit are the ones installed. Installing a unit needs the service manager told,
    which is the owner's own step (docs/runbooks/deploy.md).

Then, each step printed before it runs: the commit is checked out into a new folder under `releases/`; its
environment is built from the hash-locked requirement files, or the one already built from the same files is
reused; the smoke tests are run in it, by its own interpreter; the link is swapped; the units named are
restarted; and the platform's status is checked. If that check fails the link is swapped back and the same
units restarted, by this command, without asking the gate again: putting back the code that was running is
always allowed (the idea is the other desk's `wt.ops.deploy` at its commit 316fec5; no code is shared).

What it restarts, and what it never does. Only the recorder and the shadow runner can be named: neither holds
an object that could place an order. A unit that is not running is never started. **The trader is never
restarted, started or stopped by this command**, whatever is asked: a trader that is running (the gate allows
that only with the market shut and every sleeve flat) keeps running the checkout it was started in, and is the
owner's to restart. The timers' jobs read the new checkout the next time they start.

The smoke tests are the commands of `smoke_commands`: every unit's own module started far enough to import
everything it needs and read the shipped settings, in the staged checkout, with a scratch state directory and
no address or key. The tests marked `smoke` (tests/ops/test_deploy.py) run the same list, so what a deploy
checks on the host is what CI checked.

It runs as root, because restarting a unit needs that, and every program that touches the tree (git, uv, the
smoke run) is started as the platform's own user. This module opens no connection: `gh` and `git` are
programs it starts.

`prune` removes old checkouts and the environments nothing uses any more. Neither `run` nor `back` removes
anything, so without it `releases/` and `envs/` grow by one folder a deploy. It keeps:

  * the checkout in service, which the root links to;
  * the one to go back to, and the one that was gone back from: both commits of `last.json`;
  * every checkout a running process has its working directory in. A unit that was not restarted, the trader
    above all, goes on running the checkout it was started in, however old. Where a running unit runs is
    looked up; when that cannot be looked up, nothing is removed at all;
  * the newest of the rest, as many as `deploy.keep_more_releases` of config/ops.yaml says, by the order of
    the deploy journal. A journal that does not verify is no order: nothing is removed.

It says what it keeps and why, and what it would remove, before it removes anything, and then says each
removal before doing it. A folder is first moved out of `releases/` (or `envs/`) in one rename and removed
after that, so a checkout is whole or gone, never half of one, whenever the command is killed; what a kill
left on the way out is removed by the next run. An environment is removed only when no checkout that is kept
links to it.

Exit codes: 0 deployed (or nothing to do), 1 deployed and rolled back, 2 refused.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

from hftlab.calendar import CalendarError, load_calendar
from hftlab.calendar.week import MINUTE, in_rollover_window
from hftlab.core import Clock, WallClock
from hftlab.core.ids import sleeve_of
from hftlab.journal import Journal, JournalError
from hftlab.journal import read as read_journal
from hftlab.ops import host, units
from hftlab.ops.alerts import Page, Sender, sender_from

REQUIRED_CHECKS: Final = ("test", "leakcheck")
RECORDER, TRADER = "hft-recorder.service", "hft-trader.service"
RESTARTABLE: Final = (RECORDER, "hft-shadow.service")  # market data and the shadow venue: no order path in either
ROLLOVER_BEFORE_NS: Final = 30 * MINUTE  # ENGINEERING DEFAULT: no recorder restart from this long before 17:00 New York
ROLLOVER_AFTER_NS: Final = 30 * MINUTE  # ENGINEERING DEFAULT: until this long after it
SETTLE_S: Final = 60  # ENGINEERING DEFAULT: how long a restarted unit is given before the status check gives up
LOCK_FILE, ML_LOCK_FILE, ML_MARK = "requirements.lock.txt", "requirements-ml.lock.txt", ".hft-lock-ml"
COMPLETE: Final = ".complete"
REMOVING: Final = "removing"  # in the layout's home: where a folder is moved, in one rename, before it is removed
KEEP_MORE_KEY: Final = "keep_more_releases"  # under `deploy:` in config/ops.yaml
ENDED: Final = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})  # the stages of an order that is over
EXIT_OK, EXIT_ROLLED_BACK, EXIT_REFUSED = 0, 1, 2
_COMMIT: Final = re.compile(r"[0-9a-f]{7,40}")
_SLUG: Final = re.compile(r".*github\.com[:/](?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?")

Say = Callable[[str], None]
Git = Callable[[Sequence[str]], tuple[int, str]]  # git with these words, in the mirror: (exit code, what it printed)
Checks = Callable[[str], Mapping[str, str] | None]  # a commit's checks by name; None: they could not be looked up
# Who runs where: a running unit, or another process, and its working directory. None: it could not be looked up
InUse = Callable[[], Mapping[str, Path] | None]


class DeployRefused(Exception):
    """The deploy will not go on as things are. What is in service was not changed."""


class Units(Protocol):
    """The service manager, as far as a deploy needs it. A test hands in one that manages nothing."""

    def active(self, unit: str) -> bool: ...

    def restart(self, unit: str) -> None: ...


class Health(Protocol):
    """The status check around a swap. `note` looks before it; `after` says what is wrong since, or nothing."""

    def note(self) -> None: ...

    def after(self, restarted: Sequence[str]) -> str: ...


@dataclass(frozen=True, slots=True)
class Tools:
    """Everything a deploy does to the world outside its folders, injected: a test needs no network and no root."""

    git: Git
    checks: Checks
    build_env: Callable[[Path, Path, bool], None]  # (the staged checkout, the folder to build in, with the ML set)
    smoke: Callable[[Path], list[str]]  # what failed in the staged checkout; empty: it passed
    units: Units
    health: Health


@dataclass(frozen=True, slots=True)
class Layout:
    """Where everything is, from the install root alone."""

    root: Path  # the link every unit's WorkingDirectory names

    @property
    def home(self) -> Path:
        return self.root.with_name(self.root.name + ".d")

    @property
    def repo(self) -> Path:
        return self.home / "repo.git"

    @property
    def releases(self) -> Path:
        return self.home / "releases"

    @property
    def envs(self) -> Path:
        return self.home / "envs"

    @property
    def state(self) -> Path:
        return self.home / "var"

    @property
    def last(self) -> Path:
        return self.home / "last.json"  # {"from": ..., "to": ...}: written before a swap, read by `back`

    def release(self, commit: str) -> Path:
        return self.releases / commit

    def current(self) -> str | None:
        """The commit in service: the name of the release the root links to. None when the root is not such a link."""
        if not self.root.is_symlink():
            return None
        target = (self.root.parent / os.readlink(self.root)).resolve()
        return target.name if target.parent == self.releases.resolve() and target.is_dir() else None


# ---- what the journals say -------------------------------------------------------------------------------------------


def broker_book(outbox: Sequence[Mapping[str, object]]) -> tuple[dict[str, int], list[str]]:
    """(what each sleeve holds in each pair by the broker book's journal, the orders that have not ended).

    The order outbox holds every fill the order manager booked and every stage an order went through
    (ADR 0016). A fill is counted once, by its id. A position is named `sleeve pair`; flat ones are left out.
    """
    held: dict[str, int] = {}
    seen: set[object] = set()
    stage: dict[str, str] = {}
    for entry in outbox:
        kind, order = entry.get("type"), str(entry.get("client_order_id"))
        if kind == "intent":
            stage.setdefault(order, "JOURNALLED")
        elif kind == "stage":
            stage[order] = str(entry.get("to"))
        elif kind == "fill" and entry.get("fill_id") not in seen:
            seen.add(entry.get("fill_id"))
            quantity = entry.get("quantity")
            signed = (quantity if type(quantity) is int else 0) * (1 if entry.get("side") == "BUY" else -1)
            key = f"{sleeve_of(order) or 'unknown'} {entry.get('pair')}"
            held[key] = held.get(key, 0) + signed
    working = sorted(order for order, at in stage.items() if at not in ENDED)
    return {key: quantity for key, quantity in held.items() if quantity}, working


def holds_lock(path: Path) -> bool:
    """Does a running process hold this lock file? The trader holds one for as long as it runs."""
    try:
        with path.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


# ---- the gate --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Facts:
    """What the gate judges, looked up once. Nothing here is assumed: what could not be read says so."""

    commit: str
    on_main: bool
    checks: Mapping[str, str] | None  # None: they could not be looked up
    owner_verified: bool  # the owner said, on the command line, that they looked the checks up themselves
    held: Mapping[str, int] | None  # the broker book's open positions; None: its journal cannot be read
    working: Sequence[str]  # orders that have not ended
    trader_running: bool
    market_open: bool
    restarts: Sequence[str]
    in_rollover_window: bool
    units_differ: Sequence[str] = ()  # unit files of the commit that are not the ones installed


def refusals(facts: Facts, *, checks_needed: bool = True) -> list[str]:
    """Why this deploy may not run, one sentence each. An empty list: it may."""
    out = []
    short = facts.commit[:12]
    if checks_needed:
        if not facts.on_main:
            out.append(f"{short} is not a commit of main: only reviewed code is deployed")
        if facts.checks is None:
            if not facts.owner_verified:
                out.append(
                    f"the checks of {short} could not be looked up. Look at them yourself, and when "
                    f"{' and '.join(REQUIRED_CHECKS)} are green say so with --checks-verified-by-owner"
                )
        else:
            for name in REQUIRED_CHECKS:
                if facts.checks.get(name) != "success":
                    out.append(f"the check `{name}` of {short} is {facts.checks.get(name, 'missing')}, not success")
    if facts.held is None:
        out.append("the order outbox cannot be read or does not verify: whether the broker book is flat is not known")
    elif facts.held:
        listed = ", ".join(f"{key} {quantity:+d}" for key, quantity in sorted(facts.held.items()))
        out.append(f"the broker book is not flat: {listed}. FLATTEN first (docs/runbooks/kill-and-flatten.md)")
    if facts.working:
        out.append(f"{len(facts.working)} order(s) have not ended, by the outbox: {', '.join(facts.working[:3])}")
    if facts.trader_running and facts.market_open:
        out.append("the trader is running and the market is open: stop the trader, or deploy at the weekend")
    for unit in facts.restarts:
        if unit not in RESTARTABLE:
            out.append(f"{unit} is not restarted by a deploy: only {' and '.join(RESTARTABLE)} can be named")
    if RECORDER in facts.restarts and facts.in_rollover_window:
        out.append(
            "restarting the recorder now would put a hole in the recording inside the rollover window "
            f"({ROLLOVER_BEFORE_NS // MINUTE} minutes before to {ROLLOVER_AFTER_NS // MINUTE} minutes after "
            "17:00 New York)"
        )
    if facts.units_differ:
        out.append(
            f"unit files of {short} are not the ones installed: {', '.join(facts.units_differ)}. Install them first "
            "(docs/runbooks/deploy.md, \"When the unit files changed\")"
        )
    return out


# ---- doing it --------------------------------------------------------------------------------------------------------


class Steps:
    """Every step is said before it is done. In a dry run it is said and not done."""

    def __init__(self, say: Say, dry_run: bool) -> None:
        self._say, self.dry_run, self.count = say, dry_run, 0

    def do(self, what: str, action: Callable[[], object]) -> None:
        self.count += 1
        self._say(f"step {self.count}: {what}" + (" (dry run: not done)" if self.dry_run else ""))
        if not self.dry_run:
            action()


def _own(path: Path) -> None:
    """Hand what root just made to the platform's user. Not root: nothing to hand over."""
    if os.geteuid() == 0:
        owner = pwd.getpwnam(host.USER)
        os.lchown(path, owner.pw_uid, owner.pw_gid)


def _link(target: str, at: Path) -> None:
    """Make `at` a link to `target`, in one rename: it is the old link or the new one, never neither."""
    partial = at.with_name(at.name + ".next")
    with contextlib.suppress(FileNotFoundError):
        partial.unlink()
    os.symlink(target, partial)
    _own(partial)
    os.replace(partial, at)


def _write(path: Path, said: Mapping[str, object]) -> None:
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(said, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _own(partial)
    os.replace(partial, path)


def lock_hash(release: Path, with_ml: bool) -> str:
    """The name of the environment a checkout needs: a hash of its lock files, and of whether the ML set is in it."""
    digest = hashlib.sha256((release / LOCK_FILE).read_bytes())
    if with_ml:
        digest.update(b"\nml\n" + (release / ML_LOCK_FILE).read_bytes())
    return digest.hexdigest()


def differing_units(release: Path, unit_dir: Path | None) -> list[str]:
    """The unit files of a checkout that are not, byte for byte, the ones in the unit directory."""
    if unit_dir is None:
        return []
    files = sorted((release / "deploy" / "systemd").iterdir())
    rendered = {path.name: path.read_text(encoding="utf-8") for path in files}
    return [line.split(":")[0] for line in units.diff(rendered, unit_dir)]


@contextlib.contextmanager
def one_at_a_time(layout: Layout) -> Iterator[None]:
    with (layout.home / "deploy.lock").open("w") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DeployRefused("another deploy is running") from exc
        yield


@dataclass(frozen=True, slots=True)
class Request:
    commit: str
    restart: Sequence[str] | None = None  # None: every unit of RESTARTABLE that is running
    owner_verified: bool = False
    dry_run: bool = False
    unit_dir: Path | None = None  # where the installed unit files are; None: they are not compared


class Deploy:
    def __init__(self, layout: Layout, tools: Tools, clock: Clock, say: Say, sender: Sender | None = None) -> None:
        self.layout, self.tools, self._clock, self._say, self._sender = layout, tools, clock, say, sender

    # ---- looking -------------------------------------------------------------------------------------------------

    def _settings(self, name: str) -> dict[str, Any]:
        try:
            loaded: Any = yaml.safe_load((self.layout.root / "config" / name).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DeployRefused(f"config/{name} of the checkout in service cannot be read: {exc}") from exc
        return loaded if isinstance(loaded, dict) else {}

    def _trader_dir(self) -> Path:
        return self.layout.root / str(self._settings("trader.yaml").get("state_dir", "var/trader"))

    def _resolve(self, commit: str) -> tuple[str, bool]:
        """(the full commit, whether it is on main), by the mirror as it stands."""
        if not _COMMIT.fullmatch(commit):
            raise DeployRefused(f"{commit!r} is not a commit id: name the commit by its hash, not by a branch or a tag")
        code, full = self.tools.git(["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"])
        if code != 0 or not full.strip():
            raise DeployRefused(f"{commit} is not a commit the mirror holds. A dry run fetches nothing; a run does")
        full = full.strip()
        return full, self.tools.git(["merge-base", "--is-ancestor", full, "refs/heads/main"])[0] == 0

    def running(self) -> list[str]:
        return [unit for unit in RESTARTABLE if self.tools.units.active(unit)]

    def facts(self, commit: str, on_main: bool, request: Request, restarts: Sequence[str], *, look_up: bool) -> Facts:
        trader = self._trader_dir()
        held: dict[str, int] | None
        working: list[str] = []
        try:
            held, working = broker_book(read_journal(trader / "outbox.jsonl")[1])
        except (JournalError, OSError):
            held = None
        try:
            market_open = load_calendar(self.layout.root / "config" / "calendar").is_open(self._clock.now_ns())
        except CalendarError as exc:
            raise DeployRefused(f"the calendar of the checkout in service cannot be read: {exc}") from exc
        release = self.layout.release(commit)
        return Facts(
            commit, on_main, self.tools.checks(commit) if look_up else {name: "success" for name in REQUIRED_CHECKS},
            request.owner_verified, held, working,
            self.tools.units.active(TRADER) or holds_lock(trader / "trader.lock"), market_open, restarts,
            in_rollover_window(self._clock.now_ns(), ROLLOVER_BEFORE_NS, ROLLOVER_AFTER_NS),
            differing_units(release, request.unit_dir) if release.is_dir() else (),
        )  # fmt: skip

    def _gate(self, facts: Facts, *, checks_needed: bool = True) -> None:
        found = refusals(facts, checks_needed=checks_needed)
        if found:
            raise DeployRefused("\n".join(found))

    # ---- the steps -----------------------------------------------------------------------------------------------

    def _stage(self, commit: str) -> None:
        """A whole checkout of the commit in a folder of its own. A folder left by a deploy that died is made again."""
        release = self.layout.release(commit)
        if release.exists():
            self.tools.git(["worktree", "remove", "--force", str(release)])
            shutil.rmtree(release, ignore_errors=True)
        self.tools.git(["worktree", "prune"])
        code, said = self.tools.git(["worktree", "add", "--quiet", "--detach", str(release), commit])
        if code != 0:
            raise DeployRefused(f"the commit could not be checked out into {release}: {said.strip()[-300:]}")

    def _environment(self, commit: str, with_ml: bool) -> None:
        """Build the environment the checkout's lock files name, or find it built; then link it and the state in."""
        release = self.layout.release(commit)
        name = lock_hash(release, with_ml)
        built = self.layout.envs / name
        if not (built / COMPLETE).is_file():
            partial = self.layout.envs / f"{name}.partial"
            shutil.rmtree(partial, ignore_errors=True)
            shutil.rmtree(built, ignore_errors=True)
            self.layout.envs.mkdir(parents=True, exist_ok=True)
            self.tools.build_env(release, partial, with_ml)
            (partial / COMPLETE).write_text(name + "\n", encoding="utf-8")
            os.rename(partial, built)
        _link(os.path.join("..", "..", "envs", name), release / ".venv")
        _link(os.path.join("..", "..", "var"), release / "var")

    def _smoke(self, commit: str) -> None:
        failed = self.tools.smoke(self.layout.release(commit))
        if failed:
            said = "the smoke tests failed in the staged checkout; nothing was swapped:"
            raise DeployRefused("\n  ".join([said, *failed]))

    def _swap(self, commit: str) -> None:
        _link(os.path.join(self.layout.home.name, "releases", commit), self.layout.root)

    def _record(self, outcome: str, was: str | None, to: str, restarted: Sequence[str]) -> None:
        journal = Journal(self.layout.state / "deploy" / "deploys.jsonl", fsync=True)
        journal.open()
        try:
            now = self._clock.now_ns()
            journal.write("deploy", at_ns=now, outcome=outcome, was=was, to=to, restarted=list(restarted))
            journal.flush()
        finally:
            journal.close()

    def _restart(self, steps: Steps, restarts: Sequence[str]) -> str:
        """Restart each unit, saying so first. Returns what could not be restarted, in words, or nothing."""
        failed = []
        for unit in restarts:
            try:
                steps.do(f"restart {unit}", lambda unit=unit: self.tools.units.restart(unit))  # type: ignore[misc]
            except DeployRefused as exc:
                failed.append(str(exc))
        return "; ".join(failed)

    def _say_trader(self, was: str | None) -> None:
        if self.tools.units.active(TRADER):
            self._say(
                f"{TRADER} is running and was NOT restarted: it keeps running {str(was)[:12]} until you restart it. "
                "That is yours to do, when you choose"
            )

    # ---- the two commands ----------------------------------------------------------------------------------------

    def run(self, request: Request) -> int:
        layout, steps = self.layout, Steps(self._say, request.dry_run)
        was = layout.current()
        if was is None:
            said = f"{layout.root} is not a link into {layout.releases}"
            raise DeployRefused(f"{said}: run `adopt` once (docs/runbooks/deploy.md)")
        steps.do(
            "fetch main from origin into the mirror; only a fast-forward is taken",
            lambda: self._fetch(),
        )
        if request.dry_run:
            self._say("dry run: main was not fetched, so the commit is judged by the mirror as it stands")
        commit, on_main = self._resolve(request.commit)
        if commit == was:
            self._say(f"{commit[:12]} is in service already: nothing to do")
            return EXIT_OK
        named = list(request.restart) if request.restart is not None else self.running()
        restarts = [unit for unit in named if unit not in RESTARTABLE or self.tools.units.active(unit)]
        for unit in sorted(set(named) - set(restarts)):
            self._say(f"{unit} is not running: it is not started, and so not restarted")
        self._gate(self.facts(commit, on_main, request, restarts, look_up=True))
        self._say(f"the gate is open: {was[:12]} to {commit[:12]}; to restart: {', '.join(restarts) or 'nothing'}")
        with_ml = (layout.root / ".venv" / ML_MARK).exists()
        staging = layout.release(commit)
        steps.do(f"check out {commit[:12]} into the staging folder {staging}", lambda: self._stage(commit))
        steps.do(
            "build its environment from the hash-locked requirement files, or reuse the one built from the same "
            "files; link the environment and the state directory into it",
            lambda: self._environment(commit, with_ml),
        )
        steps.do("run the smoke tests in the staged checkout, with its own interpreter", lambda: self._smoke(commit))
        if not request.dry_run:  # staging took a while, and now the unit files of the commit can be read
            self._gate(self.facts(commit, on_main, request, restarts, look_up=False))
        steps.do("look at the platform's status, to compare with afterwards", self.tools.health.note)
        steps.do(
            f"write down that {was[:12]} is what to go back to",
            lambda: _write(layout.last, {"from": was, "to": commit}),
        )
        steps.do(f"swap {layout.root} from {was[:12]} to {commit[:12]}, in one rename", lambda: self._swap(commit))
        wrong = self._restart(steps, restarts)
        if request.dry_run:
            self._say("then: check the platform's status; if it is worse than before, swap back and restart again")
            return EXIT_OK
        wrong = wrong or self.tools.health.after(restarts)
        if wrong:
            self._say(f"the status check FAILED after the deploy: {wrong}. Going back to {was[:12]}")
            steps.do(f"swap {layout.root} back to {was[:12]}, in one rename", lambda: self._swap(was))
            again = self._restart(steps, restarts)
            if again:
                self._say(f"{was[:12]} is in service again, and a unit could NOT be restarted: {again}")
            self._record("rolled_back", was, commit, restarts)
            if self._sender is not None:
                text = f"The status check failed after the deploy of {commit[:12]}. {was[:12]} is in service again."
                self._sender(Page(f"{host.ALERT_PREFIX}deploy", "Deploy rolled back", text, 4))
            return EXIT_ROLLED_BACK
        self._record("deployed", was, commit, restarts)
        self._say(f"deployed: {commit[:12]} is in service. To go back: python -m hftlab.ops.deploy back")
        self._say_trader(was)
        return EXIT_OK

    def back(self, request: Request) -> int:
        """Put the checkout before this one in service again. The gate is asked all but the checks: it passed them."""
        layout, steps = self.layout, Steps(self._say, request.dry_run)
        now = layout.current()
        try:
            last = json.loads(layout.last.read_text(encoding="utf-8"))
            was, to = str(last["from"]), str(last["to"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DeployRefused("no deploy is on record here: there is nothing to go back to") from exc
        if now != to or not layout.release(was).is_dir():
            said = f"{str(now)[:12]} is in service and the last deploy was to {to[:12]}"
            raise DeployRefused(f"{said}: there is nothing to go back to")
        named = list(request.restart) if request.restart is not None else self.running()
        restarts = [unit for unit in named if unit not in RESTARTABLE or self.tools.units.active(unit)]
        self._gate(self.facts(was, True, request, restarts, look_up=False), checks_needed=False)
        steps.do("look at the platform's status, to compare with afterwards", self.tools.health.note)
        steps.do(f"swap {layout.root} back from {to[:12]} to {was[:12]}, in one rename", lambda: self._swap(was))
        wrong = self._restart(steps, restarts)
        if request.dry_run:
            return EXIT_OK
        wrong = wrong or self.tools.health.after(restarts)
        self._record("went_back", to, was, restarts)
        if wrong:
            self._say(f"{was[:12]} is in service again, and the status check FAILED: {wrong}. Nothing more is done")
            return EXIT_ROLLED_BACK
        self._say(f"{was[:12]} is in service again")
        self._say_trader(to)
        return EXIT_OK

    def _fetch(self) -> None:
        code, said = self.tools.git(["fetch", "--quiet", "origin", "refs/heads/main:refs/heads/main"])
        if code != 0:
            raise DeployRefused(f"main could not be fetched as a fast-forward: {said.strip()[-300:]}")


# ---- removing old checkouts ------------------------------------------------------------------------------------------


def deployed_order(layout: Layout) -> dict[str, int]:
    """Each commit the deploy journal names, with the number of the last line that put it in service or took it out.

    Raises `DeployRefused` when the journal does not verify: then which checkout is newer is not known.
    """
    try:
        entries = read_journal(layout.state / "deploy" / "deploys.jsonl")[1]
    except (JournalError, OSError) as exc:
        raise DeployRefused(f"the deploy journal cannot be read or does not verify: {exc}. Nothing is removed") from exc
    order: dict[str, int] = {}
    for number, entry in enumerate(entries):
        for key in ("was", "to"):
            if isinstance(entry.get(key), str):
                order[str(entry[key])] = number
    return order


def _env_of(release: Path) -> str | None:
    """The environment a checkout links to, by name; None when its `.venv` is not a link into `envs/`."""
    try:
        target = Path(os.readlink(release / ".venv"))
    except OSError:
        return None
    return target.name if target.parent.name == "envs" else None


def _remove(folder: Path, removing: Path) -> None:
    """Move the folder out in one rename, then remove it: it is whole in its place or it is not there."""
    removing.mkdir(exist_ok=True)
    away = removing / folder.name
    shutil.rmtree(away, ignore_errors=True)
    os.rename(folder, away)
    shutil.rmtree(away)


def prune(layout: Layout, *, keep_more: int, in_use: InUse, git: Git, say: Say, dry_run: bool = False) -> int:
    """Remove old checkouts and the environments no kept checkout links to. What it keeps is in the module's text."""
    steps = Steps(say, dry_run)
    current = layout.current()
    if current is None:
        raise DeployRefused(f"{layout.root} is not a link into {layout.releases}: there is nothing to prune here")
    if type(keep_more) is not int or keep_more < 0:
        raise DeployRefused(f"deploy.{KEEP_MORE_KEY} is a whole number, zero or more, not {keep_more!r}")
    why: dict[str, list[str]] = {current: ["in service"]}
    try:
        last = json.loads(layout.last.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}  # no deploy has been made here: there is nothing on record to go back to
    ends = (("from", "the one before the last swap: what `back` goes to"), ("to", "the last swap's other end"))
    for key, reason in ends:
        commit = last.get(key) if isinstance(last, dict) else None
        if isinstance(commit, str) and commit != current:
            why.setdefault(commit, []).append(reason)
    running = in_use()
    if running is None:
        raise DeployRefused(
            "where the running units have their working directories could not be looked up, so no checkout is "
            "known to be unused. Nothing is removed"
        )
    held = layout.releases.resolve()
    for who, folder in sorted(running.items()):
        inside = folder.resolve()
        if held in inside.parents:
            why.setdefault(inside.relative_to(held).parts[0], []).append(f"{who} runs in it")
    order = deployed_order(layout)
    present = sorted(path.name for path in layout.releases.iterdir() if path.is_dir() and not path.is_symlink())
    rest = sorted((name for name in present if name not in why), key=lambda name: (-order.get(name, -1), name))
    for name in rest[:keep_more]:
        why[name] = [f"one of the {keep_more} newest of the rest"]
    going = rest[keep_more:][::-1]  # the oldest first: whenever it is stopped, what is left is the newer part
    for name in present:
        say(f"keep {name[:12]}: {'; '.join(why[name])}" if name in why else f"REMOVE {name[:12]}")
    linked = {_env_of(layout.release(name)) for name in present if name in why}
    built = sorted(path.name for path in layout.envs.iterdir()) if layout.envs.is_dir() else []
    unused = [name for name in built if name not in linked]
    for name in built:
        said = f"keep environment {name[:12]}" if name in linked else f"REMOVE environment {name[:12]}"
        say(f"{said}: {'a kept checkout links to it' if name in linked else 'no kept checkout links to it'}")
    removing = layout.home / REMOVING

    def gone(folder: Path) -> Callable[[], None]:
        return lambda: _remove(folder, removing)
    left = sorted(path.name for path in removing.iterdir()) if removing.is_dir() else []
    if left:
        steps.do(
            f"finish removing what an earlier run left in {removing}: {', '.join(name[:12] for name in left)}",
            lambda: shutil.rmtree(removing),
        )
    for name in going:
        steps.do(f"remove the checkout {layout.release(name)}", gone(layout.release(name)))
    if going:
        steps.do("tell the mirror that those checkouts are gone", lambda: git(["worktree", "prune"]))
    for name in unused:
        steps.do(f"remove the environment {layout.envs / name}", gone(layout.envs / name))
    kept = len(present) - len(going)
    done = "would be removed" if dry_run else "removed"
    say(f"{len(going)} checkout(s) and {len(unused)} environment(s) {done}; {kept} checkout(s) kept")
    return EXIT_OK


# ---- adopting the install script's folder ----------------------------------------------------------------------------


def adopt(root: Path, any_active: Callable[[], Sequence[str]], say: Say, *, dry_run: bool = False) -> int:
    """Once: turn a plain checkout at the install root into the layout. Everything of ours must be stopped.

    Every step can be done again: a run that was killed is finished by running `adopt` once more.
    """
    layout, steps = Layout(root), Steps(say, dry_run)
    if layout.current() is not None:
        say(f"{root} is a link into {layout.releases} already: nothing to do")
        return EXIT_OK
    running = list(any_active())
    if running:
        raise DeployRefused(f"stop these first, adopting moves the folder they run in: {', '.join(running)}")
    plain = root.is_dir() and not root.is_symlink()
    moved = sorted(path.name for path in layout.releases.iterdir()) if layout.releases.is_dir() else []
    if plain:
        code, head = _program(["git", "-C", str(root), "rev-parse", "HEAD"])
        commit = head.strip() if code == 0 else ""
    else:
        commit = moved[0] if len(moved) == 1 and not root.exists() else ""
    if not _COMMIT.fullmatch(commit):
        raise DeployRefused(f"{root} is neither a git checkout nor a half-adopted one: nothing was changed")
    release = layout.release(commit)

    def mirror() -> None:
        if not layout.repo.is_dir():
            partial = layout.home / "repo.git.partial"
            shutil.rmtree(partial, ignore_errors=True)
            source = root if plain else release
            _must(["git", "clone", "--quiet", "--bare", str(source), str(partial)])
            code, origin = _program(["git", "-C", str(source), "config", "--get", "remote.origin.url"])
            if code == 0 and origin.strip():
                _must(["git", "--git-dir", str(partial), "remote", "set-url", "origin", origin.strip()])
            _must(["git", "--git-dir", str(partial), "update-ref", "refs/heads/main", commit])
            os.rename(partial, layout.repo)

    def state() -> None:
        inside = (root if plain else release) / "var"
        if not layout.state.exists():
            if inside.is_dir() and not inside.is_symlink():
                os.rename(inside, layout.state)
            else:
                layout.state.mkdir()
                _own(layout.state)
        if not inside.is_symlink():
            _link(os.path.join("..", "..", "var"), inside)

    def move() -> None:
        if plain and root.is_dir() and not root.is_symlink():
            os.rename(root, release)

    steps.do(f"make {layout.releases} and {layout.envs}", lambda: _made(layout.releases, layout.envs))
    steps.do(f"make the mirror {layout.repo} from the checkout, pointing at its origin", mirror)
    steps.do(f"move the state directory out to {layout.state} and leave a link to it in the checkout", state)
    steps.do(f"move the checkout to {release}", move)
    steps.do(f"make {root} a link to it", lambda: _link(os.path.join(layout.home.name, "releases", commit), root))
    say("adopted. Start what was running, then deploy with `run`" if not dry_run else "dry run: nothing was changed")
    return EXIT_OK


def _made(*folders: Path) -> None:
    for path in folders:
        path.mkdir(parents=True, exist_ok=True)
        _own(path)
        _own(path.parent)


# ---- the real tools --------------------------------------------------------------------------------------------------


def _program(
    command: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[int, str]:
    """Start a program and wait for it: (exit code, what it printed). As the platform's user when this is root."""
    as_user: dict[str, Any] = {"user": host.USER, "group": host.GROUP} if os.geteuid() == 0 else {}
    try:
        done = subprocess.run(
            list(command), cwd=cwd, env=None if env is None else dict(env), capture_output=True, text=True,
            check=False, timeout=1_800, **as_user,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{command[0]}: {type(exc).__name__}"
    return done.returncode, done.stdout + done.stderr


def _must(command: Sequence[str]) -> None:
    code, said = _program(command)
    if code != 0:
        raise DeployRefused(f"{' '.join(command[:3])} failed: {said.strip()[-300:]}")


def git_in(layout: Layout) -> Git:
    return lambda words: _program(["git", "--git-dir", str(layout.repo), *words])


def parse_checks(text: str) -> dict[str, str] | None:
    """A commit's checks by name from the hosting service's answer: the conclusion of the latest run of each."""
    try:
        runs = json.loads(text)["check_runs"]
        latest: dict[str, tuple[int, str]] = {}
        for run in runs:
            name, number = str(run["name"]), int(run["id"])
            if name not in latest or number > latest[name][0]:
                latest[name] = (number, str(run.get("conclusion") or run.get("status") or "unknown"))
    except (ValueError, KeyError, TypeError):
        return None
    return {name: found for name, (_, found) in latest.items()}


def gh_checks(layout: Layout) -> Checks:
    """Ask the `gh` program, when the host has it and it answers. None otherwise: the gate then asks the owner."""

    def look(commit: str) -> dict[str, str] | None:
        if shutil.which("gh") is None:
            return None
        code, origin = _program(["git", "--git-dir", str(layout.repo), "config", "--get", "remote.origin.url"])
        match = _SLUG.fullmatch(origin.strip()) if code == 0 else None
        if match is None:
            return None
        code, said = _program(["gh", "api", f"repos/{match['slug']}/commits/{commit}/check-runs?per_page=100"])
        return parse_checks(said) if code == 0 else None

    return look


def uv_build(release: Path, env: Path, with_ml: bool) -> None:
    """A new environment holding exactly what the checkout's lock files name, every package checked by its hash."""
    uv = shutil.which("uv")
    if uv is None:
        raise DeployRefused("uv was not found: the environment cannot be built")
    _must([uv, "venv", "--quiet", "--python", "3.12", str(env)])
    python = str(env / "bin" / "python")
    install = [uv, "pip", "install", "--quiet", "--require-hashes", "--python", python, "-r"]
    _must([*install, str(release / LOCK_FILE)])
    if with_ml:
        _must([*install, str(release / ML_LOCK_FILE)])
        (env / ML_MARK).write_text("ml\n", encoding="utf-8")


def smoke_commands() -> list[tuple[str, ...]]:
    """What is run in a staged checkout, after the interpreter: each unit's module, and three deeper looks.

    `--help` makes a command import everything it needs and stop. The units are compared with the rendered
    files beside them, the trader reads its settings and its stage journal, and the status command reads every
    settings file it shares with the publisher, the backup and the report.
    """
    every = (*units.SERVICES, *units.weekend(*units.weekend_times()))
    modules = sorted({words[1] for spec in every for words in (spec.command, spec.first, spec.then) if words})
    return [
        *((("-m", module, "--help")) for module in modules),
        ("-m", "hftlab.ops.units", "diff", "--unit-dir", "deploy/systemd"),
        ("-m", "hftlab.trader", "--stages"),
        ("-m", "hftlab.ops.status", "--quiet"),
    ]


def run_smoke(release: Path, python: Path | None = None) -> list[str]:
    """Run the smoke commands in a checkout with its interpreter and a scratch state directory. What failed."""
    failed = []
    interpreter = str(python or release / ".venv" / "bin" / "python")
    with tempfile.TemporaryDirectory(prefix="hft-smoke-") as scratch:
        os.chmod(scratch, 0o755)  # the platform's user must be able to enter it when this command is root
        env = {
            "PATH": os.environ.get("PATH", ""), "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1",
            "HFT_STATE_DIR": scratch, "TZ": "UTC",
        }  # fmt: skip
        for words in smoke_commands():
            journals = ("--journals", scratch) if words[1] == "hftlab.trader" else ()
            code, said = _program([interpreter, *words, *journals], cwd=release, env=env)
            if code not in (0, *((1, 3) if words[1] == "hftlab.ops.status" else ())):  # the status may find problems
                last = said.strip().splitlines()[-1:] or [""]
                failed.append(f"{' '.join(words)}: exit {code}: {last[0][:200]}")
    return failed


class Systemctl:
    """The service manager itself. Only ever handed a unit of this platform's own."""

    def active(self, unit: str) -> bool:
        return subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0

    def restart(self, unit: str) -> None:
        if not unit.startswith(host.UNIT_PREFIX) or unit == TRADER:
            raise DeployRefused(f"{unit} is not restarted by a deploy")
        if subprocess.run(["systemctl", "restart", unit], check=False).returncode != 0:
            raise DeployRefused(f"systemctl restart {unit} failed")


def wait(seconds: float) -> None:
    """Let this long pass. It reads no clock: nothing here decides anything by the time."""
    threading.Event().wait(seconds)


class StatusHealth:
    """The platform's own status check, before and after: a problem it calls bad that was not there before fails."""

    def __init__(
        self, layout: Layout, manager: Units, *, settle_s: int = SETTLE_S, sleep: Callable[[float], None] = wait
    ) -> None:
        self._layout, self._units, self._settle_s, self._sleep = layout, manager, settle_s, sleep
        self._before: set[str] = set()

    def _bad(self) -> set[str] | None:
        python = str(self._layout.root / ".venv" / "bin" / "python")
        env = {
            "PATH": os.environ.get("PATH", ""), "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1", "TZ": "UTC",
            **{name: value for name, value in host.settings().items() if value},
        }  # fmt: skip
        _, said = _program([python, "-m", "hftlab.ops.status", "--json"], cwd=self._layout.root, env=env)
        try:
            return {str(p["code"]) for p in json.loads(said[said.index("{"):])["problems"] if p["level"] == "bad"}
        except (ValueError, KeyError, TypeError):
            return None  # it did not run, or said something that is not a reading

    def note(self) -> None:
        self._before = self._bad() or set()

    def after(self, restarted: Sequence[str]) -> str:
        wrong = "the status was not looked at"
        for _ in range(max(1, self._settle_s // 5)):
            down = [unit for unit in restarted if not self._units.active(unit)]
            bad = self._bad()
            if bad is None:
                wrong = "the status command of the new checkout did not run"
            elif down:
                wrong = f"not running after the restart: {', '.join(down)}"
            elif bad - self._before:
                wrong = f"problems that were not there before: {', '.join(sorted(bad - self._before))}"
            else:
                return ""
            self._sleep(5)
        return wrong


def _main_pid(unit: str) -> int | None:
    """The main process of a unit, 0 when it is not running. None: the service manager could not be asked."""
    try:
        done = subprocess.run(
            ["systemctl", "show", "--property=MainPID", "--value", unit], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    said = done.stdout.strip()
    return int(said) if done.returncode == 0 and said.isdecimal() else None


def working_directories(
    proc: Path = Path("/proc"), main_pid: Callable[[str], int | None] = _main_pid
) -> dict[str, Path] | None:
    """Where each running unit of ours has its working directory, and every other process that can be seen.

    None when it cannot be known for a unit: the service manager did not answer, or its process's folder could
    not be read. A process that is not a unit's (a command started by hand, a child) is listed when its folder
    can be read, and passed over when it cannot.
    """
    found: dict[str, Path] = {}
    seen: set[int] = set()
    for name in sorted(units.render_all()):
        if not name.endswith(".service"):
            continue
        pid = main_pid(name)
        if pid is None:
            return None
        if pid:
            try:
                found[name] = Path(os.readlink(proc / str(pid) / "cwd"))
            except OSError:
                return None
            seen.add(pid)
    try:
        others = sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdecimal())
    except OSError:
        return None
    for pid in others:
        if pid not in seen:
            with contextlib.suppress(OSError):
                found[f"process {pid}"] = Path(os.readlink(proc / str(pid) / "cwd"))
    return found


def keep_more(layout: Layout) -> int:
    """How many old checkouts `prune` keeps beside the ones it must: `deploy.keep_more_releases` of config/ops.yaml."""
    path = layout.root / "config" / "ops.yaml"
    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeployRefused(f"config/ops.yaml of the checkout in service cannot be read: {exc}") from exc
    block = loaded.get("deploy") if isinstance(loaded, dict) else None
    number = block.get(KEEP_MORE_KEY) if isinstance(block, dict) else None
    if type(number) is not int or number < 0:
        raise DeployRefused(f"config/ops.yaml: deploy.{KEEP_MORE_KEY} must be a whole number, zero or more")
    return number


def _our_units_running() -> list[str]:
    names = [name for name in units.render_all() if name != host.SLICE]
    manager = Systemctl()
    return [name for name in names if manager.active(name)]


# ---- the command -----------------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, clock: Clock | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hftlab.ops.deploy", description="Change the code on the host.")
    parser.add_argument("--root", type=Path, default=Path(host.INSTALL_ROOT), help="the install root")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="deploy a commit of main")
    run.add_argument("--commit", required=True, help="the commit to deploy, by its hash")
    run.add_argument(
        "--checks-verified-by-owner", action="store_true",
        help="you looked: test and leakcheck are green on this commit. For when they cannot be looked up",
    )  # fmt: skip
    back = commands.add_parser("back", help="put the checkout before this one in service again")
    for one in (run, back):
        one.add_argument("--restart", action="append", metavar="UNIT",
                         help="restart this unit after the swap; may be given several times. Default: the recorder "
                              "and the shadow runner, each if it is running. Never the trader")  # fmt: skip
        one.add_argument("--dry-run", action="store_true", help="say every step and do none")
        one.add_argument("--unit-dir", type=Path, default=Path(host.UNIT_DIR), help="where the installed units are")
    commands.add_parser("smoke", help="run the smoke tests in this checkout")
    adopting = commands.add_parser("adopt", help="once: turn the install script's folder into the layout")
    adopting.add_argument("--dry-run", action="store_true")
    pruning = commands.add_parser("prune", help="remove old checkouts and the environments nothing uses")
    pruning.add_argument("--dry-run", action="store_true", help="say what would be kept and removed; remove nothing")
    args = parser.parse_args(argv)

    def say(text: str) -> None:
        print(f"deploy: {text}", flush=True)

    try:
        if args.command == "smoke":
            failed = run_smoke(Path.cwd(), Path(sys.executable))
            say("\n".join(["the smoke tests FAILED:", *failed]) if failed else "the smoke tests passed")
            return EXIT_REFUSED if failed else EXIT_OK
        if args.command == "adopt":
            return adopt(args.root, _our_units_running, say, dry_run=args.dry_run)
        layout, manager = Layout(args.root), Systemctl()
        if args.command == "prune":
            with contextlib.ExitStack() as stack:
                if not args.dry_run:
                    stack.enter_context(one_at_a_time(layout))
                return prune(
                    layout, keep_more=keep_more(layout), in_use=working_directories, git=git_in(layout), say=say,
                    dry_run=args.dry_run,
                )  # fmt: skip
        tools = Tools(git_in(layout), gh_checks(layout), uv_build, run_smoke, manager, StatusHealth(layout, manager))
        sender = sender_from(host.settings(), lambda line: print(line, file=sys.stderr, flush=True))
        deploy = Deploy(layout, tools, clock or WallClock(), say, sender)
        with contextlib.ExitStack() as stack:
            if not args.dry_run:
                stack.enter_context(one_at_a_time(layout))
            if args.command == "back":
                return deploy.back(Request("", args.restart, False, args.dry_run, args.unit_dir))
            request = Request(args.commit, args.restart, args.checks_verified_by_owner, args.dry_run, args.unit_dir)
            return deploy.run(request)
    except DeployRefused as exc:
        print("deploy: REFUSED. What is in service was not changed.", file=sys.stderr)
        for line in str(exc).splitlines():
            print(f"deploy:   {line}", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
