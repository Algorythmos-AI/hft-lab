"""The Directional Change detector: events at the tick that crossed, alternating, exact, blind to what comes next."""
from __future__ import annotations

import ast
import itertools
import json
import random
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import Quote
from hftlab.strategy.dc import DOWN, UP, Completed, DcEvent, Detector, DetectorError, Provisional, from_percent, mid
from hftlab.strategy.dc import detector as module
from tests.support import ROOT

ONE_PERCENT = Fraction(1, 100)


def run(detector: Detector, prices, state=None, start: int = 0):
    """Feed prices one by one, a nanosecond apart from `start`. The final state and every event, in order."""
    state, events = detector.initial() if state is None else state, []
    for number, price in enumerate(prices, start):
        state, found = detector.step(state, Fraction(price), number)
        events += found
    return state, events


def brief(events) -> list[tuple]:
    return [(e.number, e.direction, e.confirmed_ns, e.confirmed_price, e.extreme_ns, e.extreme_price) for e in events]


# ---- by hand --------------------------------------------------------------------------------------------------------


def test_a_fall_of_one_percent_from_the_high_and_then_a_rise_of_one_percent_from_the_low_by_hand():
    prices = ["100", "102", "101.5", "101", "100", "100.5", "99", "99.9", "101"]
    _, events = run(Detector([ONE_PERCENT], warm_up=0), prices)
    # High 102 at tick 1. Its level is 102 less 1.02: 100.98. 101.5 and 101 are short of it, 100 is beyond.
    # Low 99 at tick 6. Its level is 99 plus 0.99: 99.99. 99.9 is short of it, 101 is beyond.
    assert brief(events) == [
        (1, DOWN, 4, Fraction(100), 1, Fraction(102)),
        (2, UP, 8, Fraction(101), 6, Fraction(99)),
    ]
    assert [e.ticks for e in events] == [5, 4], "ticks 0 to 4, then ticks 5 to 8: the confirming tick is counted"
    assert all(e.threshold == ONE_PERCENT and not e.warm_up for e in events)


def test_a_machine_that_starts_down_reads_the_same_prices_from_their_low():
    prices = ["100", "98", "98.5", "99", "99.5", "101", "100.5", "99.9"]
    _, events = run(Detector([ONE_PERCENT], warm_up=0, start=DOWN), prices)
    # Low 98 at tick 1, level 98.98: 99 confirms. Then the high runs to 101 at tick 5, level 99.99: 99.9 confirms.
    assert brief(events) == [
        (1, UP, 3, Fraction(99), 1, Fraction(98)),
        (2, DOWN, 7, Fraction("99.9"), 5, Fraction(101)),
    ]


def test_an_event_is_booked_at_the_tick_that_crossed_and_never_at_the_level_it_jumped_past():
    _, (event,) = run(Detector([ONE_PERCENT], warm_up=0), ["200", "150"])
    assert (event.confirmed_price, event.confirmed_ns) == (Fraction(150), 1), "the level stood at 198"
    assert (event.extreme_price, event.extreme_ns) == (Fraction(200), 0)
    state, _ = run(Detector([ONE_PERCENT], warm_up=0), ["200", "150"])
    assert state.machines[0].extreme_price == Fraction(150), "and the new extreme is the confirming price itself"


def test_the_comparison_is_inclusive_a_move_of_exactly_the_threshold_confirms_and_a_hair_less_does_not():
    detector = Detector([ONE_PERCENT], warm_up=0)
    hair = Fraction(1, 10**12)
    assert run(detector, [100, 99 + hair])[1] == [], "0.999...% of the high"
    assert [e.confirmed_price for e in run(detector, [100, 99])[1]] == [Fraction(99)], "1% of the high, exactly"
    rising = Detector([ONE_PERCENT], warm_up=0, start=DOWN)
    assert run(rising, [100, 101 - hair])[1] == []
    assert [e.direction for e in run(rising, [100, 101])[1]] == [UP]


def test_the_move_is_divided_by_the_extreme_not_by_the_price_that_confirms():
    # From a low of 100 a rise to 101 is 1/100 of the extreme and 1/101 of the confirming price.
    detector = Detector([ONE_PERCENT], warm_up=0, start=DOWN)
    assert len(run(detector, [100, 101])[1]) == 1
    # From a high of 100 a fall to 99.01 is 0.99/100 of the extreme (no event) and 0.99/99.01 > 1% of the price.
    assert run(Detector([ONE_PERCENT], warm_up=0), [100, "99.01"])[1] == []


def test_a_repeated_price_is_dropped_before_any_machine_sees_it():
    detector = Detector([ONE_PERCENT], warm_up=0)
    plain = run(detector, [100, 102, 101, 100])
    state, events = run(detector, [100, 100, 102, 102, 102, 101, 101, 100, 100])
    assert [(e.direction, e.confirmed_price, e.ticks) for e in events] == [(DOWN, Fraction(100), 4)] == [
        (e.direction, e.confirmed_price, e.ticks) for e in plain[1]
    ], "four ticks, not nine"
    assert events[0].confirmed_ns == 7, "the first tick at the price is the one that confirms"
    assert state.ticks == plain[0].ticks == 4
    assert detector.step(state, Fraction(100), 99) == (state, ()), "a repeat changes nothing, not even the state"
    back = run(detector, [100, 102, 100, 102, 100])[0]
    assert back.ticks == 5, "a price that comes back after another is not a repeat"


# ---- the two overshoots ---------------------------------------------------------------------------------------------


def test_the_overshoot_is_provisional_until_the_next_event_fixes_it():
    detector = Detector([ONE_PERCENT], warm_up=0)
    state = detector.initial()
    seen = []
    for at, price in enumerate([100, 99, 98, 97, "97.5", 96, "96.5", 97]):
        state, events = detector.step(state, Fraction(price), at)
        seen.append((detector.provisional(state), events))
    # 99 confirms DOWN from the high of 100. The low then runs to 96, and 97 (more than 0.96 above it) confirms UP.
    assert seen[0] == (None, ()), "no event yet, so no overshoot of any kind"
    first = seen[1][1][0]
    assert (first.direction, first.completes) == (DOWN, None), "the first event completes nothing"
    so_far = [found.overshoot for found, _ in seen[1:7]]
    assert so_far == [0, Fraction(1, 99), Fraction(2, 99), Fraction(2, 99), Fraction(3, 99), Fraction(3, 99)]
    assert all(type(found) is Provisional and found.event == 1 and found.direction == DOWN for found, _ in seen[1:7])
    (second,) = seen[7][1]
    done = second.completes
    assert type(done) is Completed and (done.event, done.direction) == (1, DOWN)
    assert (done.start_price, done.start_ns, done.extreme_price, done.extreme_ns) == (99, 1, 96, 5)
    assert done.overshoot == Fraction(3, 99) == seen[6][0].overshoot, "what was provisional the tick before, now fixed"
    assert done.total_move == Fraction(4, 100), "from the high of 100 to the low of 96, as a share of the high"
    after = seen[7][0]
    assert (after.event, after.direction, after.overshoot) == (2, UP, 0), "and the next one starts at nothing"


def test_a_completed_overshoot_can_be_had_from_the_event_that_completes_it_and_from_nowhere_else():
    tree = ast.parse((ROOT / "src/hftlab/strategy/dc/detector.py").read_text())
    made = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Completed"
    ]
    assert len(made) == 1, "one place builds a Completed: the confirmation of the next event"
    returning = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.returns is not None and "Completed" in ast.unparse(node.returns)
    ]
    assert returning == [], "no function hands one out: it travels on the event"
    fields = {name for kind in (module.Machine, module.DetectorState) for name in kind.__dataclass_fields__}
    assert not {name for name in fields if "complet" in name or "overshoot" in name}, "and no state keeps one"
    assert not set(Provisional.__dataclass_fields__) & {"origin_price"} and Provisional is not Completed
    assert not hasattr(Provisional, "total_move"), "a total move needs the end, and a provisional one has none"


# ---- warm-up and gaps -----------------------------------------------------------------------------------------------

ZIGZAG = [100, 98, 100, 98, 100, 98, 100, 98, 100]  # an event on every tick after the first, starting DOWN


def test_the_first_events_after_the_start_are_flagged_as_many_as_the_setting_says():
    for count in (0, 1, 2, 5):
        _, events = run(Detector([ONE_PERCENT], warm_up=count), ZIGZAG)
        assert [e.warm_up for e in events] == [True] * count + [False] * (8 - count)
    assert Detector([ONE_PERCENT]).warm_up == 2, "two, unless the settings say otherwise"


def test_after_a_gap_the_extreme_is_forgotten_the_next_events_are_flagged_and_the_counter_and_the_mode_go_on():
    detector = Detector([ONE_PERCENT], warm_up=2)
    state, before = run(detector, ZIGZAG[:5])
    assert [(e.number, e.direction, e.warm_up) for e in before] == [
        (1, DOWN, True), (2, UP, True), (3, DOWN, False), (4, UP, False),
    ]  # fmt: skip
    assert before[3].completes is not None and detector.provisional(state) is not None
    state = detector.gap(state)
    assert detector.provisional(state) is None, "the overshoot in hand is dropped"
    assert state.machines[0].extreme_price is None and state.last_price is None
    state, after = run(detector, [50, 49, 50, 49, 50], state, start=100)
    assert [(e.number, e.direction, e.warm_up) for e in after] == [
        (5, DOWN, True), (6, UP, True), (7, DOWN, False), (8, UP, False),
    ], "still alternating across the gap, and two more are discarded"  # fmt: skip
    assert after[0].extreme_price == 50, "a price from before the gap is not an extreme after it"
    assert after[0].completes is None, "and the overshoot the gap cut is completed by nothing"
    assert (after[0].ticks, after[1].completes.event) == (2, 5), "ticks are counted from the gap"
    assert detector.provisional(state).warm_up is False and detector.provisional(run(detector, [50, 49])[0]).warm_up


def test_the_first_price_after_a_gap_is_a_tick_even_when_it_repeats_the_last_one_before():
    detector = Detector([ONE_PERCENT], warm_up=0)
    state = detector.gap(run(detector, [100, 102])[0])
    state, _ = detector.step(state, Fraction(102), 9)
    assert state.machines[0].extreme_price == 102 and state.machines[0].extreme_ns == 9


# ---- several thresholds ---------------------------------------------------------------------------------------------

PRICES = st.lists(st.integers(9_000, 11_000), min_size=1, max_size=120)
SHARES = st.sampled_from([Fraction(1, 1000), Fraction(1, 200), ONE_PERCENT, Fraction(3, 100)])


@given(prices=PRICES, shares=st.lists(SHARES, min_size=1, max_size=3, unique=True))
def test_each_threshold_is_its_own_machine_and_sees_what_it_would_see_alone(prices, shares):
    _, together = run(Detector(shares, warm_up=1), prices)
    for share in shares:
        _, alone = run(Detector([share], warm_up=1), prices)
        assert [e for e in together if e.threshold == share] == alone


# ---- no look-ahead --------------------------------------------------------------------------------------------------


@given(prices=PRICES, share=SHARES, start=st.sampled_from([UP, DOWN]), warm=st.integers(0, 3))
def test_events_alternate_and_an_event_never_changes_when_later_ticks_are_appended(prices, share, start, warm):
    detector = Detector([share], warm_up=warm, start=start)
    _, whole = run(detector, prices)
    directions = [e.direction for e in whole]
    assert all(a != b for a, b in itertools.pairwise(directions)), "up, down, up, down"
    assert directions[:1] in ([], [DOWN if start == UP else UP]), "the first leaves the starting mode"
    assert [e.number for e in whole] == list(range(1, len(whole) + 1))
    assert [e.warm_up for e in whole] == [n < warm for n in range(len(whole))]
    for cut in range(len(prices) + 1):
        _, so_far = run(detector, prices[:cut])
        assert so_far == [e for e in whole if e.confirmed_ns < cut], "the events of a prefix are a prefix of the events"
    for event in whole:
        assert event.extreme_ns <= event.confirmed_ns, "an event names nothing later than its own tick"
        assert abs(event.extreme_price - event.confirmed_price) >= share * event.extreme_price
        done = event.completes
        if done is not None:
            assert done.extreme_ns <= event.confirmed_ns and done.extreme_price == event.extreme_price
            assert done.event == event.number - 1 and done.direction != event.direction


@given(prices=PRICES, share=SHARES, cut=st.integers(0, 120))
def test_the_provisional_overshoot_at_a_tick_is_a_function_of_the_ticks_up_to_it(prices, share, cut):
    detector = Detector([share], warm_up=0)
    at = min(cut, len(prices))
    alone = detector.provisional(run(detector, prices[:at])[0])
    state = detector.initial()
    for number, price in enumerate(prices):
        if number == at:
            assert detector.provisional(state) == alone, "the ticks still to come are not in it"
        state, _ = detector.step(state, Fraction(price), number)


# ---- written out and read back --------------------------------------------------------------------------------------


def leaves(value):
    if isinstance(value, dict | list):
        for inner in value.values() if isinstance(value, dict) else value:
            yield from leaves(inner)
    else:
        yield value


@given(prices=PRICES, cut=st.integers(0, 120), gap=st.booleans())
def test_a_state_written_out_and_read_back_continues_exactly_as_if_nothing_had_happened(prices, cut, gap):
    detector = Detector([Fraction(1, 200), ONE_PERCENT], warm_up=2)
    cut = min(cut, len(prices))
    state, _ = run(detector, prices[:cut])
    if gap:
        state = detector.gap(state)
    text = json.dumps(detector.dump(state), sort_keys=True)
    restored = Detector([Fraction(1, 200), ONE_PERCENT], warm_up=2).load(json.loads(text))
    assert restored == state, "the same value, the level it confirms at included"
    assert run(detector, prices[cut:], restored, cut) == run(detector, prices[cut:], state, cut)
    assert all(type(leaf) in (int, str, type(None)) for leaf in leaves(json.loads(text))), "no float in it"


def test_a_restored_machine_is_not_put_back_into_its_warm_up():
    detector = Detector([ONE_PERCENT], warm_up=2)
    state, events = run(detector, ZIGZAG[:4])
    assert [e.warm_up for e in events] == [True, True, False]
    restored = detector.load(json.loads(json.dumps(detector.dump(state))))
    _, after = run(detector, ZIGZAG[4:], restored, 4)
    assert [e.warm_up for e in after] == [False] * 5 and after[0].completes is not None


def test_a_saved_state_is_refused_by_a_detector_with_other_settings_and_when_it_is_damaged():
    detector = Detector([ONE_PERCENT], warm_up=2)
    saved = detector.dump(run(detector, ZIGZAG)[0])
    others = (
        Detector([Fraction(1, 50)]), Detector([ONE_PERCENT], warm_up=3), Detector([ONE_PERCENT], start=DOWN),
        Detector([ONE_PERCENT, Fraction(1, 50)]),
    )  # fmt: skip
    for other in others:
        with pytest.raises(DetectorError, match="other settings"):
            other.load(saved)
    machine = saved["machines"][0]
    damaged = [
        {**saved, "version": 2}, {**saved, "extra": 1}, {**saved, "machines": []}, {**saved, "ticks": -1},
        {**saved, "last_price": [100.0, 1]}, {**saved, "last_price": [200, 2]}, {**saved, "last_price": [0, 1]},
        {**saved, "machines": [{**machine, "mode": "sideways"}]}, {**saved, "machines": [{**machine, "mark": "1"}]},
        {**saved, "machines": [{k: v for k, v in machine.items() if k != "warm"}]}, [], None,
    ]  # fmt: skip
    for bad in damaged:
        with pytest.raises(DetectorError):
            detector.load(bad)


# ---- settings -------------------------------------------------------------------------------------------------------


def test_the_price_is_the_mid_as_an_exact_rational_and_the_series_is_a_named_setting():
    quote = Quote("EUR.USD", 108_500, 108_505, 1, 1, 0, 1)
    assert mid(quote) == Fraction(217_005, 2) and type(mid(quote)) is Fraction, "between two price units, and exact"
    detector = Detector([ONE_PERCENT, Fraction(1, 400)], warm_up=3)
    assert detector.settings() == {"price": "mid", "thresholds": [[1, 100], [1, 400]], "warm_up": 3, "start": "up"}
    with pytest.raises(DetectorError, match="mid"):
        Detector([ONE_PERCENT], price="last")


def test_a_threshold_in_percent_is_converted_once_and_exactly():
    assert from_percent("0.25") == Fraction(1, 400) and from_percent("1") == ONE_PERCENT
    assert from_percent("0.015") == Fraction(3, 20_000), "no float on the way"
    for bad in (0.25, 1, None, "a quarter", "", "nan", "inf"):
        with pytest.raises(DetectorError):
            from_percent(bad)


def test_settings_and_prices_that_make_no_sense_are_refused():
    bad_settings = (
        {"thresholds": []}, {"thresholds": [0.01]}, {"thresholds": [Fraction(0)]}, {"thresholds": [Fraction(1)]},
        {"thresholds": [ONE_PERCENT, ONE_PERCENT]}, {"thresholds": [1]}, {"warm_up": -1}, {"warm_up": 1.0},
        {"start": "flat"},
    )  # fmt: skip
    for bad in bad_settings:
        with pytest.raises(DetectorError):
            Detector(**{"thresholds": [ONE_PERCENT], **bad})
    detector = Detector([ONE_PERCENT])
    for price in (100.0, 100, "100", Fraction(0), Fraction(-1)):
        with pytest.raises(DetectorError, match="exact rational"):
            detector.step(detector.initial(), price, 0)


def test_a_step_is_a_function_of_its_arguments_and_leaves_the_detector_as_it_was():
    detector = Detector([ONE_PERCENT])
    before = dict(vars(detector))
    state, _ = run(detector, ZIGZAG[:3])
    first, second = detector.step(state, Fraction(98), 3), detector.step(state, Fraction(98), 3)
    assert first == second and hash(first[0]) == hash(second[0]) and vars(detector) == before
    assert all(isinstance(event, DcEvent) for event in first[1])


# ---- the published statistics, as a check of the detector -----------------------------------------------------------

TICKS = 200_000
START = 100_000
SHARE = Fraction(21, 200_000)  # 10.5 price units at 100,000: between two grid points, on purpose
STEPS = 11  # so the smallest move against an extreme that confirms is 11 units, anywhere this walk goes
TOLERANCE = Fraction(1, 10)


def test_on_a_driftless_walk_the_mean_overshoot_is_about_the_threshold_and_the_mean_total_move_about_twice_it():
    """A check that the detector measures what the published method describes. Never a trading target.

    For a driftless walk the published statistics say that a confirmed move is followed, on average, by an
    overshoot of about the threshold, so that the mean move from extreme to extreme is about twice it. Here
    the walk moves one unit a tick, and on a grid a move confirms at the first grid point at or beyond the
    level: 11 units where the threshold asks for 10.5. The exact expectations are therefore 11 units for the
    overshoot and 22 for the total move, which is 11/10.5 and 22/10.5 of the threshold: 4.8% above the
    continuous figures. The stated tolerance is 10%: that known 4.8% of the grid, and two standard errors
    of the mean of about 1,500 overshoots, each about as variable as it is large (2 / sqrt(1,500) = 5.2%).
    """
    rng, price, events = random.Random(20261011), START, []
    detector = Detector([SHARE])
    state = detector.initial()
    low = high = price
    for at in range(TICKS):
        price += rng.choice((-1, 1))
        low, high = min(low, price), max(high, price)
        state, found = detector.step(state, Fraction(price), at)
        events += found
    assert SHARE * low > STEPS - 1 and SHARE * high < STEPS, "the walk stayed where 11 units is the confirming move"
    assert all(abs(e.extreme_price - e.confirmed_price) == STEPS for e in events), "every event at its own tick"
    done = [e.completes for e in events if e.completes is not None and not e.warm_up]
    assert len(done) > 1_400
    overshoot = sum((c.overshoot for c in done), Fraction(0)) / len(done)
    total = sum((c.total_move for c in done), Fraction(0)) / len(done)
    assert abs(overshoot / SHARE - 1) <= TOLERANCE, f"mean overshoot is {float(overshoot / SHARE):.3f} thresholds"
    assert abs(total / (2 * SHARE) - 1) <= TOLERANCE, f"mean total move is {float(total / SHARE):.3f} thresholds"
    in_units = sum((abs(c.extreme_price - c.start_price) for c in done), Fraction(0)) / len(done)
    assert abs(in_units / STEPS - 1) <= Fraction(6, 100), "and within sampling error of the grid's exact 11 units"
