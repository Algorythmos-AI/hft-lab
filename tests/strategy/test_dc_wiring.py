"""The Directional Change sleeve as a kind of the settings: placeholders in shadow, and a registered first run."""
from __future__ import annotations

from fractions import Fraction

import pytest
import yaml

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.shadow import __main__ as cli
from hftlab.strategy import ControlSleeve, IdleSleeve
from hftlab.strategy.dc import CONTRARIAN, TREND, DcSleeve
from hftlab.trader.config import load
from hftlab.trader.stages import NEVER
from hftlab.venues.sim import load_cost_model
from tests.support import ROOT
from tests.venues.kit import SECOND, plain_costs

SIZES = {"EUR.USD": 20_000}
MINUTE = 60 * SECOND
ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York
ENTRY = {
    "name": "directional-change", "kind": "directional_change", "price": "mid", "variant": "trend",
    "threshold_percent": "0.10", "overshoot_multiple": None, "max_hold_seconds": 3_600, "min_spread_ratio": "5",
    "warm_up_events": 2, "gap_seconds": 10, "retry_seconds": 10, "max_attempts": 1,
}  # fmt: skip


def build(entry: dict, costs=None) -> DcSleeve:
    (sleeve,) = cli.make_sleeves([entry], SIZES, costs=costs or plain_costs(), calendar=Calendar())
    assert isinstance(sleeve, DcSleeve)
    return sleeve


# ---- the shipped settings -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["shadow.yaml", "trader.yaml"])
def test_the_shipped_settings_hold_the_sleeve_with_every_number_marked_a_placeholder(name):
    text = (ROOT / "config" / name).read_text()
    entries = yaml.safe_load(text)["sleeves"]
    (entry,) = [entry for entry in entries if entry["kind"] == "directional_change"]
    assert entry == ENTRY, "the same placeholders in both files"
    lines = text.splitlines()
    first = lines.index("  - name: directional-change")
    block = lines[first + 2 : first + len(ENTRY)]
    assert [line.split(":")[0].strip() for line in block] == list(ENTRY)[2:]
    assert block[0].startswith("    price: mid") and "half the sum of bid and ask" in block[0]
    assert all("# PLACEHOLDER" in line for line in block[1:]), "the variant and all eight numbers"
    assert "IN PERCENT OF THE PRICE" in block[2], "the threshold's unit is said where it is written"
    sleeve = build(entry, load_cost_model(ROOT / "config" / "cost_model.yaml"))
    assert (sleeve.name, sleeve.variant, sleeve.threshold) == ("directional-change", TREND, Fraction(1, 1000))


def test_the_shipped_trader_keeps_it_in_shadow_with_no_rule_that_could_be_met():
    settings = load(ROOT / "config" / "trader.yaml")
    ladder = settings.rollout["directional-change"]
    assert ladder.start == "shadow" and ladder.to_min_size == NEVER and ladder.to_normal == NEVER


# ---- from settings to a sleeve --------------------------------------------------------------------------------------


def test_a_configured_sleeve_has_the_settings_as_written_converted_once_and_exactly():
    said = build({**ENTRY, "variant": "contrarian", "overshoot_multiple": "1.5", "threshold_percent": "0.025"})
    assert (said.variant, said.threshold, said.overshoot_multiple) == (CONTRARIAN, Fraction(1, 4000), Fraction(3, 2))
    assert (said.max_hold_ns, said.gap_ns, said.retry_ns, said.max_attempts) == (3_600 * SECOND, 10 * SECOND,
                                                                                  10 * SECOND, 1)  # fmt: skip
    assert said.min_spread_ratio == 5 and said.sizes == SIZES and said.settings()["price"] == "mid"
    assert said.settings()["warm_up"] == 2


def test_its_rollover_windows_are_the_cost_models_widened_and_no_fill_windows():
    windows = {"no_fill_before_ns": 5 * MINUTE, "no_fill_after_ns": 15 * MINUTE, "widen_before_ns": 30 * MINUTE,
               "widen_after_ns": 60 * MINUTE}  # fmt: skip
    costs = plain_costs(**windows)
    sleeve = build(ENTRY, costs)
    assert sleeve.rollover == ((5 * MINUTE, 15 * MINUTE), (30 * MINUTE, 60 * MINUTE))
    quote = Quote("EUR.USD", 108_500, 108_510, 1, 1, ROLLOVER, 1)
    seen = set()
    for minutes in range(-45, 75):
        at = ROLLOVER + minutes * MINUTE
        seen.add(costs.phase(at))
        assert sleeve._may_enter(quote, at) is (costs.phase(at) == "normal"), minutes
    assert seen == {"normal", "widened", "no_fill"}
    odd = ({**windows, "widen_before_ns": 0, "widen_after_ns": 0}, {**windows, "no_fill_after_ns": 90 * MINUTE})
    for other in odd:  # no widened window at all, or a no-fill window reaching past it: both are kept out of
        costs = plain_costs(**other)
        sleeve = build(ENTRY, costs)
        for minutes in range(-45, 100):
            at = ROLLOVER + minutes * MINUTE
            assert sleeve._may_enter(quote, at) is (costs.phase(at) == "normal"), minutes


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"threshold_percent": 0.1}, "quoted decimal"),
        ({"threshold_percent": "0"}, "above 0 and below 1"),
        ({"threshold_percent": "100"}, "above 0 and below 1"),
        ({"min_spread_ratio": 5}, "quoted decimal"),
        ({"min_spread_ratio": "0.5"}, "at least 1"),
        ({"min_spread_ratio": "five"}, "not a decimal"),
        ({"overshoot_multiple": "1"}, "contrarian"),
        ({"variant": "contrarian"}, "contrarian"),
        ({"variant": "contrarian", "overshoot_multiple": 1.5}, "quoted decimal"),
        ({"variant": "martingale"}, "trend or contrarian"),
        ({"max_hold_seconds": 1.5}, "whole numbers"),
        ({"max_attempts": 0}, "at least one entry order"),
        ({"gap_seconds": 0}, "gap time"),
        ({"price": "last"}, "mid"),
        ({"extra": 1}, "exactly the keys"),
    ],
)
def test_settings_that_are_wrong_are_refused_with_the_reason(change, message):
    with pytest.raises(cli.ConfigError, match=message):
        build({**ENTRY, **change})


def test_a_setting_left_out_is_refused_and_so_is_a_sleeve_with_no_cost_model_or_calendar_to_read():
    for key in ENTRY:
        if key not in ("name", "kind"):
            with pytest.raises(cli.ConfigError, match="exactly the keys"):
                build({k: v for k, v in ENTRY.items() if k != key})
    for given in ({}, {"costs": plain_costs()}, {"calendar": Calendar()}):
        with pytest.raises(cli.ConfigError, match="needs the cost model and the calendar"):
            cli.make_sleeves([ENTRY], SIZES, **given)
    control = {"name": "control", "kind": "control", "seed": 1, "entry_per_million": 1, "hold_seconds": 1,
               "retry_seconds": 1}  # fmt: skip
    assert len(cli.make_sleeves([{"name": "idle", "kind": "idle"}, control], SIZES)) == 2, "the others need neither"


# ---- the registered first run ---------------------------------------------------------------------------------------

FOLDER = ROOT / "research" / "experiments" / "HFT-EXP-0005-directional-change-shadow"


def test_the_registered_first_run_is_a_config_the_runner_accepts_and_it_has_no_result():
    config = cli.load_config(FOLDER / "config.yaml")
    sleeves = cli.make_sleeves(config["sleeves"], SIZES, costs=plain_costs(), calendar=Calendar())
    idle, control, *ours = sleeves
    assert isinstance(idle, IdleSleeve) and isinstance(control, ControlSleeve) and config["seed"] == 1
    assert (control.seed, control.entry_per_million, control.hold_ns) == (1, 2_000, 60 * SECOND), "as in HFT-EXP-0001"
    assert all(isinstance(sleeve, DcSleeve) for sleeve in ours)
    assert [(s.name, s.variant, s.threshold, s.overshoot_multiple) for s in ours] == [
        ("dc-trend-005", TREND, Fraction(1, 2000), None),
        ("dc-trend-010", TREND, Fraction(1, 1000), None),
        ("dc-trend-020", TREND, Fraction(1, 500), None),
        ("dc-contra-005", CONTRARIAN, Fraction(1, 2000), Fraction(1)),
        ("dc-contra-010", CONTRARIAN, Fraction(1, 1000), Fraction(1)),
        ("dc-contra-020", CONTRARIAN, Fraction(1, 500), Fraction(1)),
    ], "both variants at each of the three thresholds"
    shared = {(s.max_hold_ns, s.min_spread_ratio, s.gap_ns, s.retry_ns, s.max_attempts) for s in ours}
    assert shared == {(4 * 3_600 * SECOND, Fraction(5), 10 * SECOND, 10 * SECOND, 2)}
    assert {s.settings()["price"] for s in ours} == {"mid"} and {s.settings()["warm_up"] for s in ours} == {2}
    assert config["measure"]["null_walks"] == {"count": 99, "seed": 1, "per": "recorded_day"}
    protocol = (FOLDER / "protocol.md").read_text()
    for stated in (
        "Not run.", "It tests HFT-HYP-0005.", "**Expected outcome: no advantage.**", "0.05, 0.10 and 0.20 percent",
        "**gross:**", "**net of cost:**", "for every threshold tested", "the control sleeve's", "99 random walks",
        "has seen no real data", "**not built**",
    ):  # fmt: skip
        assert stated in protocol, stated
    assert sorted(path.name for path in FOLDER.iterdir()) == ["config.yaml", "protocol.md"], "no result: none exists"


def test_the_hypothesis_is_registered_expects_no_advantage_and_has_no_evidence():
    path = ROOT / "research" / "hypotheses" / "HFT-HYP-0005-directional-change-baselines-have-no-advantage.yaml"
    record = yaml.safe_load(path.read_text())
    assert (record["id"], record["status"], record["tested_by"]) == ("HFT-HYP-0005", "registered", ["HFT-EXP-0005"])
    assert "no advantage" in record["title"] and "This is the outcome we expect." in record["statement"]
    assert record["evidence"].startswith("None.") and "random-walk null" in record["statement"]
