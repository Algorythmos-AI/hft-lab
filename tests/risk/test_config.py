"""The risk file: nothing unknown gets in, nothing null is in force, and the shipped file is a placeholder."""
from __future__ import annotations

import re

import pytest
import yaml

from hftlab.risk import RiskConfigError, RiskNotInForce, load_risk_config, parse_risk_config
from tests.risk.kit import MINUTE, MS, SECOND, limits, raw
from tests.support import PLACEHOLDER, ROOT

SHIPPED = PLACEHOLDER / "risk.yaml"  # the limits before any decision: every one null, none in force
IN_FORCE = ROOT / "config" / "risk.yaml"


def complaint(changes: dict[str, object]) -> str:
    with pytest.raises(RiskConfigError) as caught:
        parse_risk_config(raw(changes))
    assert not isinstance(caught.value, RiskNotInForce)
    return str(caught.value)


# ---- the file that is shipped ----------------------------------------------------------------------------------------


def test_the_shipped_limits_are_in_force_by_a_decision_record_that_exists():
    shipped = load_risk_config(IN_FORCE)
    assert shipped.decision == "HFT-DEC-0001"
    assert (ROOT / "research" / "decisions" / "HFT-DEC-0001-paper-risk-limits-and-float.md").is_file()
    text = IN_FORCE.read_text()
    assert "PLACEHOLDER" not in text and text.count("NOT ENFORCED") == 2 and "PROPOSAL" in text
    assert set(shipped.pairs) == {"EUR.USD", "AUD.USD"} and set(shipped.sleeves) == {"control"}
    assert shipped.desk.max_open_pairs == len(shipped.pairs)


def test_the_shipped_float_covers_what_the_limits_let_the_desk_hold_and_keeps_its_reserve():
    """Two minimum sizes a pair, long in every pair at once or short in every pair at once, without a refusal."""
    shipped = load_risk_config(IN_FORCE)
    inventory, unit = shipped.inventory, 1_000_000
    assert inventory is not None and inventory.hub == "USD" and inventory.shadow_only == {"USD.JPY", "GBP.USD"}
    assert set(inventory.float_micro) == {"USD", "EUR", "AUD"} == set(inventory.reserve_micro)
    assert inventory.minimum_entry == {"EUR.USD": 20_000, "AUD.USD": 25_000}
    dearest = {"EUR.USD": 1.30, "AUD.USD": 0.80}  # rates well above any of the last ten years' closes
    longs = 0.0
    for pair, cap in shipped.pairs.items():
        least = inventory.minimum_entry[pair]
        assert cap.max_position == cap.max_order_size == 2 * least, pair
        spendable = inventory.float_micro[pair[:3]] - inventory.reserve_micro[pair[:3]]
        assert spendable >= cap.max_position * unit, f"{pair}: the float of {pair[:3]} pays for the largest short"
        assert cap.max_spread_ticks < cap.price_collar_ticks <= cap.exit_collar_ticks
        longs += cap.max_position * dearest[pair]
    assert (inventory.float_micro["USD"] - inventory.reserve_micro["USD"]) / unit >= longs, "and of USD for the longs"
    control = shipped.sleeves["control"]
    assert control.max_position == max(inventory.minimum_entry.values()), "one minimum size in any pair"
    for currency in ("USD", "AUD"):  # the quote currency of the pairs, and the one fees are expected in
        assert control.loss_latches[currency].daily_loss_micro < shipped.desk.loss_latches[currency].daily_loss_micro
    assert control.max_orders_per_minute < shipped.desk.max_orders_per_minute


# ---- the limits as they were before any decision: every one null -------------------------------------------------


def test_the_placeholder_file_is_well_formed_and_not_in_force_so_nothing_can_be_built_from_it():
    with pytest.raises(RiskNotInForce, match="not in force"):
        load_risk_config(SHIPPED)


def test_every_limit_in_the_placeholder_file_is_null_and_says_it_is_a_placeholder():
    text = SHIPPED.read_text()
    shipped = yaml.safe_load(text)
    assert shipped["status"] == "placeholder" and shipped["in_force"] is False and shipped["decision"] is None
    valued = [line for line in text.splitlines() if re.match(r"\s+[a-z_]+: +[^#\s]", line)]
    set_limits = [line for line in valued if not re.match(r"\s+(kill|flatten):", line)]
    assert len(set_limits) == 24, "the scan sees every limit: 22 placeholders and the two that are not enforced"
    for line in set_limits:
        assert re.match(r"\s+[a-z_]+: null\s+# PLACEHOLDER\b", line), line
    assert sum("NOT ENFORCED" in line for line in set_limits) == 2


def test_put_in_force_as_it_is_the_placeholder_file_is_refused_for_every_null_limit_by_name():
    shipped = yaml.safe_load(SHIPPED.read_text())
    shipped.update(in_force=True, status="decided", decision="HFT-DEC-0001")
    with pytest.raises(RiskConfigError) as caught:
        parse_risk_config(shipped)
    said = str(caught.value)
    for name in (
        "desk.max_open_pairs", "desk.max_orders_per_minute", "desk.max_order_to_trade_ratio",
        "desk.order_to_trade_window_seconds", "desk.reject_streak_breaker", "desk.loss_latches.USD.daily_loss",
        "desk.loss_latches.USD.drawdown", "per_pair.EUR.USD.max_position", "per_pair.EUR.USD.max_order_size",
        "per_pair.EUR.USD.price_collar_ticks", "per_pair.EUR.USD.max_spread_ticks", "per_pair.EUR.USD.stale_quote_ms",
        "per_pair.EUR.USD.exit_collar_ticks", "switches.flatten_retry_seconds",
        "per_sleeve.control.max_position", "per_sleeve.control.max_orders_per_minute",
        "per_sleeve.control.loss_latches.USD.daily_loss", "market_guards.rollover_no_entry_minutes_before",
        "market_guards.rollover_no_entry_minutes_after", "market_guards.weekly_close_no_entry_minutes",
        "reconciliation.grace_seconds",
    ):  # fmt: skip
        assert f"{name} is null: a limit in force needs a value" in said, name
    assert "max_gross_exposure is null" not in said and "max_leverage is null" not in said


# ---- what a file in force must be ------------------------------------------------------------------------------------


def test_limits_in_force_load_as_exact_integers_in_the_units_the_gate_works_in():
    config = limits()
    assert config.decision == "HFT-DEC-9999"
    pair, sleeve, desk = config.pairs["EUR.USD"], config.sleeves["control"], config.desk
    assert (pair.max_position, pair.max_order_size) == (100_000, 60_000)
    assert (pair.price_collar_ticks, pair.max_spread_ticks, pair.stale_quote_ns) == (10, 4, 2_000 * MS)
    assert (sleeve.max_position, sleeve.max_orders_per_minute) == (60_000, 20)
    assert (desk.max_open_pairs, desk.max_orders_per_minute, desk.max_order_to_trade_ratio) == (2, 30, 10)
    assert desk.order_to_trade_window_ns == 300 * SECOND and desk.reject_streak_breaker == 3
    usd, jpy = desk.loss_latches["USD"], desk.loss_latches["JPY"]
    assert (usd.daily_loss_micro, usd.drawdown_micro) == (100_000_000, 150_000_000), "millionths of a dollar"
    assert (jpy.daily_loss_micro, jpy.drawdown_micro) == (15_000_000_000, 20_000_000_000), "millionths of a yen"
    guards = config.guards
    assert (guards.rollover_before_ns, guards.rollover_after_ns, guards.weekly_close_no_entry_ns) == (
        10 * MINUTE, 20 * MINUTE, 30 * MINUTE,
    )  # fmt: skip
    assert config.reconciliation_grace_ns == 5 * SECOND
    assert (config.kill_switch, config.flatten_switch) == ("KILL", "FLATTEN")
    assert all(type(value) is int for value in (pair.stale_quote_ns, usd.daily_loss_micro, guards.rollover_before_ns))


@pytest.mark.parametrize(
    ("path", "named"),
    [
        ("max_slippage", "risk.max_slippage"),
        ("desk/max_notional", "desk.max_notional"),
        ("desk/loss_latches/USD/weekly_loss", "desk.loss_latches.USD.weekly_loss"),
        ("per_pair/EUR.USD/max_positon", "per_pair.EUR.USD.max_positon"),
        ("per_sleeve/control/max_drawdown", "per_sleeve.control.max_drawdown"),
        ("per_sleeve/control/loss_latches/USD/dayly_loss", "per_sleeve.control.loss_latches.USD.dayly_loss"),
        ("market_guards/friday_flat_minutes_before_close", "market_guards.friday_flat_minutes_before_close"),
        ("reconciliation/grace_ms", "reconciliation.grace_ms"),
        ("switches/pause", "switches.pause"),
    ],
)
def test_a_key_the_file_does_not_define_is_refused_by_its_full_name_at_every_level(path, named):
    assert f"{named} is not a key this file defines" in complaint({path: 1})


@pytest.mark.parametrize(
    "path",
    ["decision", "desk", "desk/max_open_pairs", "per_pair/EUR.USD/stale_quote_ms", "per_sleeve/control/loss_latches",
     "desk/loss_latches/USD/drawdown", "market_guards/weekly_close_no_entry_minutes", "reconciliation",
     "switches/kill"],
)  # fmt: skip
def test_a_key_that_is_missing_is_refused_too(path):
    said = complaint({path: ...})
    assert "is missing" in said or "must be" in said


@pytest.mark.parametrize("wrong", [1.5, "20000", True, -1, 0, [20_000]])
def test_a_whole_number_is_a_whole_number_never_a_float_a_string_or_a_bool(wrong):
    assert "per_pair.EUR.USD.max_position must be a whole number of at least 1" in complaint(
        {"per_pair/EUR.USD/max_position": wrong}
    )
    assert "desk.max_orders_per_minute must be a whole number" in complaint({"desk/max_orders_per_minute": wrong})


@pytest.mark.parametrize("wrong", [100, 100.0, "0", "-5.00", "1e2", "abc", "0.0000001", True])
def test_money_is_quoted_decimal_text_and_positive(wrong):
    assert "desk.loss_latches.USD.daily_loss must be a quoted positive decimal" in complaint(
        {"desk/loss_latches/USD/daily_loss": wrong}
    )


def test_a_null_limit_is_refused_while_in_force_and_only_reported_while_not():
    assert "per_pair.EUR.USD.max_order_size is null: a limit in force needs a value" in complaint(
        {"per_pair/EUR.USD/max_order_size": None}
    )
    with pytest.raises(RiskNotInForce, match="1 of them are still null"):
        parse_risk_config(raw({"per_pair/EUR.USD/max_order_size": None, "in_force": False}))


def test_a_file_that_is_not_in_force_still_has_to_be_well_formed():
    said = complaint({"in_force": False, "desk/max_notional": 5})
    assert "desk.max_notional is not a key this file defines" in said


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "placeholder"},
        {"decision": None},
        {"decision": "DEC-1"},
        {"decision": "HFT-EXP-0001"},
        {"status": "final"},
    ],
)
def test_limits_in_force_name_the_decision_record_that_set_them(changes):
    said = complaint(changes)
    assert "decision" in said or "status" in said


@pytest.mark.parametrize("in_force", ["yes", 1, None, "true"])
def test_in_force_is_true_or_false_and_nothing_else(in_force):
    assert "risk.in_force must be true or false" in complaint({"in_force": in_force})


@pytest.mark.parametrize("key", ["max_gross_exposure", "max_leverage"])
def test_a_limit_the_gate_cannot_enforce_must_stay_null(key):
    assert f"desk.{key} is not enforced yet" in complaint({f"desk/{key}": 2})


def test_every_scope_sets_a_loss_latch_in_the_quote_currency_of_every_pair():
    assert "desk.loss_latches sets no latch in JPY" in complaint({"desk/loss_latches/JPY": ...})
    assert "per_sleeve.ml.loss_latches sets no latch in USD" in complaint({"per_sleeve/ml/loss_latches/USD": ...})
    only_euro = raw({"per_pair/USD.JPY": ..., "desk/loss_latches/JPY": ...})
    assert parse_risk_config(only_euro).desk.loss_latches.keys() == {"USD"}, "no yen pair, no yen latch needed"


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"per_pair/EURUSD": {}}, "per_pair.EURUSD is not a currency pair"),
        ({"per_sleeve/Control": {}}, "per_sleeve.Control is not a sleeve name"),
        ({"desk/loss_latches/dollars": {}}, "desk.loss_latches.dollars is not a three-letter currency code"),
        ({"per_pair": {}}, "per_pair must configure at least one pair"),
        ({"per_pair": [1]}, "per_pair must be a mapping of names"),
        ({"switches/kill": "FLATTEN"}, "must be two different files"),
        ({"switches/kill": "state/KILL"}, "switches.kill must be a plain file name"),
        ({"switches/flatten": None}, "switches.flatten must be a plain file name"),
    ],
)
def test_names_are_checked_as_well_as_values(changes, said):
    assert said in complaint(changes)


def test_every_complaint_about_a_file_is_made_at_once():
    said = complaint({"desk/max_open_pairs": 1.5, "per_pair/EUR.USD/max_position": None, "extra": 1})
    assert said.count(";") == 2 and said.startswith("the risk file: ")


def test_a_file_that_cannot_be_read_or_is_not_a_mapping_is_refused(tmp_path):
    with pytest.raises(RiskConfigError, match="cannot read"):
        load_risk_config(tmp_path / "none.yaml")
    (tmp_path / "bad.yaml").write_text("desk: [unclosed")
    with pytest.raises(RiskConfigError, match="cannot read"):
        load_risk_config(tmp_path / "bad.yaml")
    (tmp_path / "list.yaml").write_text("- 1\n")
    with pytest.raises(RiskConfigError, match="risk must be a mapping"):
        load_risk_config(tmp_path / "list.yaml")
    (tmp_path / "good.yaml").write_text(yaml.safe_dump(raw()))
    assert load_risk_config(tmp_path / "good.yaml") == limits()
