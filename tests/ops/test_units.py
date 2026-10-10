"""The systemd units: every one is ours alone, capped, on paper, and exactly what the repository holds.

The units are text that a service manager on a shared host will obey, so each rule the plan made is checked on
the rendered text itself: the slice and its cap, the user, the mode, no flag for live trading, no name that
could be another desk's, no credential, and a restart that backs off instead of spinning.
"""
from __future__ import annotations

import importlib.util
import re

import pytest

from hftlab.ops import host, units
from hftlab.record import recorder
from tests.support import ROOT

RENDERED = units.render_all()
SERVICES = {name: text for name, text in RENDERED.items() if name.endswith(".service")}
TIMERS = {name: text for name, text in RENDERED.items() if name.endswith(".timer")}
OURS = {name: text for name, text in SERVICES.items() if name != units.GATEWAY}  # the gateway is not our program
COMMITTED = ROOT / "deploy" / "systemd"


def values(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}=")]


def section(text: str, name: str) -> list[str]:
    body = text.split(f"[{name}]\n", 1)[1] if f"[{name}]\n" in text else ""
    return [line for line in body.split("\n[", 1)[0].splitlines() if line]


# ---- what the plan decided, on every unit ---------------------------------------------------------------------------


def test_there_are_exactly_these_units():
    assert sorted(RENDERED) == [
        "hft-backup.service", "hft-backup.timer", "hft-gateway.service", "hft-housekeep.service",
        "hft-housekeep.timer", "hft-publish.service", "hft-publish.timer", "hft-recorder.service",
        "hft-shadow.service", "hft.slice",
    ]  # fmt: skip


def test_every_name_is_this_platforms_and_none_could_be_another_desks():
    for name, text in RENDERED.items():
        assert name == host.SLICE or name.startswith(host.UNIT_PREFIX), name
        assert not name.startswith(host.OTHER_DESKS_UNIT_PREFIX), name
        assert host.OTHER_DESKS_UNIT_PREFIX not in text, f"{name} names a unit of the other desks"
    assert host.UNIT_PREFIX != host.OTHER_DESKS_UNIT_PREFIX and host.SLICE == "hft.slice"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_is_in_the_slice_runs_as_hft_and_is_on_paper(name):
    text = SERVICES[name]
    assert values(text, "Slice") == ["hft.slice"]
    assert values(text, "User") == ["hft"] and values(text, "Group") == ["hft"]
    assert [v for v in values(text, "Environment") if v.startswith("MODE=")] == ["MODE=paper"]
    assert values(text, "WorkingDirectory")[0].startswith("/home/hft")


@pytest.mark.parametrize("name", sorted(RENDERED))
def test_no_unit_sets_a_flag_for_live_trading_or_any_other_mode(name):
    text = RENDERED[name]
    assert "LIVE" not in text and not re.search(r"(?i)\blive\b", text), "no unit so much as names it"
    assert "TRADING_ENABLED" not in text
    assert not re.search(r"MODE=(?!paper$)", text, re.MULTILINE), "the one mode a unit ever gives is paper"


def test_the_two_services_that_reach_the_gateway_load_no_file_that_could_change_their_mode():
    for name in ("hft-recorder.service", "hft-shadow.service"):
        assert values(SERVICES[name], "EnvironmentFile") == [], name
        assert values(SERVICES[name], "IPAddressDeny") == ["any"], name
        assert values(SERVICES[name], "IPAddressAllow") == ["localhost"], "the gateway on this machine, nothing else"


def test_no_unit_holds_a_credential_and_only_the_publisher_loads_the_owners_secrets_file():
    for name, text in RENDERED.items():
        assert not re.search(r"(?i)password|passwd|secret=|token|api[_-]?key|login=|username", text), name
        for value in values(text, "Environment"):
            assert value.split("=")[0] in {
                "MODE", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "TZ", "HFT_STATE_DIR",
                "HFT_BACKUP_DIR", "HFT_GATEWAY_CONFIG",
            }, f"{name}: {value}"  # fmt: skip
    loaders = [name for name, text in RENDERED.items() if values(text, "EnvironmentFile")]
    assert loaders == ["hft-publish.service"]
    assert values(SERVICES["hft-publish.service"], "EnvironmentFile") == [f"-{host.SECRETS_FILE}"]


def test_the_slice_caps_the_whole_platform_and_gives_it_a_low_share():
    text = RENDERED["hft.slice"]
    assert values(text, "MemoryMax") == ["1200M"], "the plan's cap of about 1.2 GB"
    assert values(text, "MemorySwapMax") == ["0"], "ours never goes to the swap the other desks share"
    assert int(values(text, "MemoryHigh")[0][:-1]) < 1200
    assert int(values(text, "CPUWeight")[0]) < 100 and int(values(text, "IOWeight")[0]) < 100, "100 is the default"


def test_each_of_our_services_has_its_own_fuse_under_the_cap_and_dies_before_the_other_desks():
    scores = {}
    for name, text in SERVICES.items():
        fuse = int(values(text, "MemoryMax")[0].removesuffix("M"))
        assert 0 < fuse < 1200, name
        scores[name] = int(values(text, "OOMScoreAdjust")[0])
        assert scores[name] > 0, f"{name} would not be killed before a unit of the other desks"
    assert min(scores, key=scores.__getitem__) == "hft-recorder.service", "the recorder is the last of ours to go"


@pytest.mark.parametrize("name", sorted(OURS))
def test_our_services_see_a_read_only_system_their_own_state_and_not_the_other_desks_home(name):
    text = OURS[name]
    assert values(text, "ProtectSystem") == ["strict"] and values(text, "NoNewPrivileges") == ["yes"]
    assert values(text, "InaccessiblePaths") == ["-/home/wt"]
    writable = values(text, "ReadWritePaths")[0].split()
    assert writable[0] == host.STATE_DIR and set(writable) <= {host.STATE_DIR, host.BACKUP_DIR}
    assert values(text, "CapabilityBoundingSet") == [""] and values(text, "LimitCORE") == ["0"]


def test_only_the_publisher_may_reach_the_internet():
    reach = {spec.unit: spec.reach for spec in units.SERVICES}
    assert reach == {
        "hft-recorder.service": "gateway", "hft-shadow.service": "gateway", "hft-housekeep.service": "none",
        "hft-backup.service": "none", "hft-publish.service": "out",
    }  # fmt: skip
    for name, how in reach.items():
        private = values(OURS[name], "PrivateNetwork") == ["yes"]
        fenced = values(OURS[name], "IPAddressDeny") == ["any"]
        assert (how == "none") == private and (how == "gateway") == fenced, name


# ---- what runs, and how it restarts ---------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", units.SERVICES, ids=lambda spec: spec.name)
def test_every_command_is_the_installed_python_running_a_module_that_exists(spec):
    text = OURS[spec.unit]
    for line in values(text, "ExecStart") + [v.removeprefix("-") for v in values(text, "ExecStartPre")]:
        words = line.split()
        assert words[0] == "/home/hft/hft-lab/.venv/bin/python" and words[1] == "-m", line
        assert importlib.util.find_spec(words[2]) is not None, f"{spec.unit} runs {words[2]}, which is not there"
    assert len(values(text, "ExecStart")) == 1
    assert values(text, "Environment").count("PYTHONPATH=src") == 1


def test_the_commands_are_the_ones_the_brief_names():
    commands = {name: values(text, "ExecStart")[0].split(" ", 1)[1] for name, text in OURS.items()}
    assert commands == {
        "hft-recorder.service": "-m hftlab.record",
        "hft-housekeep.service": "-m hftlab.record --housekeep",
        "hft-shadow.service": "-m hftlab.shadow --venue ibkr_paper",
        "hft-publish.service": "-m hftlab.publish --send",
        "hft-backup.service": "-m hftlab.ops.backup run",
    }
    before = values(OURS["hft-publish.service"], "ExecStartPre")
    assert before == ["-/home/hft/hft-lab/.venv/bin/python -m hftlab.ops.status --alert --quiet"]
    assert before[0].startswith("-"), "a failing status check must not keep the snapshot from being sent"


def test_the_recorder_restarts_with_back_off_stops_on_sigterm_and_is_not_restarted_when_it_was_refused():
    text = SERVICES["hft-recorder.service"]
    assert values(text, "Restart") == ["on-failure"]
    first, last = int(values(text, "RestartSec")[0]), int(values(text, "RestartMaxDelaySec")[0])
    assert 0 < first < last <= 600 and int(values(text, "RestartSteps")[0]) > 1, "the wait grows: no crash loop"
    assert values(text, "StartLimitIntervalSec") == ["0"], "and it is never given up on"
    assert values(text, "KillSignal") == ["SIGTERM"] and int(values(text, "TimeoutStopSec")[0]) >= 10
    prevented = [int(code) for code in values(text, "RestartPreventExitStatus")[0].split()]
    assert prevented == [2], "refused or misconfigured needs a person, not a restart"
    assert recorder.EXIT_QUOTA not in prevented and recorder.EXIT_IO not in prevented
    assert recorder.EXIT_OK == 0, "asked to stop: `on-failure` leaves it stopped"


def test_the_shadow_runner_is_not_restarted_onto_a_journal_that_does_not_verify():
    assert values(SERVICES["hft-shadow.service"], "RestartPreventExitStatus") == ["1 2"]


def test_every_timer_starts_a_service_that_exists_and_jobs_do_not_restart():
    assert sorted(TIMERS) == ["hft-backup.timer", "hft-housekeep.timer", "hft-publish.timer"]
    for name, text in TIMERS.items():
        target = values(text, "Unit")[0]
        assert target == name.replace(".timer", ".service") and target in SERVICES
        assert section(text, "Install") == ["WantedBy=timers.target"] and len(values(text, "OnCalendar")) == 1
        assert values(SERVICES[target], "Type") == ["oneshot"] and not values(SERVICES[target], "Restart")
        assert "[Install]" not in SERVICES[target], "a job is started by its timer, never enabled by itself"
    assert values(TIMERS["hft-backup.timer"], "OnCalendar")[0].endswith("America/New_York")


def test_the_owner_may_enable_the_two_daemons_and_the_timers_and_nothing_else():
    assert units.can_be_enabled() == [
        "hft-backup.timer", "hft-housekeep.timer", "hft-publish.timer", "hft-recorder.service", "hft-shadow.service",
    ]  # fmt: skip
    for name in ("hft-recorder.service", "hft-shadow.service"):
        assert section(SERVICES[name], "Install") == ["WantedBy=multi-user.target"], "it comes back after a reboot"


def test_the_backup_job_is_skipped_until_the_owner_has_made_its_directory():
    text = SERVICES["hft-backup.service"]
    assert values(text, "ConditionPathIsDirectory") == [host.BACKUP_DIR]
    assert f"HFT_BACKUP_DIR={host.BACKUP_DIR}" in values(text, "Environment")


# ---- the gateway placeholder ----------------------------------------------------------------------------------------


def test_the_gateway_is_described_cannot_be_enabled_and_holds_nothing_of_the_login():
    text = SERVICES[units.GATEWAY]
    described = values(text, "Description")[0]
    assert "PLACEHOLDER" in described and "third-party controller" in described
    assert "[Install]" not in text, "there is nothing for `systemctl enable` to act on"
    assert values(text, "ConditionPathExists") == ["/etc/hft/gateway/config.ini"]
    assert values(text, "ConditionFileIsExecutable") == ["/etc/hft/gateway/start"]
    assert values(text, "ExecStart") == ["/etc/hft/gateway/start"], "the owner's own few lines, not ours"
    assert "HFT_GATEWAY_CONFIG=/etc/hft/gateway/config.ini" in values(text, "Environment")
    assert not (ROOT / "deploy" / "gateway").exists(), "and no such configuration is in the repository"


# ---- stable text ----------------------------------------------------------------------------------------------------


def test_the_committed_unit_files_are_exactly_the_rendered_ones():
    assert sorted(path.name for path in COMMITTED.iterdir()) == sorted(RENDERED)
    for name, text in RENDERED.items():
        assert (COMMITTED / name).read_text(encoding="utf-8") == text, (
            f"deploy/systemd/{name} differs from the code: run `python -m hftlab.ops.units render --out deploy/systemd`"
        )


def test_rendering_is_the_same_every_time_and_holds_nothing_of_this_machine():
    assert units.render_all() == RENDERED
    for name, text in RENDERED.items():
        assert str(ROOT) not in text and "/Users/" not in text and "/tmp" not in text, name
        assert text.endswith("\n") and "\n\n\n" not in text and "\t" not in text
        for line in text.splitlines():
            assert not line or line.startswith("[") or re.fullmatch(r"[A-Za-z]+=.*", line), f"{name}: {line!r}"


def test_the_command_renders_lists_and_compares(tmp_path, capsys):
    assert units.main(["render", "--out", str(tmp_path / "out")]) == 0
    assert {path.name: path.read_text() for path in (tmp_path / "out").iterdir()} == RENDERED
    assert units.main(["diff", "--unit-dir", str(tmp_path / "out")]) == 0
    assert "match the code" in capsys.readouterr().out
    (tmp_path / "out" / "hft.slice").write_text("[Slice]\nMemoryMax=4G\n")
    (tmp_path / "out" / "hft-backup.timer").unlink()
    assert units.main(["diff", "--unit-dir", str(tmp_path / "out")]) == 1
    differing = ["hft-backup.timer: not installed", "hft.slice: differs from the code"]
    assert capsys.readouterr().out.splitlines() == differing
    assert units.main(["list"]) == 0 and capsys.readouterr().out.split() == units.can_be_enabled()
