"""The deploy gate: what it refuses, what it does in which order, and that a kill never leaves half a checkout.

Everything runs in a folder under tmp_path, with git itself against a repository beside it. The service
manager, the checks' look-up, the environment build, the smoke run and the status check are stand-ins: no
network, no root, no systemd.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import sys

import pytest

from hftlab.core import SimClock
from hftlab.journal import Journal, read
from hftlab.ops import deploy, units
from hftlab.ops.deploy import (
    RECORDER,
    TRADER,
    Deploy,
    DeployRefused,
    Facts,
    Layout,
    Request,
    Tools,
    adopt,
    broker_book,
    refusals,
)
from tests.faults.kit import AFTER, BEFORE, Killed
from tests.support import ROOT, GitRepo
from tests.trader.kit import Desk, put_stage

S = 1_000_000_000
SATURDAY = 1_760_180_400 * S  # 2025-10-11 11:00 UTC: the market is shut
WEDNESDAY = 1_760_529_600 * S  # 2025-10-15 12:00 UTC: open, nine hours before the rollover
ROLLOVER = 1_760_562_000 * S  # 2025-10-15 21:00 UTC: 17:00 in New York
SHADOW = "hft-shadow.service"
GREEN = {"test": "success", "leakcheck": "success"}


class Manager:
    """Stands where the service manager would be: it knows what runs, and keeps what it was asked to restart."""

    def __init__(self, *running: str) -> None:
        self.running, self.restarted, self.fails = set(running), [], set()

    def active(self, unit: str) -> bool:
        return unit in self.running

    def restart(self, unit: str) -> None:
        if unit in self.fails:
            raise DeployRefused(f"systemctl restart {unit} failed")
        self.restarted.append(unit)


class Status:
    """The status check: it says what it is told to say, and keeps the order it was asked in."""

    def __init__(self, *answers: str) -> None:
        self.answers, self.asked = list(answers), []

    def note(self) -> None:
        self.asked.append("note")

    def after(self, restarted) -> str:
        self.asked.append(("after", tuple(restarted)))
        return self.answers.pop(0) if self.answers else ""


class Site:
    """A host folder made by the install script's layout, adopted, with a repository to fetch from beside it."""

    def __init__(self, tmp_path, *, now_ns: int = SATURDAY, running=(RECORDER, SHADOW), adopted: bool = True) -> None:
        self.origin = GitRepo(tmp_path / "origin")
        self.origin.git("config", "user.email", "t@example.invalid")
        self.origin.git("config", "user.name", "t")
        self.origin.add("requirements.lock.txt", "a==1\n")
        self.origin.add("src/code.txt", "one\n")
        self.origin.add("config/trader.yaml", "state_dir: var/trader\n")
        for path in sorted((ROOT / "config" / "calendar").iterdir()):
            self.origin.add(f"config/calendar/{path.name}", path.read_bytes())
        self.origin.add("deploy/systemd/hft-recorder.service", "the recorder's unit, first version\n")
        self.one = self.origin.commit("one")
        self.origin.add("src/code.txt", "two\n")
        self.two = self.origin.commit("two")
        self.root = tmp_path / "host" / "hft-lab"
        self.root.parent.mkdir()
        clone = GitRepo.__new__(GitRepo)
        clone.root = self.root.parent
        clone.git("clone", "-q", str(self.origin.root), str(self.root))
        clone.root = self.root
        clone.git("checkout", "-q", "--detach", self.one)
        (self.root / "var" / "record").mkdir(parents=True)
        (self.root / "var" / "record" / "kept").write_text("a recording\n")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "made-by").write_text("the install script\n")
        self.layout = Layout(self.root)
        self.manager, self.status, self.said, self.pages = Manager(*running), Status(), [], []
        self.checks, self.built, self.smoke_fails = dict(GREEN), [], []
        self.clock = SimClock(now_ns)
        if adopted:
            assert adopt(self.root, lambda: [], self.said.append) == 0
            self.said.clear()

    def tools(self) -> Tools:
        def build(release, env, with_ml) -> None:
            env.mkdir(parents=True)
            (env / "built-from").write_text((release / "requirements.lock.txt").read_text() + f"ml={int(with_ml)}\n")
            self.built.append(env.name)

        return Tools(
            deploy.git_in(self.layout), lambda commit: self.checks, build,
            lambda release: list(self.smoke_fails), self.manager, self.status,
        )  # fmt: skip

    def deploy(self) -> Deploy:
        send = lambda page: self.pages.append(page) or True  # noqa: E731
        return Deploy(self.layout, self.tools(), self.clock, self.said.append, send)

    def run(self, commit: str | None = None, **options) -> int:
        return self.deploy().run(Request(commit or self.two, **options))

    def code(self) -> str:
        return (self.root / "src" / "code.txt").read_text().strip()

    def snapshot(self) -> dict[str, str]:
        """Every file and link under the host folder, by content: what a step that changes nothing leaves alone."""
        out = {}
        for path in sorted(self.root.parent.rglob("*")):
            name = path.relative_to(self.root.parent).as_posix()
            if "repo.git" in name or name.endswith("deploy.lock"):
                continue
            if path.is_symlink():
                out[name] = f"link:{path.readlink()}"
            elif path.is_file():
                out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    def whole(self) -> str:
        """The checkout in service, checked to be whole: its code, its environment and the state through it."""
        commit = self.layout.current()
        assert commit in (self.one, self.two), "the root links to a release"
        assert self.code() == ("one" if commit == self.one else "two")
        assert (self.root / "var" / "record" / "kept").read_text() == "a recording\n", "the state, through the link"
        assert (self.root / ".venv").is_dir() and (self.root / "requirements.lock.txt").is_file()
        return commit


# ---- adopting --------------------------------------------------------------------------------------------------------


def test_adopting_turns_the_plain_checkout_into_a_link_and_moves_the_state_out_without_losing_any(tmp_path):
    site = Site(tmp_path, adopted=False)
    assert site.layout.current() is None and not site.root.is_symlink()
    with pytest.raises(DeployRefused, match="stop these first"):
        adopt(site.root, lambda: [RECORDER], site.said.append)
    assert not site.root.is_symlink(), "refused with nothing changed"
    assert adopt(site.root, lambda: [], site.said.append, dry_run=True) == 0 and not site.root.is_symlink()
    assert all("(dry run: not done)" in line for line in site.said[:-1]) and len(site.said) == 6
    assert adopt(site.root, lambda: [], site.said.append) == 0
    assert site.root.is_symlink() and site.whole() == site.one
    assert (site.layout.state / "record" / "kept").is_file() and (site.root / "var").is_symlink()
    assert (site.root / ".venv" / "made-by").is_file(), "the environment the install script built stays in service"
    assert deploy.git_in(site.layout)(["rev-parse", "refs/heads/main"])[1].strip() == site.one
    assert adopt(site.root, lambda: [], site.said.append) == 0 and "nothing to do" in site.said[-1]


@pytest.mark.parametrize("how", [BEFORE, AFTER])
def test_an_adoption_killed_at_any_step_is_finished_by_running_it_again_and_no_state_is_lost(
    tmp_path, monkeypatch, how
):
    for at in range(1, 6):
        site = Site(tmp_path / f"{how}-{at}", adopted=False)
        kill_at(monkeypatch, at, how)
        with pytest.raises(Killed):
            adopt(site.root, lambda: [], site.said.append)
        monkeypatch.undo()
        kept = [path for path in site.root.parent.rglob("kept") if not any(p.is_symlink() for p in path.parents)]
        assert len(kept) == 1 and kept[0].read_text() == "a recording\n", f"{how} step {at}: the state is in one place"
        assert adopt(site.root, lambda: [], site.said.append) == 0, f"{how} step {at}"
        assert site.whole() == site.one, f"{how} step {at}"


def kill_at(monkeypatch, at: int, how: str) -> None:
    """Make the process die at step `at` of whatever is run next: before the step is done, or just after."""
    do = deploy.Steps.do

    def dying(self, what, action):
        if self.count + 1 == at and how == BEFORE:
            raise Killed(what)
        do(self, what, action)
        if self.count == at and how == AFTER:
            raise Killed(what)

    monkeypatch.setattr(deploy.Steps, "do", dying)


# ---- a deploy --------------------------------------------------------------------------------------------------------


def test_a_deploy_stages_builds_smokes_swaps_restarts_and_checks_and_says_each_step_before_it_does_it(tmp_path):
    site = Site(tmp_path)
    old_release = {name: found for name, found in site.snapshot().items() if f"releases/{site.one}/" in name}
    assert site.run() == 0
    assert site.layout.current() == site.two and site.whole() == site.two
    steps = [line for line in site.said if line.startswith("step ")]
    assert [line.split(": ", 1)[1].split(" ")[0] for line in steps] == [
        "fetch", "check", "build", "run", "look", "write", "swap", "restart", "restart",
    ]  # fmt: skip
    assert [line.split(":")[0] for line in steps] == [f"step {n}" for n in range(1, 10)]
    assert site.manager.restarted == [RECORDER, SHADOW] and site.status.asked == ["note", ("after", (RECORDER, SHADOW))]
    assert {name: found for name, found in site.snapshot().items() if f"releases/{site.one}/" in name} == old_release, (
        "the checkout that was in service is still there, untouched: it is what going back goes to"
    )
    env = deploy.lock_hash(site.layout.release(site.two), False)
    assert site.built == [f"{env}.partial"] and (site.root / ".venv").resolve() == (site.layout.envs / env).resolve()
    assert (site.layout.envs / env / ".complete").read_text() == env + "\n"
    assert json.loads(site.layout.last.read_text()) == {"from": site.one, "to": site.two}
    (line,) = read(site.layout.state / "deploy" / "deploys.jsonl")[1]
    assert (line["outcome"], line["was"], line["to"]) == ("deployed", site.one, site.two)
    assert line["restarted"] == [RECORDER, SHADOW]
    assert site.run() == 0 and "is in service already: nothing to do" in site.said[-1]
    assert site.manager.restarted == [RECORDER, SHADOW], "and nothing is restarted for nothing"


def test_an_environment_built_from_the_same_lock_files_is_reused_and_another_lock_file_builds_another(tmp_path):
    site = Site(tmp_path)
    site.run()
    site.origin.add("src/code.txt", "three\n")
    three = site.origin.commit("three")
    assert site.run(three) == 0 and len(site.built) == 1, "the same lock file: the environment is linked, not built"
    site.origin.add("requirements.lock.txt", "a==2\n")
    four = site.origin.commit("four")
    assert site.run(four) == 0 and len(site.built) == 2
    assert (site.root / ".venv" / "built-from").read_text() == "a==2\nml=0\n"
    assert sorted(path.name for path in site.layout.releases.iterdir()) == sorted([site.one, site.two, three, four])


def test_a_dry_run_says_every_step_and_does_none_of_them(tmp_path):
    site = Site(tmp_path)
    with pytest.raises(DeployRefused, match="is not a commit of main"):
        site.run(dry_run=True)  # the mirror's main is where it was: a dry run fetches nothing, and says so
    assert "dry run: main was not fetched, so the commit is judged by the mirror as it stands" in site.said
    site.said.clear()
    site.tools().git(["fetch", "--quiet", "origin", "refs/heads/main:refs/heads/main"])
    before = site.snapshot()
    assert site.run(dry_run=True) == 0
    assert site.snapshot() == before and site.manager.restarted == [] and site.status.asked == []
    steps = [line for line in site.said if line.startswith("step ")]
    assert len(steps) == 9 and all(line.endswith("(dry run: not done)") for line in steps)
    assert site.layout.current() == site.one and not site.layout.last.exists()


def test_a_staged_checkout_that_fails_its_smoke_tests_is_never_put_in_service(tmp_path):
    site = Site(tmp_path)
    site.smoke_fails = ["-m hftlab.record --help: exit 1: ModuleNotFoundError"]
    with pytest.raises(DeployRefused, match="the smoke tests failed in the staged checkout; nothing was swapped"):
        site.run()
    assert site.whole() == site.one and site.manager.restarted == [] and not site.layout.last.exists()
    site.smoke_fails = []
    assert site.run() == 0 and site.whole() == site.two, "the folder the failed deploy left is made again"


def test_when_the_status_check_fails_after_the_swap_it_goes_back_by_itself_and_pages(tmp_path):
    site = Site(tmp_path)
    site.status = Status("problems that were not there before: recorder_stale")
    assert site.run() == 1
    assert site.whole() == site.one, "the checkout from before is in service again"
    assert site.manager.restarted == [RECORDER, SHADOW, RECORDER, SHADOW], "restarted onto the new, then onto the old"
    (page,) = site.pages
    assert (page.key, page.title, page.priority) == ("hft:deploy", "Deploy rolled back", 4)
    assert site.two[:12] in page.message and site.one[:12] in page.message
    (line,) = read(site.layout.state / "deploy" / "deploys.jsonl")[1]
    assert line["outcome"] == "rolled_back"
    swaps = [line for line in site.said if "swap" in line and line.startswith("step ")]
    assert len(swaps) == 2 and "back to" in swaps[1]


def test_a_unit_that_cannot_be_restarted_after_the_swap_is_a_failed_deploy_and_not_a_refusal(tmp_path):
    site = Site(tmp_path)
    site.manager.fails = {SHADOW}
    assert site.run() == 1 and site.whole() == site.one
    assert site.status.asked == ["note"], "the status was not even asked: a unit that will not restart is enough"
    assert any("could NOT be restarted" in line for line in site.said)


@pytest.mark.parametrize("how", [BEFORE, AFTER])
def test_a_kill_at_any_step_leaves_the_old_checkout_or_the_new_one_whole_and_the_next_deploy_completes(
    tmp_path, monkeypatch, how
):
    for at in range(1, 10):
        site = Site(tmp_path / f"{how}-{at}")
        old = {name: found for name, found in site.snapshot().items() if f"releases/{site.one}/" in name}
        kill_at(monkeypatch, at, how)
        with pytest.raises(Killed):
            site.run()
        monkeypatch.undo()
        said = f"killed {how} step {at}"
        in_service = site.whole()
        swapped = at > 7 or (at == 7 and how == AFTER)
        assert in_service == (site.two if swapped else site.one), said
        assert {n: f for n, f in site.snapshot().items() if f"releases/{site.one}/" in n} == old, said
        assert not list(site.root.parent.glob("hft-lab.next")), f"{said}: no half-made link is left in the way"
        site.said.clear()
        assert site.run() == 0 and site.whole() == site.two, said


def test_going_back_swaps_to_the_checkout_before_and_only_once(tmp_path):
    site = Site(tmp_path)
    with pytest.raises(DeployRefused, match="no deploy is on record"):
        site.deploy().back(Request(""))
    site.run()
    site.manager.restarted.clear()
    assert site.deploy().back(Request("", dry_run=True)) == 0 and site.whole() == site.two
    assert site.deploy().back(Request("")) == 0
    assert site.whole() == site.one and site.manager.restarted == [RECORDER, SHADOW]
    record = read(site.layout.state / "deploy" / "deploys.jsonl")[1]
    assert [line["outcome"] for line in record] == ["deployed", "went_back"]
    with pytest.raises(DeployRefused, match="there is nothing to go back to"):
        site.deploy().back(Request(""))
    assert site.run() == 0 and site.whole() == site.two, "and the commit can be deployed again"


# ---- the gate --------------------------------------------------------------------------------------------------------


def facts(**changes) -> Facts:
    plain = {
        "commit": "c" * 40, "on_main": True, "checks": GREEN, "owner_verified": False, "held": {}, "working": (),
        "trader_running": False, "market_open": True, "restarts": (RECORDER,), "in_rollover_window": False,
    }  # fmt: skip
    return Facts(**{**plain, **changes})


def test_the_gate_is_open_when_everything_holds_and_each_thing_that_does_not_is_a_refusal_of_its_own():
    assert refusals(facts()) == []
    cases = {
        "is not a commit of main": {"on_main": False},
        "the check `test` of cccccccccccc is failure": {"checks": {"test": "failure", "leakcheck": "success"}},
        "the check `leakcheck` of cccccccccccc is missing": {"checks": {"test": "success"}},
        "the check `test` of cccccccccccc is in_progress": {"checks": {"test": "in_progress", "leakcheck": "success"}},
        "could not be looked up": {"checks": None},
        "whether the broker book is flat is not known": {"held": None},
        "the broker book is not flat: control EUR.USD +20000": {"held": {"control EUR.USD": 20_000}},
        "1 order(s) have not ended": {"working": ("hft-control-1",)},
        "the trader is running and the market is open": {"trader_running": True},
        "hft-trader.service is not restarted by a deploy": {"restarts": (TRADER,)},
        "inside the rollover window": {"in_rollover_window": True},
        "are not the ones installed: hft-recorder.service": {"units_differ": ("hft-recorder.service",)},
    }
    for said, change in cases.items():
        (found,) = refusals(facts(**change))
        assert said in found, found
    assert len(refusals(facts(on_main=False, held={"ml USD.JPY": -1}, trader_running=True))) == 3, "every one is said"


def test_the_owners_word_stands_in_for_a_look_up_that_could_not_be_made_and_never_for_a_check_that_is_red():
    assert refusals(facts(checks=None, owner_verified=True)) == []
    red = refusals(facts(checks={"test": "failure", "leakcheck": "success"}, owner_verified=True))
    assert len(red) == 1 and "is failure, not success" in red[0]
    assert deploy.REQUIRED_CHECKS == ("test", "leakcheck")


def test_what_else_is_allowed_a_stopped_recorder_in_the_window_and_a_running_trader_with_the_market_shut():
    assert refusals(facts(in_rollover_window=True, restarts=(SHADOW,))) == [], "no recorder restart, no hole"
    assert refusals(facts(trader_running=True, market_open=False)) == []
    assert refusals(facts(on_main=False, checks=None), checks_needed=False) == [], "going back: it passed them once"
    assert "is not flat" in refusals(facts(held={"control EUR.USD": 1}), checks_needed=False)[0]


def test_a_deploy_is_refused_on_red_checks_and_passes_on_the_owners_word_when_they_cannot_be_looked_up(tmp_path):
    site = Site(tmp_path)
    site.checks = {"test": "success", "leakcheck": "failure"}
    with pytest.raises(DeployRefused, match=r"the check `leakcheck` of .* is failure"):
        site.run(owner_verified=True)
    site.checks = None
    with pytest.raises(DeployRefused, match="--checks-verified-by-owner"):
        site.run()
    assert site.whole() == site.one and not site.layout.release(site.two).exists(), "refused before staging"
    assert site.run(owner_verified=True) == 0


def test_a_commit_that_is_not_on_main_or_is_named_by_a_branch_is_refused(tmp_path):
    site = Site(tmp_path)
    site.origin.git("checkout", "-q", "-b", "side")
    site.origin.add("src/code.txt", "not reviewed\n")
    side = site.origin.commit("side")
    with pytest.raises(DeployRefused, match="is not a commit the mirror holds"):
        site.run(side)  # only main is fetched: a commit of another branch never arrives
    with pytest.raises(DeployRefused, match="by its hash, not by a branch or a tag"):
        site.run("main")
    site.tools().git(["fetch", "--quiet", "origin", "refs/heads/side:refs/heads/side"])
    with pytest.raises(DeployRefused, match="is not a commit of main"):
        site.run(side)
    assert site.whole() == site.one


def an_outbox_with_a_position(folder) -> Desk:
    """A real trader on paper, stopped while its control sleeve holds a position in the broker book."""
    put_stage(folder, "control", "min_size")
    desk = Desk(folder)
    for _ in range(40):
        desk.run(1)
        if desk.held()[0]:
            break
    assert desk.held()[0] != 0
    desk.trader.abandon()
    return desk


def test_with_a_position_open_in_the_broker_book_nothing_is_deployed_and_nothing_is_restarted(tmp_path):
    site = Site(tmp_path)
    desk = an_outbox_with_a_position(site.layout.state / "trader")
    held, _ = broker_book(desk.outbox())
    assert held == {"control EUR.USD": desk.held()[0]} == {"control EUR.USD": desk.positions()["EUR.USD"]}, (
        "what the outbox's fills add up to is what the order manager's book holds and what the broker holds"
    )
    with pytest.raises(DeployRefused, match=r"the broker book is not flat: control EUR\.USD"):
        site.run()
    assert site.whole() == site.one and site.manager.restarted == []
    with pytest.raises(DeployRefused, match="the broker book is not flat"):
        site.run(restart=[])
    desk.start()
    for _ in range(40):
        desk.run(1, quotes=True)
        if not desk.held()[0] and not broker_book(desk.outbox())[1]:
            break
    desk.stop()
    assert broker_book(desk.outbox()) == ({}, []), "flat again, and every order has ended"
    assert site.run() == 0


def test_an_order_that_has_not_ended_and_a_fill_seen_twice_are_read_from_the_outbox_as_they_are():
    order = "hft-control-EURUSD-B-1-entry-0"
    intent = {"type": "intent", "client_order_id": order}
    fill = {"type": "fill", "fill_id": "F1", "client_order_id": order, "pair": "EUR.USD", "side": "SELL", "quantity": 5}
    assert broker_book([intent]) == ({}, [order])
    assert broker_book([intent, {"type": "stage", "client_order_id": order, "to": "WORKING"}])[1] == [order]
    assert broker_book([intent, {"type": "stage", "client_order_id": order, "to": "CANCELLED"}])[1] == []
    held, _ = broker_book([intent, fill, dict(fill), {**fill, "fill_id": "F2", "side": "BUY", "quantity": 2}])
    assert list(held.values()) == [-3], "a fill is counted once, by its id"
    assert broker_book([fill, {**fill, "fill_id": "F2", "side": "BUY"}])[0] == {}, "flat lines are left out"


def test_an_outbox_that_does_not_verify_is_not_flat_it_is_refused(tmp_path):
    site = Site(tmp_path)
    journal = Journal(site.layout.state / "trader" / "outbox.jsonl")
    journal.open()
    journal.write("intent", client_order_id="x")
    journal.close()
    journal.path.write_bytes(journal.path.read_bytes().replace(b'"x"', b'"y"'))
    with pytest.raises(DeployRefused, match="whether the broker book is flat is not known"):
        site.run()


def test_a_running_trader_stops_a_deploy_while_the_market_is_open_and_is_never_restarted_when_it_is_shut(tmp_path):
    site = Site(tmp_path, now_ns=WEDNESDAY, running=(RECORDER, TRADER))
    with pytest.raises(DeployRefused, match="the trader is running and the market is open"):
        site.run()
    site.manager.running.discard(TRADER)
    lock = site.layout.state / "trader" / "trader.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as held:  # a trader started by hand holds its lock, whatever the service manager says
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(DeployRefused, match="the trader is running and the market is open"):
            site.run()
    with pytest.raises(DeployRefused, match=r"hft-trader\.service is not restarted by a deploy"):
        site.run(restart=[TRADER])
    assert site.whole() == site.one and site.manager.restarted == []

    weekend = Site(tmp_path / "weekend", now_ns=SATURDAY, running=(RECORDER, TRADER))
    assert weekend.run() == 0
    assert weekend.manager.restarted == [RECORDER], "the recorder, which was running; never the trader"
    assert any(f"{TRADER} is running and was NOT restarted" in line for line in weekend.said)
    with pytest.raises(DeployRefused, match="is not restarted by a deploy"):
        deploy.Systemctl().restart(TRADER)
    with pytest.raises(DeployRefused, match="is not restarted by a deploy"):
        deploy.Systemctl().restart("ssh.service")


def test_the_recorder_is_not_restarted_inside_the_rollover_window_and_a_stopped_unit_is_never_started(tmp_path):
    for offset in (-30 * 60 * S, 0, 30 * 60 * S):
        site = Site(tmp_path / f"at{offset // S}", now_ns=ROLLOVER + offset)
        with pytest.raises(DeployRefused, match="inside the rollover window"):
            site.run()
    outside = Site(tmp_path / "outside", now_ns=ROLLOVER + 31 * 60 * S)
    assert outside.run() == 0
    stopped = Site(tmp_path / "stopped", now_ns=ROLLOVER, running=(SHADOW,))
    assert stopped.run(restart=[RECORDER, SHADOW]) == 0, "the recorder is not running: no restart, so no hole"
    assert stopped.manager.restarted == [SHADOW]
    assert f"{RECORDER} is not running: it is not started, and so not restarted" in stopped.said
    nothing = Site(tmp_path / "nothing", now_ns=ROLLOVER)
    assert nothing.run(restart=[]) == 0 and nothing.manager.restarted == []


def test_unit_files_that_are_not_the_installed_ones_stop_the_deploy_before_the_swap(tmp_path):
    site = Site(tmp_path)
    installed = tmp_path / "units"
    installed.mkdir()
    (installed / "hft-recorder.service").write_text("the recorder's unit, as installed last year\n")
    with pytest.raises(DeployRefused, match=r"are not the ones installed: hft-recorder\.service"):
        site.run(unit_dir=installed)
    assert site.whole() == site.one and site.manager.restarted == []
    shutil.copy(site.layout.release(site.two) / "deploy" / "systemd" / "hft-recorder.service", installed)
    assert site.run(unit_dir=installed) == 0 and site.whole() == site.two


def test_one_deploy_at_a_time(tmp_path):
    site = Site(tmp_path)
    with deploy.one_at_a_time(site.layout), pytest.raises(DeployRefused, match="another deploy is running"), \
            deploy.one_at_a_time(site.layout):  # fmt: skip
        pass


# ---- the real tools, as far as they can be tried without a host ------------------------------------------------------


def test_the_checks_are_read_from_the_hosting_services_answer_latest_run_of_each():
    answer = {"check_runs": [
        {"id": 1, "name": "test", "status": "completed", "conclusion": "failure"},
        {"id": 5, "name": "test", "status": "completed", "conclusion": "success"},
        {"id": 3, "name": "leakcheck", "status": "in_progress", "conclusion": None},
    ]}  # fmt: skip
    assert deploy.parse_checks(json.dumps(answer)) == {"test": "success", "leakcheck": "in_progress"}
    assert deploy.parse_checks("not json") is None and deploy.parse_checks('{"message": "Not Found"}') is None
    assert deploy._SLUG.fullmatch("git@github.com:Some-Org/some.repo.git")["slug"] == "Some-Org/some.repo"
    assert deploy._SLUG.fullmatch("https://github.com/Some-Org/some.repo")["slug"] == "Some-Org/some.repo"


def test_the_environments_name_is_a_hash_of_the_lock_files_it_is_built_from(tmp_path):
    (tmp_path / "requirements.lock.txt").write_text("a==1\n")
    (tmp_path / "requirements-ml.lock.txt").write_text("b==1\n")
    plain, with_ml = deploy.lock_hash(tmp_path, False), deploy.lock_hash(tmp_path, True)
    assert plain == hashlib.sha256(b"a==1\n").hexdigest() and with_ml != plain
    (tmp_path / "requirements-ml.lock.txt").write_text("b==2\n")
    assert deploy.lock_hash(tmp_path, True) != with_ml and deploy.lock_hash(tmp_path, False) == plain


def test_the_smoke_commands_start_every_units_module_and_look_three_times_deeper():
    commands = deploy.smoke_commands()
    started = {words[1] for words in commands if words[-1] == "--help"}
    every = (*units.SERVICES, *units.weekend(*units.weekend_times()))
    assert started == {words[1] for spec in every for words in (spec.command, spec.first, spec.then) if words}
    assert {"hftlab.record", "hftlab.trader", "hftlab.ops.report", "hftlab.learn.drift"} <= started
    assert commands[-3:] == [
        ("-m", "hftlab.ops.units", "diff", "--unit-dir", "deploy/systemd"), ("-m", "hftlab.trader", "--stages"),
        ("-m", "hftlab.ops.status", "--quiet"),
    ]  # fmt: skip


@pytest.mark.smoke
def test_this_checkout_passes_the_smoke_tests_a_deploy_would_run_in_it():
    assert deploy.run_smoke(ROOT, sys.executable) == []


@pytest.mark.smoke
def test_a_checkout_whose_module_cannot_start_fails_them_and_says_which(tmp_path):
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "deploy", tmp_path / "deploy")
    (tmp_path / "src" / "hftlab" / "shadow" / "__main__.py").write_text("import a_library_that_is_not_installed\n")
    failed = deploy.run_smoke(tmp_path, sys.executable)
    assert any(line.startswith("-m hftlab.shadow --help: exit 1") and "ModuleNotFoundError" in line for line in failed)
    assert not any(line.startswith("-m hftlab.record --help") for line in failed)


def test_the_command_refuses_a_root_that_was_never_adopted_and_says_nothing_was_changed(tmp_path, capsys):
    site = Site(tmp_path, adopted=False)
    code = deploy.main(["--root", str(site.root), "run", "--commit", site.two, "--dry-run"], SimClock(SATURDAY))
    said = capsys.readouterr().err
    assert code == 2 and "REFUSED. What is in service was not changed." in said and "run `adopt` once" in said
