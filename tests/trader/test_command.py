"""`python -m hftlab.trader`: the shipped settings trade nothing on paper, and the command says what it will not do."""
from __future__ import annotations

import json

import pytest
import yaml

from hftlab.journal import read
from hftlab.trader import __main__ as cli
from hftlab.trader.config import TraderConfigError, digest, load, parse
from hftlab.trader.stages import NEVER
from tests.support import ROOT
from tests.trader.kit import raw_settings

SHIPPED = ROOT / "config" / "trader.yaml"
CONFIG = ROOT / "config"
FILES = ["--config", str(SHIPPED), "--risk", str(CONFIG / "risk.yaml"), "--cost-model", str(CONFIG / "cost_model.yaml"),
         "--calendar", str(CONFIG / "calendar")]  # fmt: skip


def run(tmp_path, *more: str) -> int:
    return cli.main([*FILES, "--journals", str(tmp_path / "trader"), "--state-dir", str(tmp_path), *more])


# ---- the shipped settings --------------------------------------------------------------------------------------------


def test_the_shipped_settings_keep_every_sleeve_in_shadow_and_hold_no_rule_that_could_be_met():
    settings = load(SHIPPED)
    assert settings.placeholder and settings.venue == "ibkr_paper" and settings.fsync is True
    assert settings.names == ("idle", "control") and settings.clean_day_ns is None
    for name in settings.names:
        ladder = settings.rollout[name]
        assert ladder.start == "shadow" and ladder.to_min_size == NEVER and ladder.to_normal == NEVER
    text = SHIPPED.read_text()
    assert text.count("PLACEHOLDER: never met") == 4 and "AS SHIPPED THIS FILE TRADES NOTHING ON PAPER" in text
    assert yaml.safe_load(text)["status"] == "placeholder"


def test_the_shipped_settings_run_in_shadow_only_mode_and_say_so(tmp_path, capsys):
    assert run(tmp_path, "--dry-run-seconds", "20") == 0
    said = capsys.readouterr()
    assert "SHADOW-ONLY: the risk limits are not in force. No order can reach a broker" in said.err
    assert "PLACEHOLDER" in said.err
    result = json.loads(said.out.strip().splitlines()[-1])
    assert result["mode"] == "shadow_only" and result["books"]["broker"] is None
    assert [sleeve["stage"] for sleeve in result["sleeves"]] == ["shadow", "shadow"]
    assert not (tmp_path / "trader" / "outbox.jsonl").exists() and not (tmp_path / "trader" / "risk.jsonl").exists()
    (day,) = (tmp_path / "trader" / "days").iterdir()
    assert read(day / "decisions.jsonl")[1][0]["mode"] == "shadow_only"

    assert run(tmp_path, "--replay", str(day)) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert [(one["ended"], one["identical"]) for one in replayed["sessions"]] == [("stopped", True)]

    assert run(tmp_path, "--stages") == 0
    assert capsys.readouterr().out.splitlines() == [
        "control: shadow; 0 clean trading day(s) in a row in this stage",
        "idle: shadow; 0 clean trading day(s) in a row in this stage",
    ]


def test_it_refuses_to_start_on_limits_it_cannot_read(tmp_path, capsys):
    broken = tmp_path / "risk.yaml"
    broken.write_text((ROOT / "config" / "risk.yaml").read_text().replace("in_force: false", "in_force: maybe"))
    arguments = [*FILES, "--journals", str(tmp_path / "trader"), "--state-dir", str(tmp_path), "--dry-run-seconds", "5"]
    arguments[arguments.index("--risk") + 1] = str(broken)
    assert cli.main(arguments) == 2
    assert "refused" in capsys.readouterr().err and not (tmp_path / "trader").exists()
    arguments[arguments.index("--risk") + 1] = str(tmp_path / "missing.yaml")
    assert cli.main(arguments) == 2


def test_it_refuses_a_venue_that_is_not_the_registered_paper_venue(tmp_path, capsys):
    for name in ("sim", "ibkr_live", "ibkr"):
        other = tmp_path / f"{name}.yaml"
        other.write_text(SHIPPED.read_text().replace("venue: ibkr_paper", f"venue: {name}"))
        assert cli.main(["--config", str(other), *FILES[2:], "--journals", str(tmp_path / "t")]) == 2, name
        assert "refused" in capsys.readouterr().err


def test_a_second_trader_on_the_same_state_directory_is_refused(tmp_path, capsys):
    with cli.only_one(tmp_path / "trader"):
        assert run(tmp_path, "--dry-run-seconds", "5") == 2
    assert "another trader holds" in capsys.readouterr().err


def test_a_request_needs_a_name_and_is_left_as_a_file_for_the_running_trader(tmp_path, capsys):
    assert run(tmp_path, "--reset", "ORDER_FAULT") == 2
    assert "--operator NAME is needed" in capsys.readouterr().err
    assert run(tmp_path, "--reset", "ORDER_FAULT", "--operator", "owner") == 0
    assert run(tmp_path, "--demote", "control", "off", "--operator", "owner") == 0
    for sleeve, stage in (("control", "normal"), ("control", "min_size"), ("nobody", "off")):
        assert run(tmp_path, "--demote", sleeve, stage, "--operator", "owner") == 2, "never up, never a stranger"
    left = sorted((tmp_path / "trader" / "requests").iterdir())
    assert [json.loads(path.read_text())["what"] for path in left] == ["reset", "stage"]
    asked = json.loads(left[1].read_text())
    assert asked == {"what": "stage", "subject": "control", "value": "off", "operator": "owner"}


# ---- the loader ------------------------------------------------------------------------------------------------------


def test_the_loader_refuses_a_key_it_does_not_define_and_a_sleeve_without_a_ladder():
    with pytest.raises(TraderConfigError, match=r"not keys of this file: \['stage'\]"):
        parse({**raw_settings(), "stage": "normal"})
    without = raw_settings()
    del without["rollout"]["control"]
    with pytest.raises(TraderConfigError, match="one entry for each sleeve"):
        parse(without)
    for ladder in ({"start": "min_size", "promote": {}}, {"start": "shadow"}):
        wrong = raw_settings()
        wrong["rollout"]["control"] = ladder
        with pytest.raises(TraderConfigError):
            parse(wrong)
    started_high = raw_settings()
    started_high["rollout"]["control"]["start"] = "normal"
    with pytest.raises(TraderConfigError, match="starts in shadow or off"):
        parse(started_high)
    for key, value in (("poll_ms", 0), ("beat_ms", 1.5), ("reconcile_seconds", True), ("fsync", "yes"), ("pairs", [])):
        with pytest.raises(TraderConfigError):
            parse({**raw_settings(), key: value})


def test_the_digest_changes_when_any_file_a_decision_depends_on_changes(tmp_path):
    one, two = tmp_path / "a.yaml", tmp_path / "b.yaml"
    one.write_text("x: 1\n")
    two.write_text("y: 2\n")
    first = digest([one, two])
    assert digest([two, one]) == first, "the order they are named in does not matter"
    two.write_text("y: 3\n")
    assert digest([one, two]) != first
