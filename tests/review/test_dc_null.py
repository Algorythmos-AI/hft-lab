"""A Directional Change sleeve's day beside its null: answers worked out by hand, and the same bytes every time.

The sleeve under test is the trend variant on a threshold of 52.5 price units of a mid near 108,000, at 20,000
EUR.USD, on the test cost model: a round trip costs one spread of 10 units and two commissions of 2.00 USD.
"""
from __future__ import annotations

import itertools
import json
from fractions import Fraction

import pytest
import yaml

from hftlab.calendar import Calendar
from hftlab.record.format import Heartbeat
from hftlab.review import dc_null
from hftlab.review.dc_null import Day, report, walks_of
from hftlab.review.trips import round_trips
from hftlab.shadow.__main__ import ConfigError
from hftlab.strategy.dc import null
from tests.review.kit import FEE, UNIT, recording
from tests.shadow.kit import TRADED, session, walk
from tests.strategy.dc_kit import HIGH, LOW, quotes, zigzag
from tests.support import PLACEHOLDER, ROOT
from tests.venues.kit import EURUSD, MS, plain_costs

WALKS = 19
TREND = {
    "name": "dc-trend", "kind": "directional_change", "price": "mid", "variant": "trend",
    "threshold_percent": "0.0484", "overshoot_multiple": None, "max_hold_seconds": 3_600, "min_spread_ratio": "5",
    "warm_up_events": 2, "gap_seconds": 5, "retry_seconds": 1, "max_attempts": 3,
}  # fmt: skip
CONTRA = {**TREND, "name": "dc-contra", "variant": "contrarian", "overshoot_multiple": "1"}


def config(*sleeves, measure: bool = True) -> dict:
    """A shadow runner's configuration as a loaded file, with the null's numbers fixed in it."""
    loaded = {"journal_dir": "unused", "seed": 1, "heartbeat_ms": 1_000, "pairs": (),
              "sleeves": [{"name": "idle", "kind": "idle"}, *(sleeves or (TREND,))]}  # fmt: skip
    if measure:
        loaded["measure"] = {"null_walks": {"count": WALKS, "seed": 1, "per": "recorded_day"}}
    return loaded


def reported(tmp_path, stream, *sleeves, walks: int = WALKS, seed: int = 1, names=()) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    day = Day(stream, TRADED, "EUR.USD")
    return report(
        config(*sleeves), day, model=plain_costs(), calendar=Calendar(), walks=walks, seed=seed, names=names,
        scratch=tmp_path,
    )  # fmt: skip


# ---- by hand ---------------------------------------------------------------------------------------------------------


def test_on_a_day_that_never_moves_every_walk_is_the_day_and_the_sleeve_is_level_with_all_of_them(tmp_path):
    found = reported(tmp_path, quotes([108_000] * 300))
    assert found["ticks"] == 300 and found["mean_square_x4"] == [0, 1]
    assert (found["step_x2"], found["share_of_ticks_that_move"]) == (10, [0, 1]), "a walk that never takes its step"
    (sleeve,) = found["sleeves"]
    assert sleeve["observed"] == {"result_micro": 0, "round_trips": 0, "irregular": 0, "open_quantity": 0}
    assert sleeve["null"] == {"results_micro": [0] * WALKS, "round_trips": [0] * WALKS}
    assert sleeve["walks_at_or_above"] == WALKS and sleeve["at_or_above"] == [1, 1], "every walk did as well: 20 in 20"


def test_on_a_planted_zig_zag_the_trend_sleeve_earns_what_the_pattern_is_worth_and_no_walk_does_as_well(tmp_path):
    stream = quotes(zigzag(12))
    found = reported(tmp_path, stream)
    # The mid moves 5 units on every tick: twice the mid moves 10, so a walk steps one increment on every tick.
    assert found["mean_square_x4"] == [100, 1]
    assert (found["step_x2"], found["share_of_ticks_that_move"]) == (10, [1, 1])
    (sleeve,) = found["sleeves"]
    # A leg runs 600 units from one extreme to the other. An event confirms 55 units into it (the first tick
    # at least 52.5 from the extreme), and there the sleeve leaves the position of the leg before. The first
    # position is taken on such a tick, the sleeve being flat: it rides 600 - 55 - 55 = 490 units of the mid.
    # Every later one is taken a tick after, once the exit has filled: 5 units further on, so it rides 485.
    # Each pays one spread of 10 units and two commissions.
    first = (HIGH - LOW - 2 * 55 - 10) * UNIT - 2 * FEE
    later = first - 5 * UNIT
    assert (first, later) == (92_000_000, 91_000_000)
    trips = sleeve["observed"]["round_trips"]
    assert trips == 8 and sleeve["observed"]["result_micro"] == first + 7 * later == 729_000_000
    assert sleeve["observed"]["irregular"] == 0 and abs(sleeve["observed"]["open_quantity"]) == 20_000, "the last leg's"
    assert len(sleeve["null"]["results_micro"]) == WALKS and max(sleeve["null"]["results_micro"]) < 729_000_000
    assert sleeve["walks_at_or_above"] == 0 and sleeve["at_or_above"] == [1, WALKS + 1], "one in twenty, never zero"
    assert sleeve["settings"]["thresholds"] == [[121, 250_000]] and sleeve["settings"]["variant"] == "trend"


def test_a_day_whose_only_trade_is_still_open_at_its_end_has_a_result_of_nothing_and_says_what_it_holds(tmp_path):
    stream = quotes(zigzag(4))  # two warm-up events, and then one entry that the day ends on
    (sleeve,) = reported(tmp_path, stream)["sleeves"]
    assert sleeve["observed"] == {"result_micro": 0, "round_trips": 0, "irregular": 0, "open_quantity": -20_000}


# ---- against the pieces it is made of --------------------------------------------------------------------------------


def a_day(count: int = 1_500) -> list:
    """A walking book with a heartbeat after every fiftieth quote: a day that is more than its quotes."""
    out = []
    for n, quote in enumerate(walk(3, count)):
        out.append(quote)
        if n % 50 == 49:
            out.append(Heartbeat("EUR.USD", quote.recv_ts_ns + 10 * MS, quote.recv_ts_ns, 0))
    return out


def test_each_null_result_is_the_shadow_runners_on_that_walk_with_all_but_the_quotes_where_it_was(tmp_path):
    stream = a_day()
    day = Day(stream, TRADED, "EUR.USD")
    assert day.with_quotes(day.quotes) == stream and len(day.quotes) == 1_500
    (sleeve,) = reported(tmp_path / "tool", stream, walks=5)["sleeves"]
    numbers = itertools.count()

    def by_hand(walked) -> int:
        replaced = iter(walked)
        merged = [next(replaced) if isinstance(event, type(walked[0])) else event for event in stream]
        assert [type(event) for event in merged] == [type(event) for event in stream], "heartbeats in their places"
        (one,) = dc_null.make_sleeves([TREND], {"EUR.USD": 20_000}, costs=plain_costs(), calendar=Calendar())
        _, entries, _ = session(tmp_path / f"{next(numbers)}.jsonl", [one], merged, seed=1, calendar=Calendar())
        found = round_trips(entries, "dc-trend", {"EUR.USD": EURUSD}).get("EUR.USD")
        return sum(trip.result_micro(EURUSD) for trip in found.trips) if found else 0

    expected = null.results(by_hand, day.quotes, EURUSD, walks=5, seed=1)
    assert sleeve["null"]["results_micro"] == list(expected) and len(set(expected)) > 1, "five different walks"
    observed = by_hand(day.quotes)
    assert sleeve["observed"]["result_micro"] == observed
    as_good = sum(result >= observed for result in expected)
    assert sleeve["walks_at_or_above"] == as_good and Fraction(*sleeve["at_or_above"]) == Fraction(1 + as_good, 6)


def test_the_same_day_settings_and_seed_give_the_same_report_and_another_seed_gives_other_walks(tmp_path):
    stream = a_day(800)
    first = reported(tmp_path / "a", stream, TREND, CONTRA, walks=4)
    again = reported(tmp_path / "b", stream, TREND, CONTRA, walks=4)
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert [sleeve["name"] for sleeve in first["sleeves"]] == ["dc-trend", "dc-contra"], "each alone, in turn"
    other = reported(tmp_path / "c", stream, TREND, CONTRA, walks=4, seed=2)
    assert other["sleeves"][0]["observed"] == first["sleeves"][0]["observed"], "the day is the day"
    assert other["sleeves"][0]["null"] != first["sleeves"][0]["null"]
    only = reported(tmp_path / "d", stream, TREND, CONTRA, walks=4, names=["dc-contra"])
    assert only["sleeves"] == first["sleeves"][1:], "a sleeve's row does not depend on which others were asked for"
    assert list(tmp_path.glob("*/*walk*")) == [], "the journals of the walks are not kept"


def test_the_number_of_walks_and_the_seed_are_the_configurations_unless_both_are_given():
    assert walks_of(config(), None, None) == (WALKS, 1, "measure.null_walks of the configuration")
    assert walks_of(config(), 5, 9) == (5, 9, "the command line") == walks_of(config(measure=False), 5, 9)
    for walks, seed, loaded in ((5, None, config()), (None, 9, config()), (None, None, config(measure=False))):
        with pytest.raises(ConfigError, match="both --walks and --seed"):
            walks_of(loaded, walks, seed)
    with pytest.raises(ConfigError, match="whole count"):
        walks_of({"measure": {"null_walks": {"count": "99", "seed": 1}}}, None, None)


def test_what_cannot_be_run_is_refused(tmp_path):
    stream = quotes(zigzag(2))
    with pytest.raises(ConfigError, match="does not describe"):
        Day(stream, TRADED, "GBP.USD")
    with pytest.raises(ConfigError, match="fewer than two quotes"):
        Day(stream[:1], TRADED, "EUR.USD")
    with pytest.raises(ConfigError, match="one quote for each quote"):
        Day(stream, TRADED, "EUR.USD").with_quotes(stream[:-1])
    with pytest.raises(ConfigError, match="has the Directional Change sleeves"):
        reported(tmp_path, stream, names=["no-such-sleeve"])
    with pytest.raises(null.NullError, match="at least one"):
        reported(tmp_path, stream, walks=0)


# ---- the command -----------------------------------------------------------------------------------------------------


def test_the_command_reads_the_registered_configuration_and_a_recorded_day_and_prints_the_same_bytes_twice(
    tmp_path, capsys
):
    (day,) = recording(tmp_path / "rec", quotes(zigzag(4)))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**config(TREND, CONTRA), "pairs": []}))
    argv = ["--config", str(path), "--day", str(day), "--cost-model", str(PLACEHOLDER / "cost_model.yaml"),
            "--calendar", str(ROOT / "config" / "calendar")]  # fmt: skip
    assert dc_null.main(argv) == 0
    out, err = capsys.readouterr()
    said = json.loads(out)
    assert (said["day"], said["walks"], said["seed"]) == (day.name, WALKS, 1) and "PLACEHOLDER" in err
    assert said["walks_and_seed_from"] == "measure.null_walks of the configuration"
    assert [sleeve["name"] for sleeve in said["sleeves"]] == ["dc-trend", "dc-contra"]
    assert all(len(sleeve["null"]["results_micro"]) == WALKS for sleeve in said["sleeves"])
    assert dc_null.main(argv) == 0 and capsys.readouterr().out == out
    assert dc_null.main([*argv, "--walks", "3", "--seed", "5", "--sleeve", "dc-trend"]) == 0
    fewer = json.loads(capsys.readouterr().out)
    assert (fewer["walks"], fewer["seed"], fewer["walks_and_seed_from"]) == (3, 5, "the command line")
    assert len(fewer["sleeves"]) == 1 and fewer["sleeves"][0]["observed"] == said["sleeves"][0]["observed"]
    for wrong in (["--walks", "3"], ["--sleeve", "other"], ["--pair", "GBP.USD"], ["--day", str(tmp_path / "none")]):
        assert dc_null.main([*argv, *wrong]) == 2, wrong
    assert "dc_null:" in capsys.readouterr().err


def test_the_registered_experiments_own_configuration_is_one_the_command_can_read(tmp_path, capsys):
    """HFT-EXP-0005 as it stands on main: six sleeves, 99 walks, seed 1. Run here on a synthetic day, two sleeves."""
    registered = ROOT / "research" / "experiments" / "HFT-EXP-0005-directional-change-shadow" / "config.yaml"
    (day,) = recording(tmp_path / "rec", quotes(zigzag(2)))
    argv = ["--config", str(registered), "--day", str(day), "--cost-model", str(PLACEHOLDER / "cost_model.yaml"),
            "--calendar", str(ROOT / "config" / "calendar"), "--sleeve", "dc-trend-010", "dc-contra-020"]  # fmt: skip
    assert dc_null.main(argv) == 0
    said = json.loads(capsys.readouterr().out)
    assert (said["walks"], said["seed"], said["pair"]) == (99, 1, "EUR.USD")
    assert [sleeve["name"] for sleeve in said["sleeves"]] == ["dc-trend-010", "dc-contra-020"]
    assert all(len(sleeve["null"]["results_micro"]) == 99 for sleeve in said["sleeves"])
