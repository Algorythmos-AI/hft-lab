"""The null benchmark: walks as volatile as the stream, the same every time, and a real pattern stands out of them."""
from __future__ import annotations

import itertools
from fractions import Fraction

import pytest

from hftlab.core import Quote
from hftlab.strategy.dc import from_percent, null
from tests.shadow.kit import session, walk
from tests.strategy.dc_kit import BIG, dc, net, quotes, zigzag
from tests.venues.kit import EURUSD, T0, USDJPY

SMALL = from_percent("0.0484")  # 52.5 price units here: eleven steps of five
WALKS = 19


def runner(tmp_path):
    """The function the helper is handed: one trend sleeve over a stream in the shadow runner, and its net result."""
    numbers = itertools.count()

    def result_of(stream) -> int:
        result, _, _ = session(tmp_path / f"{next(numbers)}.jsonl", [dc(threshold=SMALL)], stream)
        return net(result)

    return result_of


# ---- the walk itself ------------------------------------------------------------------------------------------------


def test_the_step_and_how_often_it_is_taken_match_the_streams_mean_squared_change_exactly():
    still, every = quotes([108_000] * 50), quotes(zigzag(2))
    assert null.mean_square_x4(every) == 100, "the mid moves 5 units every tick: twice the mid moves 10"
    assert null.step_and_share(every, EURUSD) == (10, Fraction(1)), "one increment, on every tick"
    assert null.step_and_share(still, EURUSD) == (10, Fraction(0)), "a still stream gives walks that never move"
    some = walk(3, 2_400)
    step, share = null.step_and_share(some, EURUSD)
    assert step == 10 and share * step * step == null.mean_square_x4(some), "as an exact rational"
    wild = quotes([108_000 + 35 * (n % 2) for n in range(100)])  # 35 units a tick: seven increments
    step, share = null.step_and_share(wild, EURUSD)
    assert (step, share) == (70, Fraction(1)), "the smallest number of increments whose share is not above one"
    odd = quotes([108_000 + 15 * (n % 3 == 0) for n in range(100)])
    step, share = null.step_and_share(odd, EURUSD)
    # Two ticks in three move the mid by 15 units: a mean square of about 600 for twice the mid. Two increments
    # (400) are too few, so the step is three (900), taken about two times in three.
    assert step == 30 and Fraction(3, 5) < share < Fraction(7, 10) and share * 900 == null.mean_square_x4(odd)


def test_a_walk_keeps_every_tick_of_the_stream_and_replaces_only_the_path_of_the_mid():
    stream = walk(5, 3_000)
    made = null.walk(stream, EURUSD, seed=1, number=0)
    assert [(q.recv_ts_ns, q.seq, q.bid_size, q.ask_size, q.pair) for q in made] == [
        (q.recv_ts_ns, q.seq, q.bid_size, q.ask_size, q.pair) for q in stream
    ]
    assert made[0] == stream[0], "the first tick keeps its mid"
    assert all(q.bid % 5 == 0 and q.ask % 5 == 0 and q.spread == 10 for q in made), "on the price grid, the same spread"
    moves = [after.mid_x2 - before.mid_x2 for before, after in itertools.pairwise(made)]
    assert set(moves) == {-10, 0, 10}, "up an increment, down an increment, or not at all"
    assert [q.mid_x2 for q in made] != [q.mid_x2 for q in stream]


def test_over_many_ticks_a_walk_is_as_volatile_as_the_stream_and_has_no_drift():
    """The stated tolerance is 5% on the mean squared change, over 20 walks of 3,000 ticks: 60,000 draws at a
    share of about two thirds have a standard error of 0.3% of it, so 5% is far outside luck and well inside
    any mistake in the matching. The drift: the sum of all 60,000 changes within three standard errors of zero.
    """
    stream = walk(5, 3_000)
    wanted = null.mean_square_x4(stream)
    moves = [
        after.mid_x2 - before.mid_x2
        for number in range(20)
        for before, after in itertools.pairwise(null.walk(stream, EURUSD, seed=7, number=number))
    ]
    found = Fraction(sum(move * move for move in moves), len(moves))
    assert abs(found / wanted - 1) < Fraction(5, 100)
    assert sum(moves) ** 2 <= 9 * sum(move * move for move in moves), "ups and downs in balance"
    ups, downs = moves.count(10), moves.count(-10)
    assert abs(ups - downs) ** 2 <= 9 * (ups + downs)


def test_a_walk_is_fixed_by_the_seed_and_its_number_and_a_tick_by_its_own_place_in_it():
    stream = walk(5, 800)
    base = null.walk(stream, EURUSD, seed=1, number=0)
    assert base == null.walk(list(stream), EURUSD, seed=1, number=0)
    assert base != null.walk(stream, EURUSD, seed=2, number=0) and base != null.walk(stream, EURUSD, seed=1, number=1)
    assert null.draw(1, 0, 5) == null.draw(1, 0, 5) and len({null.draw(1, 0, tick) for tick in range(500)}) == 500
    drawn = [null.draw(3, 2, tick) for tick in range(20_000)]
    assert all(0 <= below < 2**63 for below, _ in drawn) and {sign for _, sign in drawn} == {-1, 1}
    assert 9_700 < sum(sign > 0 for _, sign in drawn) < 10_300, "a fair coin"
    assert 4_800 < sum(below < 2**61 for below, _ in drawn) < 5_200, "a quarter of them in the lowest quarter"


def test_a_spread_that_does_not_fit_the_grid_position_of_the_mid_is_widened_by_one_increment_never_narrowed():
    stream = [
        Quote("EUR.USD", 108_500, 108_510 + 5 * (n % 2), BIG, BIG, T0 + n, n + 1) for n in range(400)
    ]  # spreads of 10 and 15 in turn
    made = null.walk(stream, EURUSD, seed=1, number=0)
    assert [q.spread for q in made[:4]] == [10, 20, 10, 20], "15 becomes 20: both sides stay on the grid"
    assert all(q.bid % 5 == 0 and q.ask % 5 == 0 and q.spread >= s.spread for q, s in zip(made, stream, strict=True))
    assert {(q.spread - s.spread) for q, s in zip(made, stream, strict=True)} == {0, 5}


def test_streams_and_settings_no_walk_can_be_made_from_are_refused():
    stream = walk(5, 50)
    for bad in ([], stream[:1], [*stream, quotes([108_000], pair="USD.JPY")[0]]):
        with pytest.raises(null.NullError):
            null.walk(bad, EURUSD, seed=1, number=0)
    with pytest.raises(null.NullError, match="price grid"):
        null.walk([Quote("EUR.USD", 108_501, 108_511, 1, 1, T0 + n, n + 1) for n in range(5)], EURUSD, seed=1, number=0)
    with pytest.raises(null.NullError, match="alone"):
        null.walk(stream, USDJPY, seed=1, number=0)
    low = [Quote("EUR.USD", 5 + 5 * (n % 2), 15 + 5 * (n % 2), 1, 1, T0 + n, n + 1) for n in range(2_000)]
    with pytest.raises(null.NullError, match="not above zero"):
        null.walk(low, EURUSD, seed=1, number=0)
    for walks in (0, -1, 2.0):
        with pytest.raises(null.NullError):
            null.results(lambda made: 0, stream, EURUSD, walks=walks, seed=1)
    with pytest.raises(null.NullError):
        null.at_or_above(1, [])


def test_where_a_result_stands_among_the_null_is_an_exact_share_and_is_never_zero():
    assert null.at_or_above(5, [1, 2, 3]) == Fraction(1, 4), "above all three: one in four, itself"
    assert null.at_or_above(2, [1, 2, 3]) == Fraction(3, 4), "a tie counts against it"
    assert null.at_or_above(0, [1, 2, 3]) == 1


# ---- the helper, with the sleeve in the shadow runner ---------------------------------------------------------------


def test_the_distribution_is_the_same_every_time_it_is_asked_for(tmp_path):
    stream = walk(2, 1_500)
    first = null.results(runner(tmp_path / "a"), stream, EURUSD, walks=6, seed=1)
    assert first == null.results(runner(tmp_path / "b"), stream, EURUSD, walks=6, seed=1)
    assert first != null.results(runner(tmp_path / "c"), stream, EURUSD, walks=6, seed=2)
    assert len(first) == 6 and all(type(result) is int for result in first) and len(set(first)) > 1
    seen = []
    null.results(lambda made: seen.append(made) or 0, stream, EURUSD, walks=3, seed=1)
    assert seen == [null.walk(stream, EURUSD, seed=1, number=number) for number in range(3)], "walks 0, 1, 2, in order"


def test_a_planted_reversal_pattern_stands_out_of_its_null_and_a_plain_walk_does_not(tmp_path):
    """The zig-zag is a real pattern: every turn is followed by a run of five times the threshold and more. The
    trend variant earns on it, and no walk of the same volatility comes near: 1/20, the least 19 walks can say.

    A plain walk has nothing in it. Read against its own null, each of six of them has at least two of its 19
    walks doing as well or better, and the six land all over the range. That is what "not separated" looks like.
    """
    planted = quotes(zigzag(20))
    result_of = runner(tmp_path)
    earned = result_of(planted)
    against = null.results(result_of, planted, EURUSD, walks=WALKS, seed=1)
    assert earned > 1_000_000_000 and max(against) < 0, "it earned; every walk lost"
    assert null.at_or_above(earned, against) == Fraction(1, 20)
    shares = []
    for seed in range(1, 7):
        plain = walk(seed, 2_400)
        shares.append(null.at_or_above(result_of(plain), null.results(result_of, plain, EURUSD, walks=WALKS, seed=1)))
    assert min(shares) >= Fraction(3, 20), "none of them stands out"
    assert max(shares) > Fraction(1, 2) > min(shares), "and they fall on both sides of the middle"
