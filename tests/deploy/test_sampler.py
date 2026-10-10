"""The pre-flight sampler: a shell script the owner starts on the host. It reads three things and writes one file.

The script is run here against a temporary root that holds a made-up /proc, in --dry-run and for a few real
samples. No sudo, no systemd, and nothing outside tmp_path is touched.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest

from hftlab.ops.preflight import parse
from tests.support import ROOT

SAMPLER = ROOT / "deploy" / "bin" / "hft-preflight-sample"
MEMINFO = """MemTotal:        4028000 kB
MemFree:          310000 kB
MemAvailable:    1843200 kB
Buffers:           90000 kB
SwapTotal:        999996 kB
SwapFree:         512000 kB
"""


def a_root(tmp_path, meminfo: str = MEMINFO, loadavg: str = "0.42 0.40 0.38 1/234 5678\n"):
    (tmp_path / "root" / "proc").mkdir(parents=True)
    (tmp_path / "root" / "proc" / "meminfo").write_text(meminfo)
    (tmp_path / "root" / "proc" / "loadavg").write_text(loadavg)
    return tmp_path / "root"


def run(*arguments: object, cwd=None) -> subprocess.CompletedProcess[str]:
    command = ["sh", str(SAMPLER), *map(str, arguments)]
    return subprocess.run(command, capture_output=True, text=True, check=False, cwd=cwd)


def test_the_script_is_a_posix_shell_script_that_parses_and_is_executable():
    assert SAMPLER.read_text().startswith("#!/bin/sh\n") and SAMPLER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["sh", "-n", str(SAMPLER)], check=False).returncode == 0
    text = SAMPLER.read_text()
    assert "[[" not in text and "sudo" not in text.split("set -eu")[1], "no bash, and it never asks for privilege"


def test_a_dry_run_says_what_it_would_do_prints_the_line_and_writes_nothing(tmp_path):
    root = a_root(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    done = run("--dry-run", "--root", root, "--out", tmp_path / "out.log", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "hft-preflight-sample: DRY RUN, nothing is written"
    assert f"would append 1440 lines, one every 60 s, to {tmp_path / 'out.log'}" in lines[1]
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert not (tmp_path / "out.log").exists()
    samples, unreadable = parse(lines[-1])
    assert unreadable == 0 and len(samples) == 1, "the checker reads exactly what the sampler writes"
    sample = samples[0]
    assert (sample.mem_available_kb, sample.mem_total_kb, sample.swap_free_kb) == (1_843_200, 4_028_000, 512_000)
    assert sample.load_centi == 42 and sample.disk_free_kb > 0


def test_a_run_appends_one_line_a_sample_to_its_own_log_and_touches_nothing_else(tmp_path):
    root = a_root(tmp_path)
    proc = {path.name: path.read_text() for path in (root / "proc").iterdir()}
    done = run("--root", root, "--out", tmp_path / "out.log", "--samples", 3, "--interval", 0, cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "", done.stderr
    lines = (tmp_path / "out.log").read_text().splitlines()
    assert lines[0].startswith("# hft-preflight-sample: 3 samples, one every 0 s, from 20") and len(lines) == 4
    samples, unreadable = parse("\n".join(lines))
    assert len(samples) == 3 and unreadable == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.log", "root"]
    assert {path.name: path.read_text() for path in (root / "proc").iterdir()} == proc
    again = run("--root", root, "--out", tmp_path / "out.log", "--samples", 1, "--interval", 0, cwd=tmp_path)
    assert again.returncode == 0 and len((tmp_path / "out.log").read_text().splitlines()) == 6, "it appends"


def test_a_sample_that_cannot_be_taken_is_noted_and_does_not_end_the_day(tmp_path):
    root = a_root(tmp_path, meminfo="MemTotal: 4028000 kB\n")  # a kernel that does not report MemAvailable
    done = run("--root", root, "--out", tmp_path / "out.log", "--samples", 2, "--interval", 0)
    assert done.returncode == 0
    lines = (tmp_path / "out.log").read_text().splitlines()
    assert len(lines) == 3 and all(line.startswith("# a sample could not be taken at ") for line in lines[1:])
    assert parse("\n".join(lines)) == ([], 0)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [(["--samples", "many"], "--samples must be a whole number"), (["--interval", "1.5"], "--interval must be a whole"),
     (["--fast"], "unknown option --fast"), (["--out"], "--out needs a file")],
)  # fmt: skip
def test_it_refuses_what_it_does_not_understand(tmp_path, arguments, message):
    done = run("--root", a_root(tmp_path), *arguments)
    assert done.returncode == 2 and message in done.stderr and done.stdout == ""


def test_off_a_linux_host_it_says_so_instead_of_writing_nonsense(tmp_path):
    done = run("--root", tmp_path, "--out", tmp_path / "out.log")
    assert done.returncode == 2 and "this is for a Linux host" in done.stderr and not (tmp_path / "out.log").exists()


def test_help_is_the_header_of_the_script():
    done = run("--help")
    assert done.returncode == 0 and "OWNER ONLY" in done.stdout and "--dry-run" in done.stdout
    assert "set -eu" not in done.stdout and not done.stdout.startswith("#")
    assert os.access(SAMPLER, os.X_OK)
