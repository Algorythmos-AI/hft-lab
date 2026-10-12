"""No stage above shadow while the capability file does not show that the account can open what a sleeve needs.

The probe's second real run (HFT-EXP-0008) showed that the paper account refuses a spot order that opens a
position from flat. The trader reads that from the capability file: shadow stays allowed, nothing above it is.
"""
from __future__ import annotations

import pytest

from hftlab.calendar import Calendar
from hftlab.journal import Journal, read
from hftlab.trader import Rollout, Rule, Stages, TraderRefused
from hftlab.trader import __main__ as cli
from hftlab.trader.config import load
from hftlab.trader.stages import ACCOUNT_CANNOT_OPEN, DAY_NS
from hftlab.venues import capability
from tests.support import ROOT
from tests.trader.kit import SECOND, Desk, put_stage
from tests.trader.test_command import FILES, run
from tests.trader.test_stages import HOUR, MONDAY, a_day

SHIPPED = load(ROOT / "config" / "trader.yaml")
VENUE_FILE = ROOT / capability.path_for(SHIPPED.venue)
BLOCKED = capability.cannot_open(capability.load(VENUE_FILE), SHIPPED.pairs)
EARNED = Rollout("shadow", Rule(1, "HFT-DEC-9998"), Rule(1, "HFT-DEC-9999"))  # a rule that IS met after one day
YEN = {"USD.JPY": "buy refused (currency_not_permitted), sell not tested, by HFT-EXP-0008"}


def test_with_the_shipped_files_every_sleeve_stays_in_shadow_and_the_refusal_names_the_reason(tmp_path):
    """Give every shipped sleeve a rule that is met, limits in force and clean days: the account still holds it."""
    assert BLOCKED == {"EUR.USD": "buy refused (insufficient_cash_quote_currency), sell not tested, by HFT-EXP-0008"}
    ladders = dict.fromkeys(SHIPPED.names, EARNED)
    assert len(ladders) == 7
    found = Stages(
        Journal(tmp_path / "stages.jsonl"), ladders, calendar=Calendar(), in_force=True, min_day_ns=20 * HOUR,
        cannot_open=capability.said(BLOCKED),
    )  # fmt: skip
    for day in range(3):
        assert a_day(found, MONDAY + day * DAY_NS) == []
    assert set(found.all().values()) == {"shadow"} and found.above_shadow() == []
    held = [entry for entry in read(tmp_path / "stages.jsonl")[1] if entry["type"] == "held"]
    assert {entry["sleeve"] for entry in held} == set(SHIPPED.names) and len(held) == 3 * len(ladders)
    for entry in held:
        assert (entry["to"], entry["cause"]) == ("min_size", ACCOUNT_CANNOT_OPEN)
        assert entry["detail"] == f"EUR.USD: {BLOCKED['EUR.USD']}"
    again = Stages(Journal(tmp_path / "stages.jsonl"), ladders, calendar=Calendar(), in_force=True, min_day_ns=None)
    assert set(again.all().values()) == {"shadow"}, "nothing in the journal is a promotion"


def test_the_same_days_promote_when_the_account_can_open_the_positions(tmp_path):
    found = Stages(
        Journal(tmp_path / "stages.jsonl"), {"control": EARNED}, calendar=Calendar(), in_force=True,
        min_day_ns=20 * HOUR,
    )  # fmt: skip
    (change,) = a_day(found, MONDAY)
    assert (change.to, change.cause) == ("min_size", "PROMOTED"), "the hold above is the account's and nothing else's"


def test_the_account_is_named_before_the_limits_when_both_hold_a_step(tmp_path):
    found = Stages(
        Journal(tmp_path / "stages.jsonl"), {"control": EARNED}, calendar=Calendar(), in_force=False,
        min_day_ns=20 * HOUR, cannot_open="EUR.USD: buy refused",
    )  # fmt: skip
    a_day(found, MONDAY)
    (held,) = [entry for entry in read(tmp_path / "stages.jsonl")[1] if entry["type"] == "held"]
    assert held["cause"] == ACCOUNT_CANNOT_OPEN


@pytest.mark.parametrize("stage", ["min_size", "normal", "stepped_down"])
def test_a_trader_refuses_to_start_with_a_sleeve_above_shadow_and_says_which_pair_and_why(tmp_path, stage):
    put_stage(tmp_path, "control", stage)
    desk = Desk(tmp_path, start=False)  # the limits ARE in force: only the account stands in the way
    desk.cannot_open = dict(YEN)
    with pytest.raises(TraderRefused, match=r"control may place paper orders .* capability file") as refused:
        desk.start()
    said = str(refused.value)
    assert "USD.JPY: buy refused (currency_not_permitted), sell not tested, by HFT-EXP-0008" in said
    assert "Only shadow is allowed" in said and desk.market._orders == {}


def test_shadow_stays_allowed_and_sends_the_broker_nothing(tmp_path):
    desk = Desk(tmp_path, start=False)
    desk.cannot_open = dict(YEN)
    trader = desk.start()
    desk.quotes(20)
    desk.until = desk.clock.now_ns() + 20 * SECOND
    trader.run()
    summary = trader.close()
    assert trader.stages.all() == {"control": "shadow", "idle": "shadow"} and desk.market._orders == {}
    control = next(one for one in summary["sleeves"] if one["name"] == "control")
    assert control["stage"] == "shadow" and control["decisions"] > 0, "it decided, and only the shadow book heard"


def test_a_trader_given_no_capability_starts_above_shadow_as_before(tmp_path):
    put_stage(tmp_path, "control", "min_size")
    assert Desk(tmp_path).trader.stages.above_shadow() == ["control"]


# ---- the command reads the file -------------------------------------------------------------------------------------


def test_the_command_says_that_shadow_is_the_highest_stage_and_every_sleeve_is_in_it(tmp_path, capsys):
    assert run(tmp_path, "--dry-run-seconds", "5") == 0
    said = capsys.readouterr().err
    assert "SHADOW IS THE HIGHEST STAGE" in said and "EUR.USD: buy refused (insufficient_cash_quote_currency)" in said
    assert run(tmp_path, "--stages") == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(SHIPPED.names) and all(": shadow;" in line for line in lines)


def test_the_command_refuses_a_sleeve_the_stage_journal_puts_above_shadow(tmp_path, capsys):
    put_stage(tmp_path / "trader", "control", "min_size")
    assert run(tmp_path, "--dry-run-seconds", "5") == 2
    assert "refused: control may place paper orders" in capsys.readouterr().err


def test_the_command_refuses_to_run_without_a_capability_file_and_counts_it_among_the_settings(tmp_path, capsys):
    assert run(tmp_path, "--dry-run-seconds", "5", "--capability", str(tmp_path / "none.yaml")) == 2
    assert "cannot read the capability file" in capsys.readouterr().err
    args = cli.argparse.Namespace(
        config=ROOT / "config" / "trader.yaml", risk=ROOT / "config" / "risk.yaml",
        cost_model=ROOT / "config" / "cost_model.yaml", calendar=ROOT / "config" / "calendar", capability=VENUE_FILE,
    )  # fmt: skip
    before = cli.config_digest(args)
    copy = tmp_path / "ibkr.yaml"
    copy.write_text(VENUE_FILE.read_text().replace("buy: refused", "buy: accepted"))
    args.capability = copy
    assert cli.config_digest(args) != before, "a replay under another capability file is refused by the digest"
    assert FILES[0] == "--config"
