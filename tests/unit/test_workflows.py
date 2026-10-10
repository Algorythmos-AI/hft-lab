"""Rules for .github/workflows that a workflow linter does not check.

The repository is public, so the workflow is part of the attack surface: it must hold no secret, publish no
artifact and ask for no permission beyond reading the code. The two job names are required checks; renaming
one would leave every pull request waiting for a check that never reports.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from tests.support import ROOT

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
CI = ROOT / ".github" / "workflows" / "ci.yml"
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
# The pins the sibling repository uses, so one review covers both.
PINNED = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "c18668ad3cf93ea998bef934396af7bb5c839dc7",
}


def load(path) -> dict[str, Any]:
    data: dict[Any, Any] = yaml.safe_load(path.read_text())
    if True in data:  # YAML 1.1 reads the `on:` key as boolean True
        data["on"] = data.pop(True)
    return data


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps") or [])


def test_there_is_exactly_one_workflow_and_it_is_ci():
    assert [p.name for p in WORKFLOWS] == ["ci.yml"]


def test_the_jobs_are_exactly_test_and_leakcheck():
    jobs = load(CI)["jobs"]
    assert set(jobs) == {"test", "leakcheck"}
    assert {key: job.get("name") for key, job in jobs.items()} == {"test": "test", "leakcheck": "leakcheck"}


def test_the_workflow_may_only_read_the_code():
    workflow = load(CI)
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"job {name!r} sets its own permissions"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_references_a_secret(path):
    text = path.read_text()
    assert "secrets." not in text and "secrets[" not in text
    assert "GITHUB_TOKEN" not in text and "github.token" not in text
    for _, job in load(path)["jobs"].items():
        assert "secrets" not in job and "environment" not in job


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_publishes_an_artifact_or_writes_a_cache(path):
    used = [str(s["uses"]) for job in load(path)["jobs"].values() for s in steps(job) if "uses" in s]
    assert not [u for u in used if re.search(r"artifact|cache|upload|release|pages", u, re.IGNORECASE)], used
    assert {u.split("@")[0] for u in used} <= set(PINNED)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_the_reviewed_commit(path):
    for name, job in load(path)["jobs"].items():
        assert "uses" not in job, f"job {name!r} calls a reusable workflow"
        for step in steps(job):
            if "uses" in step:
                ref = str(step["uses"])
                assert SHA.match(ref), f"job {name!r} uses {ref!r}, not pinned to a full commit SHA"
                action, _, sha = ref.partition("@")
                assert PINNED[action] == sha, f"{action} is pinned to an unreviewed commit"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_runs_on_the_pinned_image_with_a_timeout(path):
    for name, job in load(path)["jobs"].items():
        assert job.get("runs-on") == "ubuntu-24.04", name
        assert isinstance(job.get("timeout-minutes"), int), f"job {name!r} has no timeout-minutes"


def test_checkouts_do_not_leave_a_token_behind():
    for name, job in load(CI)["jobs"].items():
        checkouts = [s for s in steps(job) if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert len(checkouts) == 1 and steps(job)[0] is checkouts[0], f"job {name!r} must start with one checkout"
        assert checkouts[0]["with"]["persist-credentials"] is False


def test_the_required_checks_always_run():
    workflow = load(CI)
    assert set(workflow["on"]) == {"pull_request", "push"}
    for event in ("pull_request", "push"):
        config = workflow["on"].get(event) or {}
        assert not {"paths", "paths-ignore"} & set(config), f"ci.yml {event} must not be path-filtered"
    assert workflow["on"]["push"] == {"branches": ["main"]}
    for name, job in workflow["jobs"].items():
        assert "if" not in job and "needs" not in job, f"job {name!r} must always run"
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_no_untrusted_text_is_interpolated_into_a_script():
    """An expression inside `run:` is pasted into the shell before it starts; pass values through `env:`."""
    for name, job in load(CI)["jobs"].items():
        for step in steps(job):
            assert "${{" not in str(step.get("run", "")), f"job {name!r}: an expression inside a run script"


def run_text(job: dict[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in steps(job))


def test_the_test_job_installs_by_hash_and_runs_every_check():
    script = run_text(load(CI)["jobs"]["test"])
    assert script.count("--require-hashes") == 2
    assert "requirements.lock.txt" in script and "requirements-dev.lock.txt" in script
    for command in ("-m ruff check .", "-m mypy", "-m pytest", "-m hftlab.registry lint"):
        assert command in script, command
    assert script.index("-m ruff") < script.index("-m mypy") < script.index("-m pytest") < script.index("registry lint")
    assert '--base "$base"' in script


def test_the_leakcheck_job_scans_history_and_verifies_the_attestation():
    job = load(CI)["jobs"]["leakcheck"]
    script = run_text(job)
    assert "tools/leakcheck.py --all-history" in script and "tools/leakcheck.py --verify-attest" in script
    assert "uv " not in script and "pip" not in script, "the leak check needs nothing installed"
    checkout = steps(job)[0]["with"]
    assert checkout["fetch-depth"] == 0, "all history is scanned"
    # The attestation describes the branch head, not the merge commit built for a pull request.
    assert checkout["ref"] == "${{ github.event.pull_request.head.sha || github.sha }}"


def test_make_ci_runs_what_the_workflow_runs():
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^ci: lint typecheck test leakcheck\b", makefile, re.MULTILINE)
    for target, command in {
        "lint": "$(PY) -m ruff check .",
        "typecheck": "$(PY) -m mypy",
        "test": "$(PY) -m pytest",
        "leakcheck": "$(PY) tools/leakcheck.py --all-history",
    }.items():
        body = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE)
        assert body and command in body.group(1), target
    assert "$(PY) -m hftlab.registry lint" in makefile and "$(PY) tools/leakcheck.py --verify-attest" in makefile
