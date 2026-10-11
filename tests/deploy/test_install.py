"""The install script and the status wrapper: what they would do, shown without doing it.

`hft-install` is run in --dry-run against a temporary root, from a throwaway clone that stands for the
owner's checkout of a commit on main. No sudo, no systemd, no user is created, and nothing outside tmp_path is
written. What is held to: it refuses without a passing pre-flight, it enables nothing unasked, it touches only
this platform's units, and a second run finds nothing left to do.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hftlab.ops import units
from tests.ops.samples import recent, write
from tests.support import ROOT, GitRepo

SCRIPTS = ROOT / "deploy" / "bin"
ORIGIN = "https://example.invalid/hft-lab.git"
GATE_NEEDS = ("src/hftlab/__init__.py", "src/hftlab/core", "src/hftlab/ops/__init__.py", "src/hftlab/ops/preflight.py",
              "config/preflight.json", "requirements.lock.txt", "requirements-ml.lock.txt", "deploy")  # fmt: skip


@pytest.fixture
def source(tmp_path) -> GitRepo:
    """A checkout the owner could run the script from: what the script reads, committed, on origin/main."""
    repo = GitRepo(tmp_path / "checkout")
    for part in GATE_NEEDS:
        target = repo.root / part
        target.parent.mkdir(parents=True, exist_ok=True)
        if (ROOT / part).is_dir():
            shutil.copytree(ROOT / part, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy(ROOT / part, target)
    repo.git("add", "-A")
    repo.commit("the tree")
    repo.git("remote", "add", "origin", ORIGIN)
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def install(source: GitRepo, *arguments: object, path: str | None = None) -> subprocess.CompletedProcess[str]:
    command = ["sh", str(source.root / "deploy" / "bin" / "hft-install"), *map(str, arguments)]
    environment = {**os.environ, "HFT_PYTHON3": sys.executable, "PATH": path or os.environ["PATH"]}
    return subprocess.run(command, capture_output=True, text=True, check=False, cwd=source.root.parent, env=environment)


def plan(done: subprocess.CompletedProcess[str]) -> list[str]:
    """The commands a run would execute: the lines it prints with a plus."""
    return [line.removeprefix("   + ") for line in done.stdout.splitlines() if line.startswith("   + ")]


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")) if root.exists() else []


def good_samples(tmp_path) -> str:
    return write(tmp_path, recent())


# ---- the scripts themselves ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hft-install", "hft-status", "hft-preflight-sample"])
def test_every_script_is_posix_shell_parses_and_is_executable(name):
    script = SCRIPTS / name
    assert script.read_text().startswith("#!/bin/sh\n") and script.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["sh", "-n", str(script)], check=False).returncode == 0
    assert "[[" not in script.read_text() and "#!/usr/bin/env bash" not in script.read_text()
    assert sorted(path.name for path in SCRIPTS.iterdir()) == ["hft-install", "hft-preflight-sample", "hft-status"]


def test_no_script_holds_an_address_a_key_or_another_desks_unit():
    for script in SCRIPTS.iterdir():
        text = script.read_text()
        assert "wt-" not in text and "/home/wt" not in text, script.name
        assert "curl" not in text and "wget" not in text, f"{script.name} downloads nothing"
        for word in ("PASSWORD", "password", "TOKEN", "HFT_PUBLISH_KEY", "HFT_ALERT_URL"):
            assert word not in text, f"{script.name} names {word}"


# ---- the gate comes first --------------------------------------------------------------------------------------------


def test_without_pre_flight_samples_it_refuses_before_anything_else(source, tmp_path):
    root = tmp_path / "root"
    done = install(source, "--dry-run", "--root", root)
    assert done.returncode == 2 and "REFUSED: no pre-flight samples" in done.stderr
    assert plan(done) == [] and "2. the user" not in done.stdout and tree(root) == []


def test_samples_that_fail_the_gate_refuse_the_install_and_show_why(source, tmp_path):
    root = tmp_path / "root"
    tight = write(tmp_path, recent(mem_available_kb=1_200_000))
    done = install(source, "--dry-run", "--root", root, "--preflight", tight)
    assert done.returncode == 2
    assert "pre-flight: FAIL: the platform is NOT installed" in done.stdout
    assert "[NO] memory headroom: least available 1,200,000 kB" in done.stdout
    assert "REFUSED: the pre-flight gate did not pass" in done.stderr and "NOT installed on this host" in done.stderr
    assert plan(done) == [] and "2. the user" not in done.stdout and tree(root) == []


def test_samples_it_cannot_read_refuse_too(source, tmp_path):
    done = install(source, "--dry-run", "--root", tmp_path / "root", "--preflight", tmp_path / "no-such.log")
    assert done.returncode == 2 and "REFUSED: cannot read" in done.stderr and plan(done) == []


# ---- a dry run prints every step and changes nothing -----------------------------------------------------------------


def test_a_dry_run_prints_every_step_in_order_and_changes_nothing(source, tmp_path):
    root = tmp_path / "root"
    before = tree(tmp_path)
    done = install(source, "--dry-run", "--root", root, "--preflight", good_samples(tmp_path))
    assert done.returncode == 0, done.stderr
    assert tree(tmp_path) == [*before, "hft-preflight.log"], "nothing was created, not even the root"
    out = done.stdout
    assert out.startswith("hft-install: DRY RUN. Nothing is changed") and out.rstrip().endswith("Nothing was changed.")
    headings = [line for line in out.splitlines() if line.startswith("== ")]
    assert [heading[3:5] for heading in headings] == ["1.", "2.", "3.", "4.", "5.", "6.", "7."]
    assert "pre-flight: PASS: the platform may be installed" in out and "the samples pass" in out
    head = source.git("rev-parse", "HEAD").stdout.strip()
    dest = f"{root}/home/hft/hft-lab"
    steps = plan(done)
    uv = shutil.which("uv") or "uv"
    assert steps[:6] == [
        f"useradd --system --create-home --home-dir {root}/home/hft --shell /usr/sbin/nologin hft",
        f"runuser -u hft -- git clone --quiet {ORIGIN} {dest}",
        f"runuser -u hft -- git -C {dest} checkout --quiet --detach {head}",
        f"runuser -u hft -- {uv} venv --quiet --python 3.12 {dest}/.venv",
        f"runuser -u hft -- {uv} pip install --quiet --require-hashes --python {dest}/.venv/bin/python "
        f"-r {dest}/requirements.lock.txt",
        f'runuser -u hft -- sh -c cksum < "$1/requirements.lock.txt" > "$1/.venv/.hft-lock" sh {dest}',
    ]
    assert f"install -d -o hft -g hft -m 0750 {dest}/var/record" in steps
    assert f"install -d -o root -g hft -m 0750 {root}/etc/hft" in steps
    assert f"keep the passing report in {root}/etc/hft/preflight-passed.json" in steps
    assert steps[-1] == "systemctl daemon-reload"


def test_the_plan_installs_exactly_the_rendered_units_and_the_status_helper(source, tmp_path):
    root = tmp_path / "root"
    steps = plan(install(source, "--dry-run", "--root", root, "--preflight", good_samples(tmp_path)))
    installed = sorted(step.split()[-1] for step in steps if step.startswith("install -m "))
    wanted = [f"{root}/etc/systemd/system/{name}" for name in units.render_all()]
    assert installed == sorted([*wanted, f"{root}/usr/local/bin/hft-status"])
    for step in steps:
        assert "wt-" not in step and "/home/wt" not in step, "nothing of the other desks is touched"
        if step.startswith(("install ", "useradd ")):
            assert str(root) in step, f"outside the root: {step}"


def test_it_enables_nothing_and_starts_nothing_unless_told(source, tmp_path):
    done = install(source, "--dry-run", "--root", tmp_path / "root", "--preflight", good_samples(tmp_path))
    assert [step for step in plan(done) if step.startswith("systemctl")] == ["systemctl daemon-reload"]
    assert "nothing is enabled and nothing is started" in done.stdout
    assert "sudo systemctl enable --now hft-recorder.service hft-housekeep.timer" in done.stdout, "the owner's command"
    assert "never creates it" in done.stdout, "the secrets file is the owner's"


def test_with_enable_it_enables_exactly_the_named_units_and_starts_them_only_with_start(source, tmp_path):
    arguments = ["--dry-run", "--root", tmp_path / "root", "--preflight", good_samples(tmp_path),
                 "--enable", "hft-recorder.service", "--enable", "hft-housekeep.timer"]  # fmt: skip
    enabled = plan(install(source, *arguments))
    assert enabled[-1] == "systemctl enable hft-recorder.service hft-housekeep.timer"
    started = plan(install(source, *arguments, "--start"))
    assert started[-1] == "systemctl enable --now hft-recorder.service hft-housekeep.timer"
    assert len([step for step in started if step.startswith("systemctl")]) == 2, "the reload, and this"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [(["--enable", "hft-gateway.service"], "hft-gateway.service cannot be enabled"),
     (["--enable", "hft-trader.service"], "hft-trader.service cannot be enabled"),
     (["--enable", "hft.slice"], "hft.slice cannot be enabled"),
     (["--enable", "hft-publish.service"], "hft-publish.service cannot be enabled"),
     (["--enable", "wt-paper-b.service"], "wt-paper-b.service cannot be enabled"),
     (["--enable", "ssh.service"], "ssh.service cannot be enabled"),
     (["--enable", "hft-train.timer"], "hft-train.timer is the owner's to enable by hand"),
     (["--enable", "hft-report.timer"], "hft-report.timer is the owner's to enable by hand"),
     (["--enable", "hft-recorder.service", "--enable", "hft-promote.timer"], "hft-promote.timer is the owner's"),
     (["--start"], "--start starts the units named with --enable"),
     (["--force"], "unknown option --force")],
)  # fmt: skip
def test_it_refuses_a_unit_that_is_not_ours_to_enable_and_an_option_it_does_not_have(
    source, tmp_path, arguments, message
):
    done = install(source, "--dry-run", "--root", tmp_path / "root", "--preflight", good_samples(tmp_path), *arguments)
    assert done.returncode == 2 and f"REFUSED: {message}" in done.stderr and plan(done) == []


def test_a_real_run_is_refused_under_a_test_root_and_without_root(source, tmp_path):
    samples = good_samples(tmp_path)
    under_a_root = install(source, "--root", tmp_path / "root", "--preflight", samples)
    assert under_a_root.returncode == 2 and "--root is for --dry-run" in under_a_root.stderr
    if os.geteuid() != 0:
        real = install(source, "--preflight", samples)
        assert real.returncode == 2 and "REFUSED: run it with sudo" in real.stderr
    assert tree(tmp_path / "root") == []


# ---- only a reviewed commit is installed -----------------------------------------------------------------------------


def test_a_commit_that_is_not_on_main_is_not_installed(source, tmp_path):
    source.add("deploy/extra.txt", "a change nobody reviewed\n")
    source.commit("a local commit")
    done = install(source, "--dry-run", "--root", tmp_path / "root", "--preflight", good_samples(tmp_path))
    assert done.returncode == 2 and "is not on origin/main: only a reviewed commit is installed" in done.stderr


def test_a_checkout_with_uncommitted_changes_is_not_installed(source, tmp_path):
    script = source.root / "deploy" / "bin" / "hft-status"
    script.write_text(script.read_text() + "# edited by hand\n")
    done = install(source, "--dry-run", "--root", tmp_path / "root", "--preflight", good_samples(tmp_path))
    assert done.returncode == 2 and "has uncommitted changes" in done.stderr and plan(done) == []


# ---- a second run changes nothing ------------------------------------------------------------------------------------


def installed(source: GitRepo, tmp_path) -> tuple[Path, str]:
    """A root that already holds everything the script installs, and a PATH on which the user `hft` exists."""
    root = tmp_path / "root"
    dest = root / "home" / "hft" / "hft-lab"
    dest.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(source.root), str(dest)], check=True, capture_output=True)
    for directory in ("var/record", "var/shadow", "var/alerts", ".venv/bin"):
        (dest / directory).mkdir(parents=True)
    python = dest / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    with (source.root / "requirements.lock.txt").open("rb") as lock:
        stamp = subprocess.run(["cksum"], stdin=lock, capture_output=True, text=True, check=True).stdout
    (dest / ".venv" / ".hft-lock").write_text(stamp)
    for directory in ("etc/hft", "etc/systemd/system", "usr/local/bin"):
        (root / directory).mkdir(parents=True)
    (root / "etc" / "hft" / "preflight-passed.json").write_text('{"result": "pass"}\n')
    for unit in (source.root / "deploy" / "systemd").iterdir():
        shutil.copy(unit, root / "etc" / "systemd" / "system" / unit.name)
    shutil.copy(source.root / "deploy" / "bin" / "hft-status", root / "usr" / "local" / "bin" / "hft-status")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    known = 'if [ "$1" = "-u" ] && [ "${2:-}" = hft ]; then echo 990; exit 0; fi'
    (stubs / "id").write_text(f'#!/bin/sh\n{known}\nexec /usr/bin/id "$@"\n')
    (stubs / "id").chmod(0o755)
    return root, f"{stubs}{os.pathsep}{os.environ['PATH']}"


def test_on_a_host_that_is_already_installed_there_is_nothing_left_to_do_and_no_samples_are_needed(source, tmp_path):
    root, path = installed(source, tmp_path)
    before = tree(root)
    done = install(source, "--dry-run", "--root", root, path=path)
    assert done.returncode == 0, done.stderr
    assert plan(done) == [], "every step is skipped"
    assert "passed at the first install" in done.stdout and "no unit changed: systemd is not reloaded" in done.stdout
    assert done.stdout.count("   = ") >= 18 and tree(root) == before


def test_only_what_has_changed_is_done_again(source, tmp_path):
    root, path = installed(source, tmp_path)
    (root / "etc" / "systemd" / "system" / "hft.slice").write_text("[Slice]\nMemoryMax=4G\n")
    (root / "home" / "hft" / "hft-lab" / ".venv" / ".hft-lock").write_text("an older lock file\n")
    steps = plan(install(source, "--dry-run", "--root", root, path=path))
    assert [step.split()[0] for step in steps] == ["runuser", "runuser", "runuser", "install", "systemctl"]
    assert " venv --quiet --clear --python 3.12 " in steps[0], "an environment that is there is replaced, not patched"
    assert steps[3].endswith(f"{root}/etc/systemd/system/hft.slice") and steps[4] == "systemctl daemon-reload"


def test_the_optional_ml_set_is_installed_only_when_asked_for_by_hash_and_once(source, tmp_path):
    fresh = tmp_path / "fresh"
    without = install(source, "--dry-run", "--root", fresh, "--preflight", good_samples(tmp_path))
    assert not [step for step in plan(without) if "requirements-ml" in step], "not unless it is asked for"
    assert "the optional ML set is not asked for (--with-ml)" in without.stdout
    asked = plan(install(source, "--dry-run", "--root", fresh, "--with-ml", "--preflight", good_samples(tmp_path)))
    dest, uv = f"{fresh}/home/hft/hft-lab", shutil.which("uv") or "uv"
    assert asked[6:8] == [
        f"runuser -u hft -- {uv} pip install --quiet --require-hashes --python {dest}/.venv/bin/python "
        f"-r {dest}/requirements-ml.lock.txt",
        f'runuser -u hft -- sh -c cksum < "$1/requirements-ml.lock.txt" > "$1/.venv/.hft-lock-ml" sh {dest}',
    ], "after the base environment, into the same one, every package checked against its hash"

    root, path = installed(source, tmp_path)
    steps = plan(install(source, "--dry-run", "--root", root, "--with-ml", path=path))
    assert len(steps) == 2 and all("requirements-ml.lock.txt" in step for step in steps), "added; nothing is rebuilt"
    with (source.root / "requirements-ml.lock.txt").open("rb") as lock:
        stamp = subprocess.run(["cksum"], stdin=lock, capture_output=True, text=True, check=True).stdout
    (root / "home" / "hft" / "hft-lab" / ".venv" / ".hft-lock-ml").write_text(stamp)
    assert plan(install(source, "--dry-run", "--root", root, "--with-ml", path=path)) == [], "a second run adds nothing"
    (root / "home" / "hft" / "hft-lab" / ".venv" / ".hft-lock").write_text("an older lock file\n")
    again = plan(install(source, "--dry-run", "--root", root, "--with-ml", path=path))
    assert " venv --quiet --clear " in again[0] and "requirements-ml.lock.txt" in again[3], "rebuilt: installed again"


def test_the_environment_is_never_rebuilt_under_a_running_service(source, tmp_path):
    root, path = installed(source, tmp_path)
    (root / "home" / "hft" / "hft-lab" / ".venv" / ".hft-lock").write_text("an older lock file\n")
    systemctl = tmp_path / "stubs" / "systemctl"  # a service manager that says the recorder is running
    systemctl.write_text('#!/bin/sh\n[ "$1" = "is-active" ] && [ "$3" = "hft-recorder.service" ]\n')
    systemctl.chmod(0o755)
    done = install(source, "--dry-run", "--root", root, path=path)
    assert done.returncode == 0
    assert "a real run would REFUSE here" in done.stdout and "running: hft-recorder.service" in done.stdout
    assert "Stop them first" in (source.root / "deploy" / "bin" / "hft-install").read_text()


def test_an_installed_checkout_at_another_commit_is_not_given_units_from_this_one(source, tmp_path):
    root, path = installed(source, tmp_path)
    source.add("deploy/systemd/hft.slice", "[Slice]\nMemoryMax=2G\n")
    source.commit("a later commit on main")
    source.git("update-ref", "refs/remotes/origin/main", "HEAD")
    done = install(source, "--dry-run", "--root", root, path=path)
    assert done.returncode == 2 and "REFUSED:" in done.stderr and "Deploy first" in done.stderr
    assert not [step for step in plan(done) if step.startswith(("install -m", "systemctl"))]


# ---- hft-status ------------------------------------------------------------------------------------------------------


def status(*arguments: str, root: Path) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "HFT_ROOT": str(root)}
    command = ["sh", str(SCRIPTS / "hft-status"), *arguments]
    return subprocess.run(command, capture_output=True, text=True, check=False, env=environment)


def a_platform(tmp_path) -> Path:
    """Enough of an installed platform for the wrapper: the code, the settings and an interpreter."""
    root = tmp_path / "hft-lab"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "var").mkdir()
    python = root / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    for name in ("src", "config"):
        (root / name).symlink_to(ROOT / name)
    return root


def test_the_status_wrapper_says_what_it_would_run(tmp_path):
    root = a_platform(tmp_path)
    done = status("--dry-run", "--json", root=root)
    assert done.returncode == 0 and done.stdout.startswith("hft-status: DRY RUN, nothing is run")
    assert f"HFT_STATE_DIR={root}/var .venv/bin/python -m hftlab.ops.status --json" in done.stdout


def test_the_status_wrapper_runs_the_status_command_in_the_installed_tree(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("as root the wrapper becomes the platform's user, which a test machine does not have")
    done = status(root=a_platform(tmp_path))
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("HFT Lab status at ") and "recorder   never started" in done.stdout


def test_the_status_wrapper_says_so_when_the_platform_is_not_installed(tmp_path):
    missing = status(root=tmp_path / "nowhere")
    assert missing.returncode == 2 and "is not there: is the platform installed?" in missing.stderr
    (tmp_path / "half").mkdir()
    half = status(root=tmp_path / "half")
    assert half.returncode == 2 and "has no environment: run hft-install" in half.stderr
