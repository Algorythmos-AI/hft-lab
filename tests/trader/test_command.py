"""`python -m hftlab.trader`: the shipped settings trade nothing on paper, and the command says what it will not do."""
from __future__ import annotations

import json

import pytest
import yaml

from hftlab.journal import read
from hftlab.lineup.serve import places
from hftlab.trader import __main__ as cli
from hftlab.trader.config import TraderConfigError, digest, load, parse
from hftlab.trader.stages import NEVER
from tests.support import PLACEHOLDER, ROOT
from tests.trader.kit import raw_settings

SHIPPED = ROOT / "config" / "trader.yaml"
CONFIG = ROOT / "config"
PLACES = ("ml", "ml-challenger-1", "ml-challenger-2", "ml-challenger-3")  # the champion's and three challengers'
# What the command is run on here: the settings, limits and cost model as they were before any decision record
# (tests/placeholder). Limits not in force, so no run of these tests holds anything that could send an order.
BEFORE = PLACEHOLDER / "trader.yaml"
FILES = ["--config", str(BEFORE), "--risk", str(PLACEHOLDER / "risk.yaml"),
         "--cost-model", str(PLACEHOLDER / "cost_model.yaml"), "--calendar", str(CONFIG / "calendar")]  # fmt: skip


def run(tmp_path, *more: str) -> int:
    return cli.main([*FILES, "--journals", str(tmp_path / "trader"), "--state-dir", str(tmp_path), *more])


# ---- the shipped settings --------------------------------------------------------------------------------------------


def test_the_shipped_settings_start_every_sleeve_in_shadow_and_only_the_control_sleeve_has_a_rule(tmp_path):
    settings = load(SHIPPED)
    assert settings.placeholder and settings.venue == "ibkr_paper" and settings.fsync is True
    assert settings.names == ("idle", "control", *PLACES, "directional-change")
    assert [pair for pair, _ in settings.pairs] == ["EUR.USD", "AUD.USD", "GBP.USD", "USD.JPY"]
    assert settings.clean_day_ns == 480 * 60 * 10**9
    assert places(settings.sleeves) == {
        "ml": ("champion", 0), "ml-challenger-1": ("challenger", 0), "ml-challenger-2": ("challenger", 1),
        "ml-challenger-3": ("challenger", 2),
    }, "four places and no model: which model stands in a place is the lineup journal's to say"  # fmt: skip
    for entry in settings.sleeves:
        if entry["name"] in PLACES:
            assert (entry["margin_micro"], entry["retry_seconds"], entry["scorer"]) == (0, 10, "in_process")
    assert SHIPPED.read_text().count("# PLACEHOLDER margin") == 4, "every place's margin is a placeholder"
    assert all("model" not in entry for entry in settings.sleeves) and str(settings.registry) == "var/learn"
    for name in settings.names:
        ladder = settings.rollout[name]
        assert ladder.start == "shadow" and ladder.to_normal == NEVER, name
        if name == "control":
            assert (ladder.to_min_size.clean_days, ladder.to_min_size.decision) == (1, "HFT-DEC-0003")
        else:
            assert ladder.to_min_size == NEVER, name
    text = SHIPPED.read_text()
    assert text.count("PLACEHOLDER: never met") == 13 and "ONE SLEEVE CAN REACH THE PAPER ACCOUNT" in text
    assert (ROOT / "research" / "decisions" / "HFT-DEC-0003-control-sleeve-to-minimum-size.md").is_file()
    control = next(entry for entry in settings.sleeves if entry["name"] == "control")
    assert (control["entry_per_million"], control["hold_seconds"], control["retry_seconds"]) == (50, 60, 10)


def test_the_shipped_files_agree_with_each_other_on_the_pairs_and_the_sleeve_that_may_trade():
    settings, risk = load(SHIPPED), cli.limits(CONFIG / "risk.yaml")
    assert risk is not None and risk.inventory is not None and risk.decision == "HFT-DEC-0001"
    traded = {pair for pair, _ in settings.pairs}
    assert traded == set(risk.pairs) | risk.inventory.shadow_only, "every pair is on paper or on the shadow venue alone"
    assert set(risk.sleeves) == {"control"}, "the one sleeve with a rule has limits; no other can pass the gate"
    costs = cli.load_cost_model(CONFIG / "cost_model.yaml")
    for pair, least in risk.inventory.minimum_entry.items():
        assert least == costs.least(pair[:3]), f"{pair}: the gate's minimum entry is the cost model's minimum size"
        assert risk.sleeves["control"].max_position >= least and risk.pairs[pair].max_position == 2 * least
    recorded = yaml.safe_load((CONFIG / "recorder.yaml").read_text())["pairs"]
    assert {entry["pair"] for entry in recorded} == traded, "what is traded is recorded"


def test_the_settings_as_they_were_before_any_decision_hold_no_rule_that_could_be_met():
    settings = load(BEFORE)
    assert settings.clean_day_ns is None
    for name in settings.names:
        ladder = settings.rollout[name]
        assert ladder.start == "shadow" and ladder.to_min_size == NEVER and ladder.to_normal == NEVER


def test_limits_that_are_not_in_force_run_in_shadow_only_mode_and_say_so(tmp_path, capsys):
    assert run(tmp_path, "--dry-run-seconds", "20") == 0
    said = capsys.readouterr()
    assert "SHADOW-ONLY: the risk limits are not in force. No order can reach a broker" in said.err
    assert "PLACEHOLDER" in said.err
    result = json.loads(said.out.strip().splitlines()[-1])
    assert result["mode"] == "shadow_only" and result["books"]["broker"] is None
    assert [sleeve["stage"] for sleeve in result["sleeves"]] == ["shadow"] * 7
    assert read(next((tmp_path / "trader" / "days").iterdir()) / "decisions.jsonl")[1][0]["serving"] == dict.fromkeys(
        PLACES
    ), "nothing is registered: every place runs idle"
    assert not (tmp_path / "trader" / "outbox.jsonl").exists() and not (tmp_path / "trader" / "risk.jsonl").exists()
    (day,) = (tmp_path / "trader" / "days").iterdir()
    assert read(day / "decisions.jsonl")[1][0]["mode"] == "shadow_only"

    assert run(tmp_path, "--replay", str(day)) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert [(one["ended"], one["identical"]) for one in replayed["sessions"]] == [("stopped", True)]

    assert run(tmp_path, "--stages") == 0
    assert capsys.readouterr().out.splitlines() == [
        "control: shadow; 0 clean trading day(s) in a row in this stage",
        "directional-change: shadow; 0 clean trading day(s) in a row in this stage",
        "idle: shadow; 0 clean trading day(s) in a row in this stage",
        "ml: shadow; 0 clean trading day(s) in a row in this stage",
        "ml-challenger-1: shadow; 0 clean trading day(s) in a row in this stage",
        "ml-challenger-2: shadow; 0 clean trading day(s) in a row in this stage",
        "ml-challenger-3: shadow; 0 clean trading day(s) in a row in this stage",
    ]


def test_it_refuses_to_start_on_limits_it_cannot_read(tmp_path, capsys):
    broken = tmp_path / "risk.yaml"
    broken.write_text((PLACEHOLDER / "risk.yaml").read_text().replace("in_force: false", "in_force: maybe"))
    arguments = [*FILES, "--journals", str(tmp_path / "trader"), "--state-dir", str(tmp_path), "--dry-run-seconds", "5"]
    arguments[arguments.index("--risk") + 1] = str(broken)
    assert cli.main(arguments) == 2
    assert "refused" in capsys.readouterr().err and not (tmp_path / "trader").exists()
    arguments[arguments.index("--risk") + 1] = str(tmp_path / "missing.yaml")
    assert cli.main(arguments) == 2


def test_it_refuses_a_venue_that_is_not_the_registered_paper_venue(tmp_path, capsys):
    for name in ("sim", "ibkr_live", "ibkr"):
        other = tmp_path / f"{name}.yaml"
        other.write_text(BEFORE.read_text().replace("venue: ibkr_paper", f"venue: {name}"))
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
