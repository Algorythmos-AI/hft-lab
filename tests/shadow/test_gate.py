"""The F3 gate, on synthetic data: doing nothing earns exactly nothing, and trading at random costs what the

cost model says trading costs. If either fails, no profit figure from this platform means anything.
"""
from __future__ import annotations

import pytest

from hftlab.strategy import ControlSleeve, IdleSleeve
from tests.shadow.kit import constant, kinds, session, walk
from tests.venues.kit import EURUSD, MS, SECOND, plain_costs

SIZE = 20_000
FEE = 2_000_000  # the per-order minimum of the test cost model, in millionths: 2.00 USD
ROUND_TRIP = 10 * SIZE * 10 + 2 * FEE  # one spread of 10 price units on 20,000, in millionths, and two commissions


def control(seed: int, **changes) -> ControlSleeve:
    settings = {"sizes": {"EUR.USD": SIZE}, "entry_per_million": 50_000, "hold_ns": SECOND, "retry_ns": 10 * SECOND}
    return ControlSleeve("control", seed=seed, **{**settings, **changes})


def test_the_round_trip_cost_used_here_is_the_cost_models_own_figure():
    costs = plain_costs()
    doubled = costs.round_trip_cost_micro(EURUSD, SIZE, (108_500, 108_510), (108_700, 108_710))
    assert doubled == 2 * ROUND_TRIP == 12_000_000, "6.00 USD: 2.00 of spread and two commissions of 2.00"


# ---- doing nothing ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_sleeve_that_never_trades_earns_exactly_zero(tmp_path, seed):
    harsh = {"commission_rate_bps_x100": 20, "latency_min_ns": 20 * MS, "latency_max_ns": 80 * MS}
    costs = plain_costs(slippage_increments=1, **harsh)
    result, entries, shadow = session(tmp_path / "j.jsonl", [IdleSleeve()], walk(seed, 5_000), seed=seed, costs=costs)
    assert result["events"] == 5_000
    assert result["sleeves"]["idle"] == {
        "decisions": 0, "orders": 0, "refused": 0, "fills": 0,
        "pairs": {
            "EUR.USD": {"currency": "USD", "quantity": 0, "realised": 0, "unrealised": 0, "price_scale": 5,
                        "fees_micro": 0, "net_micro": 0, "fees_not_netted_micro": {}},
            "USD.JPY": {"currency": "JPY", "quantity": 0, "realised": 0, "unrealised": 0, "price_scale": 3,
                        "fees_micro": 0, "net_micro": 0, "fees_not_netted_micro": {}},
        },
    }  # fmt: skip
    assert kinds(entries) == ["start", "stop"], "and it left nothing in the journal but the session itself"
    assert shadow.book.sleeves() == [] and shadow.book.fees() == {}
    assert shadow.book.snapshot() == {"lines": {}, "fees": {}, "fills": []}


def test_an_idle_sleeve_beside_a_trading_one_still_earns_exactly_zero(tmp_path):
    result, _, _ = session(tmp_path / "j.jsonl", [control(1), IdleSleeve()], walk(1, 3_000))
    assert result["sleeves"]["control"]["fills"] > 50
    assert all(pair["net_micro"] == 0 for pair in result["sleeves"]["idle"]["pairs"].values())


# ---- trading at random: on a book that does not move, the result IS the cost ----------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_on_a_book_that_does_not_move_the_control_sleeve_loses_exactly_the_modelled_cost(tmp_path, seed):
    result, _, _ = session(tmp_path / "j.jsonl", [control(seed)], constant(4_000), seed=seed)
    line = result["sleeves"]["control"]
    trips, half = divmod(line["fills"], 2)
    assert trips >= 50, "enough round trips for the equality to mean something"
    pair = line["pairs"]["EUR.USD"]
    # A position still open at the end has paid one commission and is valued a whole spread away from its entry.
    assert pair["net_micro"] == -(trips * ROUND_TRIP + half * (ROUND_TRIP - FEE)), "to the millionth"
    assert pair["fees_micro"] == line["fills"] * FEE and line["refused"] == 0
    assert pair["realised"] == -trips * 10 * SIZE and pair["unrealised"] == -half * 10 * SIZE


def test_with_a_rate_instead_of_the_minimum_and_with_slippage_it_is_still_exact(tmp_path):
    costs = plain_costs(commission_rate_bps_x100=2_000, commission_minimum={"USD": 0}, slippage_increments=2)
    result, _, _ = session(tmp_path / "j.jsonl", [control(1)], constant(4_000), costs=costs)
    line = result["sleeves"]["control"]
    assert line["fills"] == 262, "131 round trips, and this seed ends flat"
    trips = line["fills"] // 2
    # Bought at 1.08510 + 2 increments of 5, sold at 1.08500 - 2 increments: 30 price units apart.
    buy_fee = -(-(108_520 * SIZE * 10) * 2_000 // 1_000_000)  # 20 basis points of 21,704 USD, rounded up
    sell_fee = -(-(108_490 * SIZE * 10) * 2_000 // 1_000_000)
    assert (buy_fee, sell_fee) == (43_408_000, 43_396_000)
    one = 30 * SIZE * 10 + buy_fee + sell_fee
    assert line["pairs"]["EUR.USD"]["net_micro"] == -trips * one
    quoted = (108_500, 108_510)
    assert costs.round_trip_cost_micro(EURUSD, SIZE, quoted, quoted) == 2 * one, "the model's own figure, doubled"


# ---- trading at random on a market that moves: the mean is the cost --------------------------------------------------

SEEDS = range(30)
TOLERANCE_PPM = 30_000  # the stated tolerance: the mean over the seeds is within 3% of the modelled cost


def test_over_many_seeds_the_control_sleeves_mean_result_is_the_modelled_cost_within_three_percent(tmp_path):
    excess, trips_total, shares = 0, 0, []
    for seed in SEEDS:
        result, _, _ = session(tmp_path / f"{seed}.jsonl", [control(seed)], walk(1_000 + seed, 6_000), seed=seed)
        line = result["sleeves"]["control"]
        trips, half = divmod(line["fills"], 2)
        # What the walk added to (or took from) the modelled cost in this seed. On a still book it is exactly 0.
        moved = line["pairs"]["EUR.USD"]["net_micro"] + trips * ROUND_TRIP + half * (ROUND_TRIP - FEE)
        excess, trips_total = excess + moved, trips_total + trips
        shares.append(moved * 1_000_000 // (trips * ROUND_TRIP))
    assert trips_total > 5_000
    mean_ppm = excess * 1_000_000 // (trips_total * ROUND_TRIP)
    assert abs(mean_ppm) <= TOLERANCE_PPM, f"the mean result is {mean_ppm} ppm away from the modelled cost"
    assert max(shares) > TOLERANCE_PPM and min(shares) < -TOLERANCE_PPM, "single seeds are further out: luck, both ways"
    assert sum(share > 0 for share in shares) in range(8, 23), "and about as often lucky as unlucky"
