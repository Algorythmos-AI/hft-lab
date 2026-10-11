"""A sleeve's state, written down and read back: exactly the state, and so exactly the decisions (ADR 0026).

The test that matters cuts a session. A sleeve is folded over a stream; at a chosen event its state is saved,
turned into the text a file holds, read back by a NEW sleeve built from the same settings, and the fold goes
on with that one. Whatever the cut, every decision after it, and the state at the end, must be the uncut
run's. The cut is made at every event of the stream in turn, with orders on their way and without.

The world a sleeve trades in here is a small one that answers by rule: an order is filled at the next event,
except the ones a hash says came to nothing. It is the same world on both sides of a cut.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from types import MappingProxyType

import pytest

from hftlab.book import SleeveView
from hftlab.calendar import Calendar
from hftlab.core import Fill, OrderEvent, OrderState, Position, Quote, Side, client_order_id
from hftlab.journal import canonical
from hftlab.models import scorer_for
from hftlab.models.baseline import number
from hftlab.record.format import Heartbeat
from hftlab.strategy import ControlSleeve, IdleSleeve, Intent, MlSleeve, Placed, Refused, Saves, StateError
from hftlab.strategy.dc import CONTRARIAN, DcSleeve
from tests.learn.kit import features
from tests.strategy.dc_kit import dc, quotes, zigzag
from tests.strategy.test_ml import MARGIN, the_map, trained
from tests.venues.kit import MS, SECOND, T0

SIZES = {"EUR.USD": 20_000, "USD.JPY": 20_000}


class World:
    """What answers a sleeve: its orders are filled one event later, refused or cancelled, by a rule of its own.

    It keeps the sleeve's position and the orders on their way. It is not part of the sleeve and is not cut.
    """

    def __init__(self, name: str, seed: int = 5) -> None:
        self.name, self.seed = name, seed
        self.held: dict[str, int] = {}
        self.pending: list[tuple[str, Intent]] = []
        self.log: list[tuple] = []

    def view(self) -> SleeveView:
        positions = {pair: Position(pair, quantity) for pair, quantity in self.held.items() if quantity}
        return SleeveView(self.name, MappingProxyType(positions), MappingProxyType({}), MappingProxyType({}))

    def news(self, k: int, now: int) -> list:
        """What became of the orders sent on the event before: a fill, or word that the order came to nothing."""
        out = []
        for order_id, intent in self.pending:
            if number(self.seed, k, 1) % 4 == 0:
                out.append(OrderEvent(order_id, OrderState.CANCELLED, now, k))
                continue
            self.held[intent.pair] = self.held.get(intent.pair, 0) + intent.side.sign * intent.quantity
            price = 108_510 if intent.side is Side.BUY else 108_500
            out.append(Fill(f"W.{k}", order_id, intent.pair, intent.side, intent.quantity, price, now, venue_ts_ns=now))
            out.append(OrderEvent(order_id, OrderState.FILLED, now, k, intent.quantity))
        self.pending = []
        return out

    def take(self, k: int, intent: Intent) -> Placed | Refused:
        if number(self.seed, k, 2) % 7 == 0:
            return Refused(intent, "the world would not take it")
        order_id = client_order_id(
            sleeve=self.name, pair=intent.pair, side=intent.side, decision_seq=k, leg=intent.leg, attempt=intent.attempt
        )
        self.pending.append((order_id, intent))
        return Placed(intent, order_id)


def run(make, stream, cut: int | None = None):
    """Fold a sleeve over the stream. With `cut`, the state goes through a file's text into a new sleeve there.

    Returns everything the sleeve wanted, event by event, the state at the end, and the state at the cut.
    """
    sleeve = make()
    world, state, at_cut = World(sleeve.name), sleeve.initial(), None
    for k, event in enumerate(stream):
        if k == cut:
            text = canonical(sleeve.save(state))
            assert "." not in text.replace("EUR.USD", "").replace("USD.JPY", ""), "whole numbers and text only"
            sleeve, before = make(), state
            state = at_cut = sleeve.restore(json.loads(text))
            assert state == before, "read back, it is the state that was written"
        now = event.recv_ts_ns
        waiting = [*world.news(k, now), event]
        while waiting:
            state, actions = sleeve.decide(state, waiting.pop(0), now, world.view())
            for action in actions:
                world.log.append((k, action))
                waiting.append(world.take(k, action))
    return world.log, state, at_cut


def every_cut_agrees(make, stream, cuts, least: int = 20) -> int:
    """The uncut run against the run cut at each of `cuts`. Returns how many cuts found an order on its way."""
    log, final, _ = run(make, stream)
    assert len(log) >= least, "a stream on which the sleeve trades, or the test shows nothing"
    in_flight = 0
    for cut in cuts:
        cut_log, cut_final, at_cut = run(make, stream, cut)
        assert cut_log == log and cut_final == final, f"cut at event {cut}"
        in_flight += "ing" in json.dumps(make().save(at_cut))  # entering or exiting: an order was on its way
    return in_flight


# ---- the three sleeves ---------------------------------------------------------------------------------------------


def a_control(**changes) -> ControlSleeve:
    settings = {"seed": 7, "sizes": SIZES, "entry_per_million": 300_000, "hold_ns": SECOND, "retry_ns": 300 * MS}
    return ControlSleeve("control", **{**settings, **changes})


def two_pairs(count: int) -> list:
    """Two books a tenth of a second apart in turn, with a heartbeat now and then."""
    out = []
    for n in range(count):
        at = T0 + n * 100 * MS
        pair, bid = ("EUR.USD", 108_500) if n % 2 else ("USD.JPY", 151_230)
        still = Heartbeat(pair, at, at, 0)
        out.append(still if n % 11 == 0 else Quote(pair, bid, bid + 10, 10**6, 10**6 + n, at, n + 1))
    return out


def test_a_control_sleeve_cut_at_any_event_decides_as_the_one_that_was_never_cut():
    stream = two_pairs(400)
    assert every_cut_agrees(a_control, stream, range(len(stream))) > 50, "and many cuts fell on an order on its way"


def dc_stream() -> list[Quote]:
    """Six legs of a zig-zag, then nothing for a minute (a gap: the machines warm up again), then six more."""
    first = quotes(zigzag(6))
    return [*first, *quotes(zigzag(6), start_ns=first[-1].recv_ts_ns + 60 * SECOND)]


@pytest.mark.parametrize(
    "changes",
    [{}, {"variant": CONTRARIAN, "overshoot_multiple": Fraction(1, 2)}, {"max_hold_ns": 2 * SECOND, "warm_up": 0}],
    ids=["trend", "contrarian", "short holds"],
)
def test_a_directional_change_sleeve_cut_at_any_event_decides_as_the_one_that_was_never_cut(changes):
    stream = dc_stream()

    def make() -> DcSleeve:
        return dc(**changes)

    assert every_cut_agrees(make, stream, range(0, len(stream), 5), least=8) > 5
    _, final, _ = run(make, stream)
    assert final[0].detector.machines[0].events >= 8 and final[0].trips >= 2, "events on both sides of the gap"


def test_a_restored_directional_change_sleeve_is_not_back_in_its_warm_up_and_a_cold_one_is():
    """What the saved state is for: the same ticks after a restart are traded on, or thrown away as warm-up."""
    stream = quotes(zigzag(8))
    half = len(stream) // 2
    log, _, at_cut = run(dc, stream, half)
    assert at_cut[0].detector.machines[0].warm_left == 0 and [k for k, _ in log if k > half], "it trades after the cut"
    cold = dc()
    state, wanted = cold.initial(), []
    for event in stream[half : half + 130]:
        state, actions = cold.decide(state, event, event.recv_ts_ns, World("dc").view())
        wanted += actions
    assert wanted == [] and state[0].detector.machines[0].warm_left > 0, "a cold start is still warming up there"
    assert [k for k, _ in log if half < k <= half + 130], "where the restored one had already traded"


@pytest.fixture(scope="module")
def planted_model(tmp_path_factory):
    return trained(tmp_path_factory.mktemp("models"), blocks=20)


def test_an_ml_sleeve_cut_at_any_event_decides_as_the_one_that_was_never_cut(planted_model):
    stream, model = planted_model
    feasibility = the_map(stream)

    def make() -> MlSleeve:
        return MlSleeve(
            "ml", scorer=scorer_for(model), features=features(), feasibility=feasibility, margin_micro=MARGIN,
            retry_ns=300 * MS,
        )  # fmt: skip

    shown = stream[:360]
    assert every_cut_agrees(make, shown, range(0, len(shown), 2)) > 5
    cold_log, _, _ = run(make, shown[180:])
    log, _, _ = run(make, shown, 180)
    first = next(k for k, _ in log if k >= 180)
    assert first - 180 < cold_log[0][0], "and it has its history: a cold one waits for it"


# ---- what is refused -------------------------------------------------------------------------------------------------


def sleeves(planted_model):
    stream, model = planted_model
    ml = MlSleeve("ml", scorer=scorer_for(model), features=features(), feasibility=the_map(stream),
                  margin_micro=MARGIN, retry_ns=SECOND)  # fmt: skip
    return {"control": (a_control(), two_pairs(60)), "dc": (dc(), quotes(zigzag(3))), "ml": (ml, stream[:60])}


def a_saved_state(sleeve, stream) -> dict:
    world, state = World(sleeve.name), sleeve.initial()
    for k, event in enumerate(stream):
        waiting = [*world.news(k, event.recv_ts_ns), event]
        while waiting:
            state, actions = sleeve.decide(state, waiting.pop(0), event.recv_ts_ns, world.view())
            waiting += [world.take(k, action) for action in actions]
    return json.loads(canonical(sleeve.save(state)))


def places(value, path=()):
    """Every place in a saved state: the path to each mapping, list and leaf."""
    yield path
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, inner in children:
        yield from places(inner, (*path, key))


def changed(saved, path, new):
    out = copy.deepcopy(saved)
    at = out
    for key in path[:-1]:
        at = at[key]
    at[path[-1]] = new
    return out


@pytest.mark.parametrize("name", ["control", "dc", "ml"])
def test_a_saved_state_damaged_in_any_one_place_is_refused_or_is_exactly_another_state_it_could_have_written(
    planted_model, name
):
    """Never a third thing: no damage is read as something near what was meant, and none raises anything else."""
    sleeve, stream = sleeves(planted_model)[name]
    saved = a_saved_state(sleeve, stream)
    assert sleeve.save(sleeve.restore(saved)) == saved and isinstance(sleeve, Saves)
    refused = accepted = 0
    for path in places(saved):
        if not path:
            continue
        for wrong in (1.5, "x", -1, True, None, [], {}, [1, 2, 3], 10**30):
            damaged = changed(saved, path, wrong)
            if damaged == saved:
                continue
            try:
                state = sleeve.restore(damaged)
            except StateError:
                refused += 1
            else:
                accepted += 1
                assert json.loads(canonical(sleeve.save(state))) == damaged, f"{path} = {wrong!r} was read as another"
    assert refused > 100 and refused > 3 * accepted, (refused, accepted)
    for key in saved:
        with pytest.raises(StateError):
            sleeve.restore({k: v for k, v in saved.items() if k != key})
    with pytest.raises(StateError):
        sleeve.restore({**saved, "also": 1})
    for not_one in (None, [], "text", 7):
        with pytest.raises(StateError):
            sleeve.restore(not_one)


def test_a_state_saved_under_other_settings_is_refused_by_each_sleeve(planted_model):
    stream, model = planted_model
    made = sleeves(planted_model)
    saved = {name: a_saved_state(*made[name]) for name in made}
    others = {
        "control": [a_control(seed=8), a_control(hold_ns=2 * SECOND), a_control(sizes={"EUR.USD": 20_000}),
                    a_control(entry_per_million=1), a_control(retry_ns=SECOND)],
        "dc": [dc(threshold=Fraction(1, 500)), dc(warm_up=3), dc(max_attempts=1), dc(gap_ns=6 * SECOND),
               dc(variant=CONTRARIAN, overshoot_multiple=Fraction(1)), dc(sizes={"EUR.USD": 30_000})],
        "ml": [
            MlSleeve("ml", scorer=scorer_for(model), features=features(), feasibility=the_map(stream),
                     margin_micro=MARGIN + 1, retry_ns=SECOND),
            MlSleeve("ml", scorer=scorer_for(trained_other(model)), features=features(), feasibility=the_map(stream),
                     margin_micro=MARGIN, retry_ns=SECOND),
            MlSleeve("ml", scorer=scorer_for(model), features=features(), feasibility={}, margin_micro=MARGIN,
                     retry_ns=SECOND),
        ],
    }  # fmt: skip
    for name, different in others.items():
        for other in different:
            with pytest.raises(StateError, match="other settings"):
                other.restore(saved[name])
    for name, (sleeve, _) in made.items():
        for another in set(made) - {name}:
            with pytest.raises(StateError, match="is not a version 1 state of"):
                sleeve.restore(saved[another])
        with pytest.raises(StateError, match="is not a version 1 state of"):
            sleeve.restore({**saved[name], "version": 2})


def trained_other(model):
    """The same model fitted to another dataset: another hash, and so another model as far as a saved state goes."""
    import dataclasses

    return dataclasses.replace(model, dataset_hash="0" * 64)


def test_the_idle_sleeve_keeps_nothing_and_says_so_by_having_no_way_to_save():
    assert not isinstance(IdleSleeve(), Saves), "saving is optional: a sleeve without it starts cold, as before"
    assert all(isinstance(sleeve, Saves) for sleeve in (a_control(), dc()))


def test_the_feature_history_is_read_back_only_whole_and_in_order(planted_model):
    stream, _ = planted_model
    builder, state = features(), features().initial()
    for quote in stream[:40]:
        state, _ = builder.step(state, quote, quote.recv_ts_ns)
    saved = json.loads(canonical(builder.dump(state)))
    assert builder.load(saved) == state and len(state.times) > 10
    for damage in (
        {"times": saved["times"][::-1]}, {"times": saved["times"][1:]}, {"top": saved["top"][:3]}, {"day": [1]},
        {"n": -1}, {"mids": None}, {"times": [1.0] * len(saved["times"])}, {"changed_ns": "soon"},
    ):  # fmt: skip
        with pytest.raises(ValueError, match="saved feature state"):
            builder.load({**saved, **damage})
    assert builder.load({**saved, "top": None, "changed_ns": None}).top is None, "an empty book is a state it has"
    assert Calendar() is not None
