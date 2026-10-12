"""`deploy/bin/hft-paper-local`: the recorder and the trader on a laptop, against a paper gateway on that laptop.

The script is run here with a stand-in for the interpreter, in a temporary checkout. No gateway, no broker
library and no trader: what is tested is what the script itself decides, starts and stops.
"""
from __future__ import annotations

import os
import signal
import stat
import subprocess
import time

import pytest

from tests.support import ROOT

SCRIPT = ROOT / "deploy" / "bin" / "hft-paper-local"
# Stands where the interpreter would: writes down how it was called, then waits to be told to stop, like a service.
STAND_IN = """#!/bin/sh
name=$(echo "$2" | tr . _)
echo "$MODE|$PYTHONPATH|$HFT_STATE_DIR|$*" > "$HFT_ROOT/called_$name"
trap 'echo stopped > "$HFT_ROOT/stopped_$name"; exit 0' TERM
trap '' INT
[ -f "$HFT_ROOT/refuse_$name" ] && { sleep 1; exit 2; }  # long enough for the other one to be up
while :; do sleep 0.1; done
"""


def checkout(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trader.yaml").write_text("status: placeholder\n")
    stand_in = tmp_path / "python"
    stand_in.write_text(STAND_IN)
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
    env = {key: value for key, value in os.environ.items() if key != "MODE"}
    return {**env, "HFT_ROOT": str(tmp_path), "HFT_PYTHON": str(stand_in)}


def started(tmp_path, env, *words):
    child = subprocess.Popen([str(SCRIPT), *words], env=env, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if (tmp_path / "called_hftlab_trader").exists():
            return child
        time.sleep(0.05)
    child.kill()
    raise AssertionError(child.stderr.read())


def test_it_is_executable_and_names_one_gateway_and_no_other_port():
    text = SCRIPT.read_text()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR and text.startswith("#!/bin/sh\n")
    assert "PORT=4002" in text and text.count("127.0.0.1") >= 3
    assert "sudo" not in text.replace("no sudo", "") and "systemctl" not in text
    assert "caffeinate" in text


def test_a_dry_run_prints_the_two_commands_and_starts_nothing(tmp_path):
    env = checkout(tmp_path)
    done = subprocess.run([str(SCRIPT), "--dry-run"], env=env, capture_output=True, text=True)
    assert done.returncode == 0 and "DRY RUN, nothing is run" in done.stdout
    assert "-m hftlab.record --host 127.0.0.1 --port 4002" in done.stdout
    assert "-m hftlab.trader --host 127.0.0.1 --port 4002" in done.stdout and "MODE=paper" in done.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config", "python"], "no var/, no process"


@pytest.mark.parametrize("words", [["--port", "4001"], ["--host", "example.invalid"], ["--mode", "other"], ["x"]])
def test_it_takes_no_host_no_port_and_no_other_word(tmp_path, words):
    done = subprocess.run([str(SCRIPT), *words], env=checkout(tmp_path), capture_output=True, text=True)
    assert done.returncode == 2 and "refused" in done.stderr and not (tmp_path / "var").exists()


def test_it_refuses_a_mode_that_is_not_paper_and_a_place_that_is_no_checkout(tmp_path):
    env = checkout(tmp_path)
    done = subprocess.run([str(SCRIPT)], env={**env, "MODE": "other"}, capture_output=True, text=True)
    assert done.returncode == 2 and "MODE is set and is not 'paper'" in done.stderr
    nowhere = str(tmp_path / "none")
    done = subprocess.run([str(SCRIPT)], env={**env, "HFT_ROOT": nowhere}, capture_output=True, text=True)
    assert done.returncode == 2 and "not a checkout" in done.stderr
    done = subprocess.run([str(SCRIPT)], env={**env, "HFT_PYTHON": nowhere}, capture_output=True, text=True)
    assert done.returncode == 2 and "make bootstrap" in done.stderr
    assert not (tmp_path / "var").exists() and not list(tmp_path.glob("called_*"))


@pytest.mark.parametrize("how", [signal.SIGINT, signal.SIGTERM])
def test_it_starts_both_on_the_paper_port_and_one_signal_stops_both_cleanly(tmp_path, how):
    child = started(tmp_path, checkout(tmp_path))
    for _ in range(100):
        if (tmp_path / "called_hftlab_record").exists():
            break
        time.sleep(0.05)
    for name in ("trader", "record"):
        mode, path, state, words = (tmp_path / f"called_hftlab_{name}").read_text().strip().split("|")
        assert (mode, path, state) == ("paper", "src", "var"), "the paper mode, and its state under var/"
        assert words == f"-m hftlab.{name} --host 127.0.0.1 --port 4002"
    assert (tmp_path / "var" / "log").is_dir()
    child.send_signal(how)
    assert child.wait(timeout=20) == 0
    said = child.stderr.read()
    assert "stopped (trader 0, recorder 0)" in said and "positions are NOT closed" in said
    assert (tmp_path / "stopped_hftlab_trader").exists() and (tmp_path / "stopped_hftlab_record").exists()


def test_the_trader_alone_when_asked(tmp_path):
    child = started(tmp_path, checkout(tmp_path), "--no-recorder")
    time.sleep(0.3)
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=20) == 0 and not (tmp_path / "called_hftlab_record").exists()


def test_a_trader_that_refuses_to_start_takes_the_recorder_down_with_it_and_its_code_is_the_scripts(tmp_path):
    env = checkout(tmp_path)
    (tmp_path / "refuse_hftlab_trader").write_text("")
    child = subprocess.Popen([str(SCRIPT)], env=env, stderr=subprocess.PIPE, text=True)
    assert child.wait(timeout=20) == 2
    assert "the trader ended by itself with code 2" in child.stderr.read()
    assert (tmp_path / "stopped_hftlab_record").exists(), "nothing is left running behind a refusal"
