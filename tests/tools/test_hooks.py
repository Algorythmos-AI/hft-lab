"""The tracked hooks, run by git itself in a temporary repository that carries this repository's tools."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from tests.support import ROOT, GitRepo

HOOKS = ("pre-commit", "commit-msg", "pre-push")
PDF = b"%PDF-1.7\n1 0 obj\n"


@pytest.fixture
def hooked(repo, monkeypatch) -> GitRepo:
    """A repository with this repository's hooks and tools installed the way `make hooks` installs them."""
    shutil.copytree(ROOT / ".githooks", repo.root / ".githooks")
    (repo.root / "tools").mkdir()
    for name in ("leakcheck.py", "commitmsg.py", "leakcheck-allow.txt"):
        shutil.copy(ROOT / "tools" / name, repo.root / "tools" / name)
    repo.git("config", "core.hooksPath", ".githooks")
    monkeypatch.setenv("HFTLAB_PYTHON", sys.executable)
    return repo


def test_the_hooks_are_tracked_executable_and_installed_by_make_hooks():
    for name in HOOKS:
        mode = (ROOT / ".githooks" / name).stat().st_mode
        assert mode & stat.S_IXUSR, f".githooks/{name} is not executable"
    assert "git config core.hooksPath .githooks" in (ROOT / "Makefile").read_text()


def test_a_commit_is_scanned_attested_and_carries_its_attestation(hooked):
    hooked.git("add", "-A")
    hooked.commit("Add the tools")
    assert hooked.git("status", "--porcelain").stdout == ""
    doc = json.loads(hooked.git("show", "HEAD:.leakcheck-attest.json").stdout)
    assert doc["result"] == "pass" and doc["files"] == 6
    assert ".leakcheck-attest.json" in hooked.git("show", "--name-only", "--format=", "HEAD").stdout.split()


def test_every_later_commit_is_re_attested(hooked):
    hooked.git("add", "-A")
    hooked.commit("Add the tools")
    first = hooked.git("show", "HEAD:.leakcheck-attest.json").stdout
    hooked.add("README.md", "hello\n")
    hooked.commit("Add a readme")
    second = hooked.git("show", "HEAD:.leakcheck-attest.json").stdout
    assert json.loads(first)["tree"] != json.loads(second)["tree"] and json.loads(second)["files"] == 7
    verify = [sys.executable, "tools/leakcheck.py", "--verify-attest"]
    assert subprocess.run(verify, cwd=hooked.root, capture_output=True, check=False).returncode == 0


def test_a_commit_with_a_denied_file_is_refused(hooked):
    hooked.git("add", "-A")
    hooked.commit("Add the tools")
    hooked.add("docs/paper.pdf", PDF)
    result = hooked.git("commit", "-q", "-m", "Add a paper", check=False)
    assert result.returncode != 0 and "extension: docs/paper.pdf" in result.stderr
    assert hooked.git("log", "--format=%s").stdout.strip() == "Add the tools"


def test_a_commit_message_with_a_co_author_trailer_is_refused(hooked):
    hooked.git("add", "-A")
    result = hooked.git("commit", "-q", "-m", "Add the tools\n\nCo-Authored-By: x <x@example.invalid>", check=False)
    assert result.returncode != 0 and "Co-Authored-By" in result.stderr
    assert hooked.git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0


def test_a_push_is_verified_and_an_unattested_commit_is_not_pushed(hooked, tmp_path):
    remote = tmp_path / "remote.git"
    hooked.git("init", "-q", "--bare", str(remote))
    hooked.git("remote", "add", "origin", str(remote))
    hooked.git("add", "-A")
    hooked.commit("Add the tools")
    pushed = hooked.git("push", "-q", "origin", "main", check=False)
    assert pushed.returncode == 0, pushed.stderr

    hooked.add("README.md", "committed around the hooks\n")
    hooked.git("commit", "-q", "--no-verify", "-m", "Skip the hooks")
    refused = hooked.git("push", "-q", "origin", "main", check=False)
    assert refused.returncode != 0 and "is stale" in refused.stderr
    assert hooked.git("rev-parse", "origin/main").stdout != hooked.git("rev-parse", "main").stdout

    hooked.commit("Attest again")  # an empty commit still runs pre-commit, which re-attests the tree
    again = hooked.git("push", "-q", "origin", "main", check=False)
    assert again.returncode == 0, again.stderr


def test_deleting_a_remote_branch_needs_no_attestation(hooked, tmp_path):
    remote = tmp_path / "remote.git"
    hooked.git("init", "-q", "--bare", str(remote))
    hooked.git("remote", "add", "origin", str(remote))
    hooked.git("add", "-A")
    hooked.commit("Add the tools")
    hooked.git("push", "-q", "origin", "main:side")
    assert hooked.git("push", "-q", "origin", ":side", check=False).returncode == 0


def test_the_hooks_run_with_no_virtualenv_through_the_fallback_interpreter(hooked, monkeypatch):
    monkeypatch.delenv("HFTLAB_PYTHON")
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    assert shutil.which("python3"), "the fallback needs a python3 on PATH"
    hooked.git("add", "-A")
    hooked.commit("Add the tools")
    assert json.loads(hooked.git("show", "HEAD:.leakcheck-attest.json").stdout)["result"] == "pass"
