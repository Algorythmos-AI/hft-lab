"""Shared fixtures: a hermetic git environment and throwaway repositories."""
from __future__ import annotations

import os

import pytest
from hypothesis import settings

from tests.support import GitRepo

# No example database (it would write into the checkout) and no per-example deadline (CI runners stall).
settings.register_profile("hftlab", database=None, deadline=None)
settings.load_profile("hftlab")


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch):
    """Git sees neither the developer's configuration nor a calling hook's index; the leak inputs are unset."""
    for name in [n for n in os.environ if n.startswith("GIT_")] + ["HFTLAB_LEAK_TERMS", "HFTLAB_LEAK_CORPUS"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "tester")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "tester@example.invalid")
        monkeypatch.setenv(f"GIT_{role}_DATE", "2026-01-01T00:00:00+00:00")


@pytest.fixture
def repo(tmp_path) -> GitRepo:
    return GitRepo(tmp_path / "repo")
