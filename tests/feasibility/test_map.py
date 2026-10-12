"""The feasibility map, on streams whose answer can be worked out by hand: where a move is worth a round trip."""
from __future__ import annotations

import json

import pytest

from hftlab.calendar import Calendar
from hftlab.core import Quote
from hftlab.feasibility import FeasibilityError, Settings, feasibility_map, load_settings, quantile, sample_points
from hftlab.feasibility import __main__ as cli
from hftlab.record.format import Heartbeat, Marker
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.shadow.kit import BIG, constant, walk
from tests.support import PLACEHOLDER, ROOT
from tests.venues.kit import EURUSD, SECOND, T0, USDJPY, plain_costs

MINUTE = 60 * SECOND
ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York, later the same day as T0
TRADED = {"EUR.USD": EURUSD, "USD.JPY": USDJPY}
SIZE = 20_000
# One round trip of 20,000 at a spread of 10 price units, in millionths of a dollar, doubled: half the spread at
# each end is 10 x 20,000 x 10 = 2.00 USD, and two commissions of 2.00 USD. 6.00 USD, so 12,000,000.
COST_X2 = 2 * (10 * SIZE * 10 + 2 * 2_000_000)
JUMP = 35  # price units. On 20,000 that is 7.00 USD: one dollar more than the round trip costs
PAID_X2 = 2 * JUMP * SIZE * 10
AROUND = {"no_fill_before_ns": 5 * MINUTE, "no_fill_after_ns": 15 * MINUTE, "widen_before_ns": 30 * MINUTE,
          "widen_after_ns": 60 * MINUTE, "spread_multiplier_x100": 300}  # fmt: skip


def settings(*horizons_s: int, **changes) -> Settings:
    horizons = tuple(h * SECOND for h in horizons_s or (1, 5, 10, 30, 60, 300))
    rule = {"upper_quantile_ppm": 900_000, "min_windows": 100, "min_share_ppm": 500_000, "placeholder": False}
    return Settings(**{"horizons_ns": horizons, "step_ns": SECOND, **rule, **changes})


def mapped(stream, costs=None, how: Settings | None = None, calendar: Calendar | None = None) -> dict:
    return feasibility_map(stream, TRADED, costs or plain_costs(), calendar or Calendar(), how or settings())


def cells(result: dict, pair: str = "EUR.USD") -> dict[int, dict]:
    return {cell["horizon_ns"] // SECOND: cell for cell in result["pairs"][pair]["cells"]}


def stairs(jump: int = JUMP, *, start_ns: int = T0, steps: int = 80, sizes=(BIG, BIG)) -> list[Quote]:
    """A book that steps up by `jump` price units every thirty seconds and is still in between."""
    return [
        Quote("EUR.USD", 108_500 + jump * k, 108_510 + jump * k, *sizes, start_ns + k * 30 * SECOND, k + 1)
        for k in range(steps + 1)
    ]


# ---- the two cases whose answer is known before anything is computed ------------------------------------------------


def test_a_book_that_does_not_move_has_no_viable_cell_at_any_holding_time():
    result = mapped(constant(12_000))  # twenty minutes of the same book, 100 ms apart: 1,200 grid seconds
    found = cells(result)
    assert {h: cell["windows"] for h, cell in found.items()} == {h: 1_200 - h for h in (1, 5, 10, 30, 60, 300)}
    for cell in found.values():
        assert (cell["median_move_x2_micro"], cell["upper_move_x2_micro"]) == (0, 0)
        assert cell["median_cost_x2_micro"] == COST_X2 == 12_000_000, "6.00 USD, doubled"
        assert (cell["paying"], cell["share_ppm"], cell["unfillable"], cell["viable"]) == (0, 0, 0, False)
    assert result["viable_cells"] == []
    assert result["pairs"]["EUR.USD"] | {"cells": 0} == {
        "currency": "USD", "price_scale": 5, "size": SIZE, "sample_points": 1_200, "cells": 0,
    }  # fmt: skip
    quiet = result["pairs"]["USD.JPY"]
    assert quiet["sample_points"] == 0 and all(c["windows"] == 0 and c["viable"] is False for c in quiet["cells"])
    assert all(c["median_move_x2_micro"] is None and c["median_cost_x2_micro"] is None for c in quiet["cells"])


def test_a_book_that_jumps_by_more_than_the_cost_every_thirty_seconds_is_viable_from_thirty_seconds_up():
    result = mapped(stairs())  # 80 steps, the last at 2,400 s: 2,401 grid seconds
    found = cells(result)
    # A window of h seconds starting s seconds in holds a step when s is within h of the next multiple of 30.
    # Below 30 s that is h windows in every 30; from 30 s up every window holds h / 30 steps.
    assert {h: cell["windows"] for h, cell in found.items()} == {h: 2_401 - h for h in (1, 5, 10, 30, 60, 300)}
    paying = {h: cell["paying"] for h, cell in found.items()}
    assert paying == {1: 80, 5: 79 * 5 + 1, 10: 79 * 10 + 1, 30: 2_371, 60: 2_341, 300: 2_101}
    shares = {h: cell["share_ppm"] for h, cell in found.items()}
    assert shares == {1: 33_333, 5: 165_275, 10: 330_823, 30: 1_000_000, 60: 1_000_000, 300: 1_000_000}
    assert {h for h, cell in found.items() if cell["viable"]} == {30, 60, 300}, "exactly the holding times >= 30 s"
    assert result["viable_cells"] == [["EUR.USD", h * SECOND] for h in (30, 60, 300)]

    assert PAID_X2 == 14_000_000 > COST_X2, "7.00 USD against 6.00, both doubled"
    medians = {h: cell["median_move_x2_micro"] for h, cell in found.items()}
    assert medians == {1: 0, 5: 0, 10: 0, 30: PAID_X2, 60: 2 * PAID_X2, 300: 10 * PAID_X2}
    uppers = {h: cell["upper_move_x2_micro"] for h, cell in found.items()}
    assert uppers == {1: 0, 5: PAID_X2, 10: PAID_X2, 30: PAID_X2, 60: 2 * PAID_X2, 300: 10 * PAID_X2}, "the best tenth"
    assert all(cell["median_cost_x2_micro"] == COST_X2 and cell["unfillable"] == 0 for cell in found.values())


def test_a_move_of_exactly_the_cost_does_not_pay():
    exact = cells(mapped(stairs(jump=30)))  # 30 price units on 20,000 is 6.00 USD: the cost, and not a cent more
    assert exact[30]["median_move_x2_micro"] == COST_X2 and exact[30]["paying"] == 0 and not exact[30]["viable"]
    assert cells(mapped(stairs(jump=-JUMP)))[30]["paying"] == 2_371, "a fall is a move too: sold first, bought back"


def test_the_rule_is_two_settings_and_both_are_in_force():
    def viable(**rule) -> set[int]:
        return {h for h, cell in cells(mapped(stairs(), how=settings(**rule))).items() if cell["viable"]}

    assert viable(min_share_ppm=100_000) == {5, 10, 30, 60, 300}, "a tenth of the windows is enough: 16% and 33% pass"
    assert viable(min_share_ppm=165_276) == {10, 30, 60, 300}, "one millionth above the 5 s cell's exact share"
    assert viable(min_share_ppm=1_000_000) == {30, 60, 300}
    assert viable(min_windows=2_342) == {30} and viable(min_windows=2_372) == set(), "a cell needs enough windows too"


def test_a_quantile_is_by_nearest_rank_and_is_a_value_that_occurred():
    values = [1, 2, 3, 4]
    asked = (1, 250_000, 250_001, 500_000, 500_001, 900_000, 1_000_000)
    assert [quantile(values, ppm) for ppm in asked] == [1, 1, 2, 2, 3, 4, 4]
    assert quantile([7], 1) == quantile([7], 1_000_000) == 7


# ---- what counts as a sample, and what the cost model refuses to fill -----------------------------------------------


def test_a_book_nobody_confirmed_is_not_sampled_and_a_heartbeat_keeps_it():
    costs = plain_costs(stale_after_ns=2 * SECOND)
    first, later = constant(600), constant(600, start_ns=T0 + 10 * MINUTE)  # a minute, nine of silence, a minute
    silent = mapped([*first, *later], costs, settings(1, 30, 600))
    # The last quote of the first minute, at 59.9 s, stands for two seconds more: grid seconds 0 to 61, then 600 on.
    assert silent["pairs"]["EUR.USD"]["sample_points"] == 62 + 60
    assert {h: c["windows"] for h, c in cells(silent).items()} == {1: 61 + 59, 30: 32 + 30, 600: 60}
    beats = [Heartbeat("EUR.USD", T0 + s * SECOND, T0 + s * SECOND, 0) for s in range(60, 600)]
    vouched = mapped([*first, *beats, *later], costs, settings(1, 30, 600))
    assert vouched["pairs"]["EUR.USD"]["sample_points"] == 660, "the venue said each second: no change, and I am here"
    assert {h: c["windows"] for h, c in cells(vouched).items()} == {1: 659, 30: 630, 600: 60}


def test_a_shut_market_and_a_crossed_book_are_not_sampled():
    saturday = 1_760_184_000 * SECOND  # 2025-10-11 12:00 UTC
    assert mapped(constant(600, start_ns=saturday))["pairs"]["EUR.USD"]["sample_points"] == 0
    crossed = [Quote("EUR.USD", 108_520, 108_510, BIG, BIG, T0 + (20 + n) * SECOND, 1_000 + n) for n in range(10)]
    stream = sorted([*constant(600), *crossed], key=lambda q: q.recv_ts_ns)
    result = mapped(stream)
    # Each crossed quote stands from its own second until the good quote 100 ms after it: ten grid seconds lost.
    assert result["pairs"]["EUR.USD"]["sample_points"] == 60 - 10 and result["stream"]["crossed_points"] == 10


def test_near_the_rollover_the_spread_is_charged_wider_and_inside_the_window_nothing_pays():
    costs = plain_costs(**AROUND)

    def at(start_ns: int) -> dict:
        return cells(mapped(stairs(start_ns=start_ns, steps=10), costs))[30]  # five minutes: 271 windows of 30 s

    calm, wide, shut = at(ROLLOVER - 120 * MINUTE), at(ROLLOVER - 20 * MINUTE), at(ROLLOVER - 2 * MINUTE)
    assert (calm["median_cost_x2_micro"], calm["paying"], calm["unfillable"]) == (COST_X2, 271, 0)
    # A spread of 10 taken as 30 at both ends: (30 + 30) x 20,000 x 10 and twice two commissions. 10.00 USD.
    assert (wide["median_cost_x2_micro"], wide["paying"], wide["unfillable"]) == (20_000_000, 0, 0)
    assert (shut["windows"], shut["paying"], shut["unfillable"]) == (271, 0, 271), "seen, and not to be had"
    assert calm["viable"] and not wide["viable"] and not shut["viable"]


def test_a_window_pays_only_if_the_side_to_be_taken_shows_the_minimum_size():
    thin = cells(mapped(stairs(sizes=(BIG, SIZE - 1_000))))[30]  # a rise is bought at the start: the ask is thin
    assert (thin["paying"], thin["unfillable"]) == (0, 2_371)
    assert cells(mapped(stairs(sizes=(BIG, SIZE))))[30]["paying"] == 2_371, "exactly the minimum is enough"
    selling = cells(mapped(stairs(jump=-JUMP, sizes=(SIZE - 1_000, BIG))))[30]  # a fall is sold at the start
    assert (selling["paying"], selling["unfillable"]) == (0, 2_371)


def test_the_cost_is_for_the_larger_of_the_venues_and_the_models_minimum_size_in_the_pairs_own_currency():
    steps = [(151_230 + 40 * k, T0 + k * 30 * SECOND) for k in range(81)]
    yen = [Quote("USD.JPY", bid, bid + 10, BIG, BIG, at, k + 1) for k, (bid, at) in enumerate(steps)]
    costs = plain_costs(min_order_size=25_500, commission_minimum={"USD": 2_000_000, "JPY": 300_000_000})
    result = mapped(yen, costs)
    assert result["pairs"]["USD.JPY"]["size"] == 26_000 and result["pairs"]["USD.JPY"]["currency"] == "JPY"
    cell = cells(result, "USD.JPY")[30]
    # At scale 3 a price unit on 26,000 is 26 yen. Spread: (10 + 10) x 26,000 x 1,000; commissions: 4 x 300 yen.
    assert cell["median_cost_x2_micro"] == 20 * 26_000 * 1_000 + 4 * 300_000_000 == 1_720_000_000, "860 yen, doubled"
    assert cell["median_move_x2_micro"] == 2 * 40 * 26_000 * 1_000 == 2_080_000_000 and cell["viable"], "1,040 yen"


# ---- a pure function of the recording, in exact integers ------------------------------------------------------------


def leaves(value):
    if isinstance(value, dict):
        for inner in value.values():
            yield from leaves(inner)
    elif isinstance(value, list | tuple):
        for inner in value:
            yield from leaves(inner)
    else:
        yield value


def test_the_map_is_a_function_of_the_events_and_holds_integers_and_text_only():
    stream = walk(3, 6_000)
    noise = [Marker(True, T0, 1), ConnectionEvent(ConnectionState.RECONCILING, T0, 1)]
    stranger = [Quote("GBP.USD", 130_000, 130_010, BIG, BIG, T0 + SECOND, 9)]
    one, two = mapped(stream), mapped(iter([*noise, *stream[:10], *stranger, *stream[10:]]))
    assert one["stream"] == {"events": 6_000, "quotes": 6_000, "crossed_points": 0, "first_ns": T0,
                             "last_ns": stream[-1].recv_ts_ns}  # fmt: skip
    assert {**one, "stream": 0} == {**two, "stream": 0}, "what is not a quote of a known pair changes nothing"
    assert json.dumps(one, sort_keys=True) == json.dumps(mapped(list(stream)), sort_keys=True)
    assert {type(leaf) for leaf in leaves(one)} <= {int, str, bool, type(None)}, "no float anywhere"
    assert json.loads(json.dumps(one)) == one


def test_time_does_not_run_backwards_when_the_recorded_clock_stepped_back():
    quotes = constant(600)
    back = Quote("EUR.USD", 108_500, 108_510, BIG, BIG, T0 - 5 * SECOND, 301)  # the wall clock was corrected
    stepped = [*quotes[:300], back, *quotes[300:]]
    points, counts = sample_points(stepped, TRADED, plain_costs(), Calendar(), SECOND)
    assert sorted(points["EUR.USD"]) == [T0 + s * SECOND for s in range(60)] and counts["first_ns"] == T0


# ---- the map says what it was computed on ---------------------------------------------------------------------------


def test_a_map_on_placeholder_costs_says_so_and_carries_every_parameter_it_used():
    costs = plain_costs()
    result = mapped(constant(50), costs)
    assert costs.placeholder is True and result["placeholder"] is True and result["note"].startswith("NOT A RESULT")
    assert result["cost_model"] == costs.as_dict() and result["cost_model"]["placeholder"] is True
    assert result["settings"] == {
        "placeholder": False, "horizons_ns": [h * SECOND for h in (1, 5, 10, 30, 60, 300)], "step_ns": SECOND,
        "upper_quantile_ppm": 900_000, "min_windows": 100, "min_share_ppm": 500_000,
    }  # fmt: skip
    assert "TIMES TWO" in result["money"] and (result["kind"], result["version"]) == ("feasibility-map", 1)
    measured = plain_costs(placeholder=False)
    assert mapped(constant(50), measured)["placeholder"] is False
    undecided = mapped(constant(50), measured, settings(placeholder=True))
    assert undecided["placeholder"] is True, "a rule nobody has decided is a placeholder too"
    assert "NOT A RESULT" not in mapped(constant(50), measured)["note"]


def test_the_shipped_settings_load_and_say_they_are_placeholders():
    shipped = load_settings(ROOT / "config" / "feasibility.yaml")
    assert shipped.placeholder is True and shipped.step_ns == SECOND
    assert shipped.horizons_ns == tuple(h * SECOND for h in (1, 5, 30, 60, 300, 900))
    assert (shipped.upper_quantile_ppm, shipped.min_windows, shipped.min_share_ppm) == (900_000, 1_000, 500_000)
    text = (ROOT / "config" / "feasibility.yaml").read_text()
    assert text.count("PLACEHOLDER") >= 6 and "status: placeholder" in text


@pytest.mark.parametrize(
    "change",
    [("version: 1", "version: 2"), ("step_seconds: 1 ", "step_seconds: 0 "), ("step_seconds: 1 ", "step_seconds: 7 "),
     ("[1, 5, 30, 60, 300, 900]", "[1, 5, 5]"), ("[1, 5, 30, 60, 300, 900]", "[]"), ("[1, 5, 30,", "[1.5, 5, 30,"),
     ("[1, 5, 30, 60, 300, 900]", "60"), ("upper_quantile_ppm: 900000", "upper_quantile_ppm: 1000001"),
     ("min_windows: 1000", "min_windows: 0"), ("min_share_ppm: 500000", 'min_share_ppm: "0.5"'),
     ("viable:", "rule:")],
)  # fmt: skip
def test_settings_that_are_incomplete_or_inexact_are_refused(tmp_path, change):
    text = (ROOT / "config" / "feasibility.yaml").read_text()
    assert text.count(change[0]) == 1
    path = tmp_path / "feasibility.yaml"
    path.write_text(text.replace(*change))
    with pytest.raises(FeasibilityError):
        load_settings(path)
    with pytest.raises(FeasibilityError, match="cannot read"):
        load_settings(tmp_path / "missing.yaml")


# ---- the command ----------------------------------------------------------------------------------------------------


def recorded_store(tmp_path, seconds: int = 120):
    from hftlab.record import __main__ as record

    frozen = ["--config", str(PLACEHOLDER / "recorder.yaml")]
    assert record.main([*frozen, "--dry-run-seconds", str(seconds), "--root", str(tmp_path / "rec")]) == 0
    return tmp_path / "rec"


def test_the_command_maps_a_recorded_store_and_writes_json(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    store = recorded_store(tmp_path)
    capsys.readouterr()
    out = tmp_path / "maps" / "map.json"
    costs = ["--cost-model", str(PLACEHOLDER / "cost_model.yaml")]
    assert cli.main(["--store", str(store), "--out", str(out), *costs]) == 0
    printed, said = capsys.readouterr()
    assert printed == "" and "PLACEHOLDER" in said and f"1 day(s), map written to {out}" in said
    result = json.loads(out.read_text())
    assert result["days"] == ["2026-10-07"] and result["placeholder"] is True and result["viable_cells"] == []
    assert result["cost_model"]["placeholder"] is True and result["cost_model"]["commission_rate_bps_x100"] == 20
    assert sorted(result["pairs"]) == ["EUR.USD", "USD.JPY"] and result["stream"]["quotes"] > 5_000
    for pair, currency in (("EUR.USD", "USD"), ("USD.JPY", "JPY")):
        line = result["pairs"][pair]
        assert (line["currency"], line["size"]) == (currency, 20_000) and 118 <= line["sample_points"] <= 121
        by_horizon = {cell["horizon_ns"] // SECOND: cell for cell in line["cells"]}
        assert sorted(by_horizon) == [1, 5, 30, 60, 300, 900]
        assert by_horizon[5]["windows"] == line["sample_points"] - 5 and by_horizon[300]["windows"] == 0
        assert by_horizon[5]["median_cost_x2_micro"] > 0 and by_horizon[900]["median_cost_x2_micro"] is None
        assert not any(cell["viable"] for cell in line["cells"]), "two minutes are fewer windows than the rule asks"

    assert cli.main(["--store", str(store), "--day", "2026-10-07", *costs]) == 0
    assert json.loads(capsys.readouterr().out) == result, "the same recording, the same map, printed this time"


def test_what_cannot_be_mapped_is_refused_with_the_reason(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    store = recorded_store(tmp_path, 2)
    (tmp_path / "bare" / "2026-10-07").mkdir(parents=True)
    capsys.readouterr()
    for arguments, message in (
        (["--store", str(tmp_path / "nowhere")], "holds no recorded day"),
        (["--store", str(store), "--day", "2026-10-08"], "has no day 2026-10-08"),
        (["--store", str(tmp_path / "bare")], "does not say what its instruments are"),
        (["--store", str(store), "--config", str(tmp_path / "none.yaml")], "cannot read"),
        (["--store", str(store), "--cost-model", str(tmp_path / "none.yaml")], "cannot read"),
        (["--store", str(store), "--calendar", str(tmp_path / "none")], "is not a directory"),
    ):
        assert cli.main(arguments) == 2, arguments
        said = capsys.readouterr().err
        assert said.startswith("feasibility: ") and message in said, arguments
