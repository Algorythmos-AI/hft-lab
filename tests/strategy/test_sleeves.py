"""The sleeve interface: a decision is a pure function of the events so far, the event's time and the book view."""
from __future__ import annotations

import ast
import copy
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.book import SleeveView
from hftlab.core import Fill, OrderEvent, OrderState, Position, Quote, Side, TimeInForce, client_order_id
from hftlab.record.format import Heartbeat, Marker
from hftlab.strategy import Cancel, ControlSleeve, IdleSleeve, Intent, Placed, Refused
from hftlab.strategy.control import ENTERING, EXITING, FLAT, HOLDING, PairState, draw
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.support import called_name, imports, sources
from tests.venues.kit import MS, SECOND, T0

SIZES = {"EUR.USD": 20_000, "USD.JPY": 20_000}


def view(**quantities: int) -> SleeveView:
    positions = {pair.replace("_", "."): Position(pair.replace("_", "."), q) for pair, q in quantities.items()}
    return SleeveView("control", MappingProxyType(positions), MappingProxyType({}), MappingProxyType({}))


def quote(seq: int, at: int = T0, pair: str = "EUR.USD") -> Quote:
    return Quote(pair, 108_500, 108_510, 10**6, 10**6, at, seq)


def control(**changes) -> ControlSleeve:
    settings = {"seed": 7, "sizes": SIZES, "entry_per_million": 1_000_000, "hold_ns": 2 * SECOND, "retry_ns": SECOND}
    return ControlSleeve("control", **{**settings, **changes})


def an_id(number: int = 1) -> str:
    return client_order_id(sleeve="control", pair="EUR.USD", side=Side.BUY, decision_seq=number)


def a_fill(side: Side, order_id: str, at: int) -> Fill:
    return Fill(f"S.{at}", order_id, "EUR.USD", side, 20_000, 108_510, at + 9 * MS, venue_ts_ns=at)


# ---- the sleeve that never trades -----------------------------------------------------------------------------------

EVENTS = st.one_of(
    st.builds(quote, st.integers(1, 10**6), st.integers(T0, T0 + 10**12)),
    st.builds(Heartbeat, st.just("EUR.USD"), st.integers(T0, T0 + 10**12), st.just(T0), st.just(0)),
    st.builds(ConnectionEvent, st.sampled_from(ConnectionState), st.just(T0), st.integers(1, 99)),
    st.builds(Marker, st.booleans(), st.just(T0), st.integers(1, 9)),
)


@given(events=st.lists(EVENTS, max_size=30))
def test_the_idle_sleeve_wants_nothing_whatever_it_is_shown(events):
    sleeve, state = IdleSleeve(), IdleSleeve().initial()
    for event in events:
        state, actions = sleeve.decide(state, event, T0, view())
        assert state is None and tuple(actions) == ()


# ---- the control sleeve's randomness --------------------------------------------------------------------------------


def test_a_draw_is_fixed_by_the_seed_and_the_quote_and_by_nothing_else():
    assert draw(7, "EUR.USD", T0, 5) == draw(7, "EUR.USD", T0, 5)
    changed = [(8, "EUR.USD", T0, 5), (7, "USD.JPY", T0, 5), (7, "EUR.USD", T0 + 1, 5), (7, "EUR.USD", T0, 6)]
    others = {draw(*arguments) for arguments in changed}
    assert draw(7, "EUR.USD", T0, 5) not in others and len(others) == 4


def test_draws_are_spread_evenly_and_the_side_is_a_fair_coin():
    drawn = [draw(3, "EUR.USD", T0 + n * MS, n) for n in range(40_000)]
    assert all(0 <= number < 1_000_000 for number, _ in drawn)
    below = sum(number < 50_000 for number, _ in drawn)
    buys = sum(side is Side.BUY for _, side in drawn)
    assert 1_800 < below < 2_200, "5% of 40,000 is 2,000"
    assert 19_600 < buys < 20_400, "half of 40,000 is 20,000"
    chosen = [side for number, side in drawn if number < 50_000]
    assert 0.45 < sum(side is Side.BUY for side in chosen) / len(chosen) < 0.55, "the side does not lean on the entry"


def test_the_entry_rate_is_the_share_of_quotes_it_enters_on():
    for rate, low, high in ((0, 0, 0), (1_000_000, 5_000, 5_000), (20_000, 70, 130)):
        sleeve = control(entry_per_million=rate)
        entries = sum(bool(sleeve.decide(sleeve.initial(), quote(n), T0, view())[1]) for n in range(5_000))
        assert low <= entries <= high, (rate, entries)


# ---- one round trip, step by step -----------------------------------------------------------------------------------


def test_a_round_trip_step_by_step():
    sleeve = control()
    state, actions = sleeve.decide(sleeve.initial(), quote(1), T0, view())
    (entry,) = actions
    assert isinstance(entry, Intent) and (entry.pair, entry.quantity, entry.leg) == ("EUR.USD", 20_000, "entry")
    assert entry.tif is TimeInForce.FOK and entry.limit_price is None, "a market order, all or nothing"
    assert (state[0].phase, state[0].tried_ns) == (ENTERING, T0) and state[1] == PairState(), "the other is untouched"

    state, actions = sleeve.decide(state, Placed(entry, an_id()), T0, view())
    assert state[0].order_id == an_id() and not actions
    state, actions = sleeve.decide(state, quote(2, T0 + 10 * MS), T0 + 10 * MS, view())
    assert not actions, "one entry at a time: it does not enter again while the first is on its way"

    long = view(EUR_USD=entry.side.sign * 20_000)
    state, actions = sleeve.decide(state, a_fill(entry.side, an_id(), T0 + 50 * MS), T0 + 60 * MS, long)
    assert (state[0].phase, state[0].since_ns, state[0].order_id) == (HOLDING, T0 + 50 * MS, None)
    assert not actions, "held from the venue's time of the fill, not from when the report came"
    assert state[0].tried_ns is None, "the entry did what it was sent to do: nothing to wait for"

    done = OrderEvent(an_id(), OrderState.FILLED, T0 + 60 * MS, 4, 20_000)
    assert sleeve.decide(state, done, T0 + 60 * MS, long) == (state, ()), "the order's own FILLED changes nothing more"
    early = T0 + 50 * MS + 2 * SECOND - 1
    assert sleeve.decide(state, quote(3, early), early, long) == (state, ()), "a nanosecond before the holding time"

    due = early + 1
    state, actions = sleeve.decide(state, quote(4, due), due, long)
    (leave,) = actions
    assert (leave.side, leave.quantity, leave.leg, leave.attempt) == (entry.side.opposite, 20_000, "exit", 0)
    assert state[0].phase == EXITING

    state, _ = sleeve.decide(state, Placed(leave, an_id(2)), due, long)
    state, actions = sleeve.decide(state, a_fill(leave.side, an_id(2), due + 5 * MS), due + 9 * MS, view(EUR_USD=0))
    assert state[0] == PairState(FLAT, trips=1) and not actions


def test_the_exit_is_for_what_the_book_says_it_holds_not_for_what_it_remembers_ordering():
    sleeve = control()
    state = (PairState(HOLDING, since_ns=T0), PairState())
    _, (leave,) = sleeve.decide(state, quote(1, T0 + 3 * SECOND), T0 + 3 * SECOND, view(EUR_USD=-37_000))
    assert (leave.side, leave.quantity) == (Side.BUY, 37_000)


def test_a_quiet_book_can_still_be_left_on_a_heartbeat_but_is_never_entered_on_one():
    sleeve = control()
    beat = Heartbeat("EUR.USD", T0 + 3 * SECOND, T0 + 3 * SECOND, 0)
    assert sleeve.decide(sleeve.initial(), beat, beat.recv_ts_ns, view()) == (sleeve.initial(), ())
    state = (PairState(HOLDING, since_ns=T0), PairState())
    _, (leave,) = sleeve.decide(state, beat, beat.recv_ts_ns, view(EUR_USD=20_000))
    assert (leave.side, leave.leg) == (Side.SELL, "exit")


@pytest.mark.parametrize("how", ["cancelled", "rejected", "refused"])
def test_an_entry_that_comes_to_nothing_leaves_the_pair_flat(how):
    sleeve = control()
    state, (entry,) = sleeve.decide(sleeve.initial(), quote(1), T0, view())
    if how == "refused":
        state, actions = sleeve.decide(state, Refused(entry, "no"), T0, view())
    else:
        state, _ = sleeve.decide(state, Placed(entry, an_id()), T0, view())
        dead = OrderState.CANCELLED if how == "cancelled" else OrderState.REJECTED
        state, actions = sleeve.decide(state, OrderEvent(an_id(), dead, T0 + MS, 3), T0 + MS, view())
    assert state[0].phase == FLAT and state[0].order_id is None and not actions
    again = T0 + SECOND
    assert sleeve.decide(state, quote(2, again - 1), again - 1, view()) == (state, ()), "not again at once"
    assert sleeve.decide(state, quote(3, again), again, view())[1], "it may enter again when the wait is over"


def test_an_exit_that_comes_to_nothing_is_sent_again_as_a_new_attempt():
    sleeve, long = control(), view(EUR_USD=20_000)
    state = (PairState(HOLDING, since_ns=T0), PairState())
    at = T0 + 3 * SECOND
    state, (first,) = sleeve.decide(state, quote(1, at), at, long)
    state, _ = sleeve.decide(state, Placed(first, an_id(1)), at, long)
    assert sleeve.decide(state, quote(2, at + MS), at + MS, long)[1] == (), "one exit at a time"
    stranger = OrderEvent(an_id(99), OrderState.CANCELLED, at + MS, 2)
    assert sleeve.decide(state, stranger, at + MS, long) == (state, ()), "another order's end is not this one's"
    state, _ = sleeve.decide(state, OrderEvent(an_id(1), OrderState.CANCELLED, at + MS, 3), at + MS, long)
    assert (state[0].phase, state[0].tried_ns) == (HOLDING, at), "held again, and it remembers when it tried"
    for early in (at + 2 * MS, at + SECOND - 1):
        beat = Heartbeat("EUR.USD", early, early, 0)
        assert sleeve.decide(state, quote(3, early), early, long) == (state, ()), "not on the very next quote"
        assert sleeve.decide(state, beat, early, long) == (state, ()), "nor on a heartbeat"
    state, (second,) = sleeve.decide(state, quote(4, at + SECOND), at + SECOND, long)
    assert (first.attempt, second.attempt) == (0, 1) and second.side is Side.SELL
    assert state[0].tried_ns == at + SECOND, "the wait is counted from the attempt, send to send"


def test_orders_that_keep_coming_to_nothing_are_sent_once_per_wait_and_not_once_per_quote():
    sleeve, long = control(retry_ns=30 * SECOND), view(EUR_USD=20_000)
    holding = (PairState(HOLDING, since_ns=T0 - 3 * SECOND), PairState())
    for start, held in ((holding, long), (sleeve.initial(), view())):
        state, sent = start, []
        for number in range(6_000):  # ten minutes of quotes, 100 ms apart, and nothing sent in them is ever taken
            at = T0 + number * 100 * MS
            state, actions = sleeve.decide(state, quote(number + 1, at), at, held)
            for intent in actions:
                sent.append((at, intent.leg, intent.attempt))
                state, _ = sleeve.decide(state, Refused(intent, "no"), at, held)
        assert [at for at, _, _ in sent] == [T0 + n * 30 * SECOND for n in range(20)], "600 s / 30 s, not 6,000"
        if held is long:
            assert [(leg, attempt) for _, leg, attempt in sent] == [("exit", n) for n in range(20)]


def test_the_wait_follows_a_failure_only_so_a_filled_entry_is_left_on_time():
    sleeve = control(hold_ns=SECOND, retry_ns=30 * SECOND)
    state, (entry,) = sleeve.decide(sleeve.initial(), quote(1), T0, view())
    state, _ = sleeve.decide(state, Placed(entry, an_id()), T0, view())
    long = view(EUR_USD=entry.side.sign * 20_000)
    state, _ = sleeve.decide(state, a_fill(entry.side, an_id(), T0 + 50 * MS), T0 + 60 * MS, long)
    due = T0 + 50 * MS + SECOND
    state, (leave,) = sleeve.decide(state, quote(2, due), due, long)
    assert leave.leg == "exit", "one second after the fill, although the entry was sent less than thirty ago"
    state, _ = sleeve.decide(state, a_fill(leave.side, an_id(2), due + 5 * MS), due + 9 * MS, view())
    assert state[0] == PairState(FLAT, trips=1), "and a completed round trip leaves nothing to wait for either"
    assert sleeve.decide(state, quote(3, due + 10 * MS), due + 10 * MS, view())[1]


def test_pairs_are_independent_and_a_pair_it_was_not_given_is_ignored():
    sleeve = control()
    state, (first,) = sleeve.decide(sleeve.initial(), quote(1), T0, view())
    state, (second,) = sleeve.decide(state, quote(2, pair="USD.JPY"), T0, view())
    assert (first.pair, second.pair) == ("EUR.USD", "USD.JPY") and [s.phase for s in state] == [ENTERING, ENTERING]
    assert sleeve.decide(state, quote(3, pair="GBP.USD"), T0, view()) == (state, ())
    stray = Fill("X1", an_id(), "GBP.USD", Side.BUY, 1, 1, T0)
    assert sleeve.decide(state, stray, T0, view()) == (state, ())
    assert sleeve.decide(state, Refused(Cancel(an_id()), "no"), T0, view()) == (state, ())


def test_settings_that_make_no_sense_are_refused():
    bad_settings = ({"sizes": {}}, {"sizes": {"EUR.USD": 0}}, {"entry_per_million": 1_000_001}, {"hold_ns": -1})
    for bad in (*bad_settings, {"retry_ns": -1}):
        with pytest.raises(ValueError):
            control(**bad)
    with pytest.raises(ValueError, match="sleeve name"):
        ControlSleeve("Not A Name", seed=1, sizes=SIZES, entry_per_million=1, hold_ns=1, retry_ns=1)
    with pytest.raises(TypeError, match="retry_ns"):
        ControlSleeve("control", seed=1, sizes=SIZES, entry_per_million=1, hold_ns=1)  # never a silent default
    for bad in ({"quantity": 0}, {"pair": "EURUSD"}, {"limit_price": 0}, {"attempt": -1}, {"leg": "two words"}):
        with pytest.raises((ValueError, TypeError)):
            Intent(**{"pair": "EUR.USD", "side": Side.BUY, "quantity": 1, **bad})


# ---- pure: the same inputs give the same outputs, and nothing else changes ------------------------------------------

STEP = st.tuples(st.integers(1, 10**6), st.integers(0, 5_000), st.sampled_from([-20_000, 0, 20_000]))
STEPS = st.lists(STEP, min_size=1, max_size=40)


@given(steps=STEPS, seed=st.integers(0, 50))
def test_a_decision_is_a_function_of_its_arguments_and_leaves_the_sleeve_as_it_was(steps, seed):
    sleeve = control(seed=seed, entry_per_million=300_000)
    before = copy.deepcopy(vars(sleeve))
    state = sleeve.initial()
    for seq, later_ms, held in steps:
        at = T0 + later_ms * MS
        arguments = (state, quote(seq, at), at, view(EUR_USD=held))
        first, second = sleeve.decide(*arguments), sleeve.decide(*arguments)
        assert first == second, "asked twice, it answers the same"
        assert arguments[0] == state and hash(state) == hash(arguments[0]), "the state it was given is a value"
        state = first[0]
    assert vars(sleeve) == before, "the sleeve object holds settings only: deciding does not change it"
    again = control(seed=seed, entry_per_million=300_000)
    replayed = again.initial()
    for seq, later_ms, held in steps:
        replayed, _ = again.decide(replayed, quote(seq, T0 + later_ms * MS), T0 + later_ms * MS, view(EUR_USD=held))
    assert replayed == state, "a second sleeve with the same seed, shown the same events, ends in the same state"


OUTSIDE_WORLD = {"random", "time", "datetime", "os", "pathlib", "socket", "asyncio", "subprocess"}
NOT_A_SLEEVES_BUSINESS = {"place", "cancel", "open", "now_ns", "mark_reconciled"}


def test_a_sleeve_is_handed_no_venue_no_clock_no_file_and_no_generator():
    seen = 0
    for rel, tree in sources():
        if not rel.startswith("src/hftlab/strategy/"):
            continue
        seen += 1
        for _, name in imports(tree, rel):
            top = name.split(".")[0]
            assert top not in OUTSIDE_WORLD, (rel, name)
            if name.startswith("hftlab.venues"):
                assert name.startswith("hftlab.venues.port"), f"{rel} imports {name}: a sleeve sees events, not venues"
            assert not name.startswith(("hftlab.shadow", "hftlab.replay", "hftlab.safety")), (rel, name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in NOT_A_SLEEVES_BUSINESS, (rel, node.lineno)
            assert not isinstance(node, ast.Global | ast.Nonlocal), (rel, node.lineno)
    assert seen == 3
