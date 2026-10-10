"""The capability probe, run against the fake broker: read-only by default, and it never leaves an order behind."""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import InstrumentKind, OrderState, SimClock
from hftlab.safety import PaperOnlyError
from hftlab.venues.fake import FakeConfig, build
from hftlab.venues.ibkr import probe
from hftlab.venues.ibkr.probe import Probe, ProbeSettings
from hftlab.venues.port import ConnectionState, VenueError
from tests.venues.kit import EURUSD, INSTRUMENTS, MS, SECOND, T0, USDJPY

FAST = ProbeSettings(window_ns=2 * SECOND, depth_window_ns=SECOND, history_ticks=50, order_wait_ns=2 * SECOND)


def a_venue(config: FakeConfig | None = None, *, quotes: bool = True, **options):
    clock = SimClock(T0)
    config = config or FakeConfig(depth_levels=5)
    broker = build(clock, INSTRUMENTS, config, seed=3, auto_advance=True, **options)
    if quotes:
        for pair, mid in (("EUR.USD", 108_505), ("USD.JPY", 151_235)):
            walk = {"start_ns": T0 + MS, "count": 4_000, "interval_ns": 5 * MS, "mid": mid, "spread": 10, "step": 5}
            broker.market.generate_quotes(pair, **walk)
    return clock, broker


def run(settings: ProbeSettings = FAST, **options) -> tuple[dict, object]:
    clock, broker = a_venue(**options)
    return Probe(broker, clock, settings).run(), broker


def floats_in(value) -> list:
    if isinstance(value, float):
        return [value]
    if isinstance(value, dict):
        return [f for item in value.values() for f in floats_in(item)]
    if isinstance(value, list | tuple):
        return [f for item in value for f in floats_in(item)]
    return []


# ---- read-only by default -------------------------------------------------------------------------------------------


def test_by_default_the_probe_places_no_order_and_disconnects_when_done():
    report, broker = run()
    assert report["orders"] is None and broker.market._orders == {}
    assert broker.state is ConnectionState.DISCONNECTED
    assert report["schema"] == 1 and report["venue"] == "fake" and report["probed_at"] == "2025-10-09T08:53:20Z"


def test_accounts_are_masked_to_their_prefix_and_last_two_characters():
    report, _ = run(config=FakeConfig(accounts=("DU1234567", "DU7654321")))
    assert report["accounts"] == ["DU*****67", "DU*****21"]
    assert "1234567" not in json.dumps(report) and "7654321" not in json.dumps(report)


@pytest.mark.parametrize(
    ("account", "masked"),
    [("DU1234567", "DU*****67"), ("U1234567", "U*****67"), ("DF12", "DF**"), ("DU1", "DU*"), ("123456", "****56"),
     ("", ""), ("DU", "DU"), ("DUX9988776", "DUX*****76")],
)  # fmt: skip
def test_masking(account, masked):
    assert probe.mask(account) == masked


def test_each_pair_is_reported_in_each_form_with_the_venues_own_numbers():
    report, _ = run()
    euro, yen = report["pairs"]["EUR.USD"], report["pairs"]["USD.JPY"]
    assert euro["forms"]["cash"] == {
        "resolves": True, "price_scale": 5, "price_increment": "0.00005", "min_order_size": 20_000,
        "size_increment": 1_000,
    }  # fmt: skip
    assert euro["forms"]["cfd"]["resolves"] is True and euro["forms"]["cfd"]["min_order_size"] == 1_000
    assert yen["forms"]["cash"]["price_increment"] == "0.005" and yen["forms"]["cash"]["price_scale"] == 3
    assert yen["forms"]["cfd"]["resolves"] is False and "USD.JPY" in yen["forms"]["cfd"]["reason"]
    assert euro["sampled_as"] == "cash"


def test_a_pair_the_venue_does_not_have_is_reported_not_fatal():
    report, _ = run(dataclasses.replace(FAST, pairs=("GBP.USD", "EUR.USD")))
    missing = report["pairs"]["GBP.USD"]
    assert not missing["forms"]["cash"]["resolves"] and not missing["forms"]["cfd"]["resolves"]
    assert missing["quotes"] is None and missing["depth"] is None and missing["historical_ticks"] is None
    assert report["pairs"]["EUR.USD"]["quotes"]["updates"] > 0
    assert [p["pair"] for p in report["proposed_config"]["pairs"]] == ["EUR.USD"]


def test_quote_rate_gaps_and_spread_are_measured_over_the_window():
    report, _ = run()
    quotes = report["pairs"]["EUR.USD"]["quotes"]
    assert quotes["window_us"] == 2_000_000 and 300 <= quotes["updates"] <= 400
    assert quotes["updates_per_second"] == probe.per_second(quotes["updates"], 2 * SECOND)
    gaps = quotes["gap_us"]
    assert gaps["count"] == quotes["updates"] - 1 and gaps["min"] == 5_000 and gaps["median"] == 5_000
    assert gaps["max"] % 5_000 == 0, "a longer gap is a run of quotes that did not move the top of the book"
    assert quotes["spread"] == {"count": quotes["updates"], "min": 10, "median": 10, "p99": 10, "max": 10}
    assert quotes["spread_mean_x1000"] == 10_000 and quotes["crossed_or_locked"] == 0
    assert set(quotes["first"]) == {"bid", "ask", "bid_size", "ask_size"} and "." in quotes["first"]["bid"]


def test_a_pair_with_no_quotes_reports_zero_not_an_error():
    report, _ = run(quotes=False)
    quotes = report["pairs"]["EUR.USD"]["quotes"]
    assert quotes["updates"] == 0 and quotes["gap_us"] is None and quotes["spread"] is None
    assert quotes["updates_per_second"] == "0.00" and quotes["first"] is None
    assert report["pairs"]["EUR.USD"]["depth"]["available"] is False
    assert report["pairs"]["EUR.USD"]["historical_ticks"] == {"available": False, "ticks": 0}


def test_depth_and_history_are_reported_as_found():
    report, _ = run()
    assert report["pairs"]["EUR.USD"]["depth"] == {"available": True, "snapshots": 200, "levels": 5, "notices": []}
    assert report["pairs"]["EUR.USD"]["historical_ticks"] == {"available": True, "ticks": 50}
    without, _ = run(config=FakeConfig(depth_levels=0, history=False))
    assert without["pairs"]["EUR.USD"]["depth"]["available"] is False
    assert without["pairs"]["EUR.USD"]["historical_ticks"]["available"] is False


def test_the_server_clock_offset_is_reported():
    report, _ = run(config=FakeConfig(server_clock_offset_ns=-1_500 * MS, depth_levels=5))
    assert report["server_time"] == {"server": "2025-10-09T08:53:18Z", "offset_us": -1_500_000, "round_trip_us": 0}


def test_the_report_has_no_float_anywhere_and_says_what_it_did_not_measure():
    report, _ = run(dataclasses.replace(FAST, orders=True))
    assert floats_in(report) == [] and json.loads(json.dumps(report)) == report
    assert len(report["not_measured"]) == 3 and "commission" in report["not_measured"][0]
    assert report["disconnects_during_probe"] == 0 and report["notices"] == []


def test_a_disconnect_during_the_probe_is_counted():
    from hftlab.venues.supervisor import Backoff

    config = FakeConfig(depth_levels=5, outages=((T0 + 500 * MS, T0 + 700 * MS),))
    report, _ = run(config=config, backoff=Backoff())
    assert report["disconnects_during_probe"] == 1


# ---- the lock -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("accounts", [("U7654321",), ("DU1234567", "U7654321"), ()])
def test_the_probe_refuses_through_the_lock_when_the_account_is_not_paper(accounts):
    clock, broker = a_venue(FakeConfig(accounts=accounts))
    with pytest.raises(PaperOnlyError):
        Probe(broker, clock, dataclasses.replace(FAST, orders=True)).run()
    assert broker.state is ConnectionState.REFUSED and broker.market._orders == {}


# ---- the order round trip -------------------------------------------------------------------------------------------

WITH_ORDERS = dataclasses.replace(FAST, orders=True)


def test_with_orders_one_far_away_minimum_size_order_per_pair_is_placed_timed_and_cancelled():
    report, broker = run(WITH_ORDERS)
    orders = report["orders"]
    assert orders["placed"] is True and orders["left_working"] == [] and orders["positions_changed"] is False
    assert set(orders["pairs"]) == {"EUR.USD", "USD.JPY"}
    for pair, instrument in (("EUR.USD", EURUSD), ("USD.JPY", USDJPY)):
        result = orders["pairs"][pair]
        assert result["quantity"] == instrument.min_order_size
        assert (result["ack_us"], result["cancel_us"]) == (2_000, 2_000), "the fake's configured delays, exactly"
        assert (result["state_after_placing"], result["state_after_cancelling"]) == ("WORKING", "CANCELLED")
        limit, bid = instrument.price(result["limit_price"]), instrument.price(result["bid_at_the_time"])
        assert limit % instrument.price_increment == 0 and bid * 94 // 100 < limit <= bid * 95 // 100 + 5
    booked = list(broker.market._orders.values())
    assert len(booked) == 2 and all(not b.open and b.executed == 0 for b in booked), "cancelled, never filled"
    assert broker.market.executions() == [] and broker.market.positions() == {}


def test_the_round_trip_times_go_into_the_proposed_config():
    report, _ = run(WITH_ORDERS)
    (euro, _yen) = report["proposed_config"]["pairs"]
    assert euro["order_round_trip_ms"] == {"median": 2, "p99": None} and euro["update_interval_ms"] == 5
    assert euro["commission"] == {"rate_bps": None, "minimum": None, "currency": None}


def test_the_probe_places_nothing_beside_orders_that_are_already_working():
    clock, broker = a_venue()
    from tests.venues.kit import an_order

    broker.connect()
    instrument = broker.instrument("EUR.USD", InstrumentKind.CASH)
    broker.mark_reconciled()
    broker.place(an_order(limit=100_000), instrument)
    broker.disconnect()
    report = Probe(broker, clock, WITH_ORDERS).run()
    assert report["orders"]["placed"] is False and "already has 1 working order" in report["orders"]["reason"]
    assert len(broker.market._orders) == 1


def test_an_order_the_broker_rejects_is_reported_and_nothing_is_left():
    report, broker = run(WITH_ORDERS, reject=lambda order: "not permitted on this account")
    result = report["orders"]["pairs"]["EUR.USD"]
    assert result["state_after_placing"] == "REJECTED" and "cancel_us" not in result
    assert report["orders"]["left_working"] == [] and broker.market.working() == []


def test_an_order_that_gets_no_answer_is_still_cancelled_in_the_end():
    """The acknowledgement takes longer than the probe waits: the cleanup must find the order and cancel it."""
    slow = FakeConfig(depth_levels=5, ack_delay_ns=30 * SECOND, fill_delay_ns=60 * SECOND)
    report, broker = run(WITH_ORDERS, config=slow)
    result = report["orders"]["pairs"]["EUR.USD"]
    assert (result["ack_us"], result["state_after_placing"]) == (None, "no answer") and "cancel_us" not in result
    assert report["orders"]["left_working"] == [] and broker.market.working() == []


def test_whatever_goes_wrong_in_between_the_probes_orders_are_cancelled(monkeypatch):
    clock, broker = a_venue()
    probe_run = Probe(broker, clock, WITH_ORDERS)
    real_wait = probe_run._wait_for
    calls = []

    def failing_wait(order_id, wanted):
        calls.append(order_id)
        if OrderState.CANCELLED not in wanted:
            raise RuntimeError("something unexpected right after the order was placed")
        return real_wait(order_id, wanted)

    monkeypatch.setattr(probe_run, "_wait_for", failing_wait)
    with pytest.raises(RuntimeError):
        probe_run.run()
    assert len(calls) == 1 and broker.market.holds(calls[0])
    assert broker.market.working() == [], "the order placed before the failure was cancelled on the way out"
    assert broker.state is ConnectionState.DISCONNECTED


def test_an_order_that_cannot_be_cancelled_is_reported_as_left_working():
    clock, broker = a_venue(FakeConfig(depth_levels=5, cancel_delay_ns=3_600 * SECOND))
    report = Probe(broker, clock, WITH_ORDERS).run()
    left = report["orders"]["left_working"]
    assert len(left) == 2 and all(order_id.startswith("hft-probe-") for order_id in left)
    assert report["orders"]["pairs"]["EUR.USD"]["state_after_cancelling"] == "no answer"


def test_a_position_that_changes_during_the_order_test_is_flagged():
    """The far-away order must not trade. A broker that fills it anyway moves the position, and that is said."""
    clock, broker = a_venue(FakeConfig(depth_levels=5, fill_delay_ns=0))  # it fills the moment it is acknowledged
    real_place = broker.market.submit

    def filling_submit(order, instrument, at_ns):
        marketable = dataclasses.replace(order, limit_price=instrument.price_increment * 10**7)
        return real_place(marketable, instrument, at_ns)

    broker.market.submit = filling_submit
    report = Probe(broker, clock, WITH_ORDERS).run()
    assert report["orders"]["positions_changed"] is True and report["orders"]["positions_before"] == []
    assert [p["quantity"] for p in report["orders"]["positions_after"]] == [20_000, 20_000]
    assert report["orders"]["left_working"] == []


# ---- helpers --------------------------------------------------------------------------------------------------------


@given(st.lists(st.integers(-(10**12), 10**12), min_size=1, max_size=300))
def test_the_summary_is_ordered_and_made_of_values_that_occurred(values):
    found = probe.summary(values)
    assert found["count"] == len(values)
    assert min(values) == found["min"] <= found["median"] <= found["p99"] <= found["max"] == max(values)
    assert {found["median"], found["p99"]} <= set(values)


def test_summary_and_rate_examples():
    assert probe.summary([]) is None
    assert probe.summary([5]) == {"count": 1, "min": 5, "median": 5, "p99": 5, "max": 5}
    assert probe.summary(list(range(1, 101)))["p99"] == 100 and probe.summary(list(range(1, 101)))["median"] == 50
    assert probe.per_second(400, 2 * SECOND) == "200.00" and probe.per_second(1, 3 * SECOND) == "0.33"
    assert probe.per_second(5, 0) == "0.00"


@given(st.integers(100, 10**9), st.sampled_from([EURUSD, USDJPY]))
def test_the_far_price_is_on_the_grid_and_about_five_per_cent_under_the_bid(bid, instrument):
    price = probe.far_below(bid, instrument)
    assert price > 0 and price % instrument.price_increment == 0
    assert price <= bid - bid // 20 and price > bid - bid // 20 - instrument.price_increment


def test_the_smallest_order_respects_the_minimum_and_the_increment():
    assert probe.smallest_order(EURUSD) == 20_000
    odd = dataclasses.replace(EURUSD, min_order_size=20_500)
    assert probe.smallest_order(odd) == 21_000 and odd.refusals(21_000, None) == []


def test_a_clock_that_does_not_move_is_an_error_not_a_hang():
    clock = SimClock(T0)
    broker = build(clock, INSTRUMENTS)  # no auto_advance: time stands still while the probe waits
    with pytest.raises(VenueError, match="clock is not advancing"):
        Probe(broker, clock, FAST).run()
    assert broker.state is ConnectionState.DISCONNECTED


# ---- the command ----------------------------------------------------------------------------------------------------


def test_the_command_runs_a_dry_run_and_writes_the_report(tmp_path, capsys):
    code = probe.main(["--fake", "--window-seconds", "1", "--pairs", "EUR.USD", "--out-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads(captured.out)
    written = tmp_path / "fake-2025-10-09.json"
    assert json.loads(written.read_text()) == report and f"report written to {written}" in captured.err
    assert report["venue"] == "fake" and report["orders"] is None and list(report["pairs"]) == ["EUR.USD"]


def test_the_command_with_orders_against_the_fake(tmp_path, capsys):
    code = probe.main(["--fake", "--window-seconds", "1", "--orders", "--out-dir", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["orders"]["left_working"] == [] and len(report["orders"]["pairs"]) == 2


def test_the_capability_file_is_written_only_when_asked_and_never_the_real_one_from_a_dry_run(tmp_path, capsys):
    real = (probe.CONFIG_PATH).read_text()
    target = tmp_path / "ibkr.yaml"
    base = ["--fake", "--window-seconds", "1", "--out-dir", str(tmp_path)]
    assert probe.main(base) == 0 and not target.exists()
    assert probe.main([*base, "--write-config"]) == 2, "a dry run may not overwrite the real file"
    assert probe.CONFIG_PATH.read_text() == real
    assert probe.main([*base, "--write-config", "--config-path", str(target)]) == 0
    capsys.readouterr()
    proposed = yaml.safe_load(target.read_text())
    assert (proposed["venue"], proposed["status"], proposed["probed_at"]) == ("ibkr_paper", "probed", "2025-10-09")
    assert proposed["access_route"] is None and proposed["instrument_form"] is None, "decisions stay the owner's"
    assert [p["pair"] for p in proposed["pairs"]] == ["EUR.USD", "USD.JPY"]
    assert proposed["pairs"][0]["forms"] == ["cash", "cfd"] and proposed["pairs"][1]["forms"] == ["cash"]
    assert target.read_text().startswith("# Capability file")


@pytest.mark.parametrize("arguments", [["--pairs", "EURUSD"], ["--pairs", ""], ["--window-seconds", "0"]])
def test_the_command_refuses_arguments_that_make_no_sense(arguments, tmp_path, capsys):
    assert probe.main(["--fake", "--out-dir", str(tmp_path), *arguments]) == 2
    assert capsys.readouterr().out == "" and list(tmp_path.iterdir()) == []


def test_without_the_fake_the_command_is_refused_by_the_lock_before_it_dials(tmp_path, monkeypatch, capsys):
    """Against a real gateway the lock comes first. With MODE unset nothing is dialled, so this needs no network."""
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    try:
        assert probe.main(["--out-dir", str(tmp_path)]) == 2
        assert "refused by the paper-only lock: MODE is None" in capsys.readouterr().err
        monkeypatch.setenv("MODE", "paper")
        assert probe.main(["--port", "4001", "--out-dir", str(tmp_path)]) == 2
        assert "live trading port" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []
    finally:
        asyncio.get_event_loop_policy().get_event_loop().close()
        asyncio.set_event_loop(None)

