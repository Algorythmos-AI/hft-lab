"""Pruning old checkouts: what is in service, what `back` goes to and what a unit runs in are never removed.

Everything runs in a folder under tmp_path, on the layout a real deploy made there with git itself. Where the
running units have their working directories is a stand-in: no systemd, no /proc.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from hftlab.core import SimClock
from hftlab.ops import deploy
from hftlab.ops.deploy import RECORDER, TRADER, DeployRefused, Layout, Request, prune
from tests.faults.kit import AFTER, BEFORE, Killed
from tests.ops.test_deploy import SATURDAY, Site, kill_at
from tests.support import ROOT

KEPT_FILES = ("src/code.txt", "requirements.lock.txt", "config/trader.yaml")


class Host(Site):
    """A host after four deploys: five checkouts, the newest in service, and two environments."""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self.commits = [self.one, self.two]
        for text, lock in (("three", "a==1\n"), ("four", "a==2\n"), ("five", "a==2\n")):
            self.origin.add("src/code.txt", text + "\n")
            self.origin.add("requirements.lock.txt", lock)
            self.commits.append(self.origin.commit(text))
        for commit in self.commits[1:]:
            assert self.run(commit) == 0
        self.three, self.four, self.five = self.commits[2:]
        self.old_env, self.new_env = (deploy.lock_hash(self.layout.release(c), False) for c in (self.two, self.five))
        self.said.clear()
        self.running: dict[str, Path] | None = {}  # who runs where; nothing does, unless a test says so

    def prune(self, keep_more: int = 1, **options) -> int:
        return prune(
            self.layout, keep_more=keep_more, in_use=lambda: self.running, git=self.tools().git,
            say=self.said.append, **options,
        )  # fmt: skip

    def releases(self) -> list[str]:
        """The checkouts that are there, in the order they were deployed, each checked to be whole."""
        found = [commit for commit in self.commits if self.layout.release(commit).exists()]
        for commit in found:
            assert all((self.layout.release(commit) / name).is_file() for name in KEPT_FILES), f"{commit} is whole"
        assert len(found) == len(list(self.layout.releases.iterdir())), "and nothing else is in the folder"
        return found

    def envs(self) -> list[str]:
        return sorted(path.name for path in self.layout.envs.iterdir())

    def served(self) -> str:
        """The commit in service, with its code, its environment and the state all read through the root."""
        commit = self.layout.current()
        text = dict(zip(self.commits, ("one", "two", "three", "four", "five"), strict=True))[commit]
        assert self.code() == text and (self.root / ".venv").is_dir()
        assert (self.root / "var" / "record" / "kept").read_text() == "a recording\n"
        return commit


def test_it_keeps_what_is_in_service_what_back_goes_to_and_the_newest_of_the_rest_and_says_so_first(tmp_path):
    site = Host(tmp_path)
    assert site.releases() == site.commits and site.envs() == sorted([site.old_env, site.new_env])
    assert site.prune(keep_more=1) == 0
    assert site.releases() == [site.three, site.four, site.five] and site.served() == site.five
    assert site.envs() == sorted([site.old_env, site.new_env]), "the checkout that was kept still links to the old one"
    plan = [line for line in site.said if line.startswith(("keep ", "REMOVE "))]
    assert sorted(plan) == sorted([
        f"REMOVE {site.one[:12]}", f"REMOVE {site.two[:12]}",
        f"keep {site.three[:12]}: one of the 1 newest of the rest",
        f"keep {site.four[:12]}: the one before the last swap: what `back` goes to",
        f"keep {site.five[:12]}: in service",
        f"keep environment {site.old_env[:12]}: a kept checkout links to it",
        f"keep environment {site.new_env[:12]}: a kept checkout links to it",
    ]), "one line for every checkout and every environment there is"  # fmt: skip
    steps = [line for line in site.said if line.startswith("step ")]
    assert [line.split(": ", 1)[1] for line in steps] == [
        f"remove the checkout {site.layout.release(site.one)}", f"remove the checkout {site.layout.release(site.two)}",
        "tell the mirror that those checkouts are gone",
    ], "the oldest first"  # fmt: skip
    assert max(site.said.index(line) for line in plan) < min(site.said.index(line) for line in steps), (
        "everything it keeps and removes is said before the first removal"
    )
    assert site.said[-1] == "2 checkout(s) and 0 environment(s) removed; 3 checkout(s) kept"
    listed = site.tools().git(["worktree", "list"])[1]
    assert site.two not in listed and site.three in listed, "and the mirror no longer counts what is gone"
    assert site.deploy().back(Request("")) == 0 and site.served() == site.four, "going back still works"
    site.said.clear()
    assert site.prune(keep_more=1) == 0 and site.releases() == [site.three, site.four, site.five], "twice: nothing more"
    assert not any(line.startswith(("step ", "REMOVE ")) for line in site.said)


def test_with_none_more_to_keep_it_leaves_two_checkouts_and_removes_the_environment_nothing_links_to(tmp_path):
    site = Host(tmp_path)
    assert site.prune(keep_more=0) == 0
    assert site.releases() == [site.four, site.five] and site.envs() == [site.new_env] and site.served() == site.five
    assert f"REMOVE environment {site.old_env[:12]}: no kept checkout links to it" in site.said
    assert site.said[-1] == "3 checkout(s) and 1 environment(s) removed; 2 checkout(s) kept"
    assert (site.layout.state / "record" / "kept").read_text() == "a recording\n", "the state is not a checkout"
    assert site.deploy().back(Request("")) == 0 and site.served() == site.four
    assert site.prune(keep_more=0) == 0 and site.releases() == [site.four, site.five], (
        "after going back, the checkout that was gone back from is the other end of the last swap, and is kept"
    )
    with pytest.raises(DeployRefused, match="a whole number, zero or more"):
        site.prune(keep_more=-1)


def test_a_dry_run_says_the_same_and_removes_nothing(tmp_path):
    site = Host(tmp_path)
    before = site.snapshot()
    assert site.prune(keep_more=0, dry_run=True) == 0
    assert site.snapshot() == before and site.releases() == site.commits
    steps = [line for line in site.said if line.startswith("step ")]
    assert len(steps) == 5 and all(line.endswith("(dry run: not done)") for line in steps)
    assert [line for line in site.said if line.startswith("REMOVE ")] == [
        *sorted([f"REMOVE {site.one[:12]}", f"REMOVE {site.two[:12]}", f"REMOVE {site.three[:12]}"]),
        f"REMOVE environment {site.old_env[:12]}: no kept checkout links to it",
    ]
    assert site.said[-1] == "3 checkout(s) and 1 environment(s) would be removed; 2 checkout(s) kept"
    assert not (site.layout.home / deploy.REMOVING).exists()


def test_a_checkout_a_unit_or_any_process_still_runs_in_is_never_removed_however_old(tmp_path):
    site = Host(tmp_path)
    site.running = {
        TRADER: site.layout.release(site.one), RECORDER: site.root,  # the trader was never restarted: the oldest
        "process 4242": site.layout.release(site.two) / "src", "process 1": Path("/"),
    }  # fmt: skip
    assert site.prune(keep_more=0) == 0
    assert site.releases() == [site.one, site.two, site.four, site.five]
    assert f"keep {site.one[:12]}: {TRADER} runs in it" in site.said
    assert f"keep {site.two[:12]}: process 4242 runs in it" in site.said
    assert f"keep {site.five[:12]}: in service; {RECORDER} runs in it" in site.said, "through the link, too"
    assert site.envs() == sorted([site.old_env, site.new_env]), "and so is the environment such a checkout links to"
    assert (site.layout.release(site.one) / ".venv" / "made-by").is_file(), "the first checkout's own, inside it"


def test_when_it_cannot_be_known_where_the_units_run_or_which_checkout_is_newer_nothing_is_removed(tmp_path):
    site = Host(tmp_path)
    before = site.snapshot()
    site.running = None
    with pytest.raises(DeployRefused, match="could not be looked up, so no checkout is known to be unused"):
        site.prune(keep_more=0)
    assert site.snapshot() == before
    site.running = {}
    journal = site.layout.state / "deploy" / "deploys.jsonl"
    whole = journal.read_bytes()
    journal.write_bytes(whole.replace(b'"deployed"', b'"rolled_back"', 1))
    with pytest.raises(DeployRefused, match="the deploy journal cannot be read or does not verify"):
        site.prune(keep_more=0)
    journal.write_bytes(whole)
    assert {name: found for name, found in site.snapshot().items() if "deploys.jsonl" not in name} == {
        name: found for name, found in before.items() if "deploys.jsonl" not in name
    }
    site.root.unlink()  # no link: nothing says what is in service
    with pytest.raises(DeployRefused, match="there is nothing to prune here"):
        site.prune(keep_more=0)
    assert site.releases() == site.commits


def test_a_record_of_the_last_swap_that_is_lost_costs_the_way_back_its_protection_and_nothing_else(tmp_path):
    site = Host(tmp_path)
    site.layout.last.write_text("not json\n")
    assert site.prune(keep_more=1) == 0
    assert site.releases() == [site.four, site.five] and site.served() == site.five, (
        "the newest of the rest by the deploy journal is the one before, so it is kept as that"
    )
    assert f"keep {site.four[:12]}: one of the 1 newest of the rest" in site.said


@pytest.mark.parametrize("how", [BEFORE, AFTER])
def test_a_kill_between_two_removals_leaves_every_checkout_whole_or_gone_and_the_next_run_finishes(
    tmp_path, monkeypatch, how
):
    for at in range(1, 6):  # three checkouts, the mirror, one environment
        site = Host(tmp_path / f"{how}-{at}")
        kill_at(monkeypatch, at, how)
        with pytest.raises(Killed):
            site.prune(keep_more=0)
        monkeypatch.undo()
        said = f"killed {how} step {at}"
        gone = at - (how == BEFORE)
        assert site.releases() == site.commits[min(gone, 3):], said  # whole, each of them: `releases` checks
        assert site.served() == site.five and site.layout.release(site.four).is_dir(), said
        assert site.new_env in site.envs(), said
        site.said.clear()
        assert site.prune(keep_more=0) == 0, said
        assert site.releases() == [site.four, site.five] and site.envs() == [site.new_env], said
        assert site.deploy().back(Request("")) == 0 and site.served() == site.four, said


def test_a_kill_in_the_middle_of_one_removal_leaves_no_half_checkout_where_checkouts_are(tmp_path, monkeypatch):
    site = Host(tmp_path)
    remove = shutil.rmtree

    def dying(path, *more, **options):
        if Path(path).parent.name == deploy.REMOVING and Path(path).exists():
            os.unlink(Path(path) / "src" / "code.txt")  # it had begun
            raise Killed("in the middle of removing a folder")
        return remove(path, *more, **options)

    monkeypatch.setattr(deploy.shutil, "rmtree", dying)
    with pytest.raises(Killed):
        site.prune(keep_more=0)
    monkeypatch.undo()
    assert site.releases() == site.commits[1:], "the one being removed had left the folder in one rename, whole"
    left = site.layout.home / deploy.REMOVING
    assert [path.name for path in left.iterdir()] == [site.one] and site.served() == site.five
    site.said.clear()
    assert site.prune(keep_more=0) == 0
    assert site.said[[line.startswith("step 1: ") for line in site.said].index(True)] == (
        f"step 1: finish removing what an earlier run left in {left}: {site.one[:12]}"
    )
    assert site.releases() == [site.four, site.five] and not any(left.iterdir()) and site.envs() == [site.new_env]


# ---- where the units run, and how many are kept --------------------------------------------------------------------


def made_up_proc(folder: Path, running: dict[int, Path]) -> Path:
    for pid, where in running.items():
        (folder / str(pid)).mkdir(parents=True)
        (folder / str(pid) / "cwd").symlink_to(where)
    (folder / "meminfo").parent.mkdir(parents=True, exist_ok=True)
    (folder / "meminfo").write_text("not a process\n")
    return folder


def test_where_a_unit_runs_is_its_main_processs_working_directory_and_an_unknown_one_is_no_answer(tmp_path):
    old, new = tmp_path / "releases" / "a", tmp_path / "releases" / "b"
    proc = made_up_proc(tmp_path / "proc", {11: old, 12: new, 13: tmp_path})
    pids = {TRADER: 11, RECORDER: 12}
    found = deploy.working_directories(proc, lambda unit: pids.get(unit, 0))
    assert found == {TRADER: old, RECORDER: new, "process 13": tmp_path}, "a unit that is not running has no folder"
    assert deploy.working_directories(proc, lambda unit: None) is None, "the service manager could not be asked"
    assert deploy.working_directories(proc, lambda unit: 99 if unit == TRADER else 0) is None, (
        "a unit runs and its folder cannot be read: not knowing is not the same as nowhere"
    )
    assert deploy.working_directories(tmp_path / "nothing", lambda unit: 0) is None
    (proc / "14").mkdir()  # a process whose folder cannot be read, and that is no unit's: passed over
    assert deploy.working_directories(proc, lambda unit: pids.get(unit, 0)) == found


def test_how_many_more_are_kept_is_the_settings_of_the_checkout_in_service_and_the_shipped_number_is_small(tmp_path):
    assert deploy.keep_more(Layout(ROOT)) == 2, "config/ops.yaml as shipped: an engineering default"
    for text, refused in (("deploy: {keep_more_releases: 0}\n", False), ("deploy: {keep_more_releases: 3}\n", False),
                          ("deploy: {keep_more_releases: -1}\n", True), ("deploy: {keep_more_releases: true}\n", True),
                          ("deploy: {keep_more_releases: '2'}\n", True), ("deploy: {}\n", True), ("status: {}\n", True),
                          ("[]\n", True), (": :\n", True)):  # fmt: skip
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "ops.yaml").write_text(text)
        if refused:
            with pytest.raises(DeployRefused, match=r"config/ops\.yaml"):
                deploy.keep_more(Layout(tmp_path))
        else:
            assert deploy.keep_more(Layout(tmp_path)) == int(text.split(": ")[2][0])
    with pytest.raises(DeployRefused, match="cannot be read"):
        deploy.keep_more(Layout(tmp_path / "nowhere"))


def test_the_command_prunes_by_the_settings_in_service_and_refuses_when_it_cannot_know_what_runs_where(
    tmp_path, capsys, monkeypatch
):
    site = Host(tmp_path)
    arguments = ["--root", str(site.root), "prune"]
    monkeypatch.setattr(deploy, "working_directories", lambda: None)
    assert deploy.main([*arguments, "--dry-run"], SimClock(SATURDAY)) == 2
    assert "config/ops.yaml of the checkout in service cannot be read" in capsys.readouterr().err
    (site.root / "config" / "ops.yaml").write_text("deploy: {keep_more_releases: 1}\n")
    assert deploy.main(arguments, SimClock(SATURDAY)) == 2
    said = capsys.readouterr().err
    assert "REFUSED. What is in service was not changed." in said and "could not be looked up" in said
    assert site.releases() == site.commits
    monkeypatch.setattr(deploy, "working_directories", lambda: {})
    assert deploy.main([*arguments, "--dry-run"], SimClock(SATURDAY)) == 0 and site.releases() == site.commits
    assert f"deploy: REMOVE {site.one[:12]}" in capsys.readouterr().out
    assert deploy.main(arguments, SimClock(SATURDAY)) == 0
    assert site.releases() == [site.three, site.four, site.five] and site.served() == site.five
