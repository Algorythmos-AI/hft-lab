"""The ML sleeve: a registered model's score turned into intents, in shadow, replayable, no better than its model."""
from __future__ import annotations

import dataclasses
from fractions import Fraction
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.book import SleeveView
from hftlab.calendar import Calendar
from hftlab.core import Fill, OrderEvent, OrderState, Position, Quote, Side, client_order_id
from hftlab.feasibility.map import Settings, feasibility_map
from hftlab.features import FeatureRow
from hftlab.learn.dataset import build
from hftlab.learn.train import flat_model, ridge_model
from hftlab.models import Decision, ModelRegistry, scorer_for
from hftlab.strategy import ControlSleeve, Intent, MlSleeve, Placed, Refused
from hftlab.strategy.ml import ENTERING, EXITING, FLAT, HOLDING, viable_cell
from tests.learn.kit import CONFIG, COST, HORIZON, SIZE, WIN, dataset, features, labels, planted
from tests.shadow.kit import session
from tests.venues.kit import EURUSD, MS, SECOND, T0, plain_costs

MARGIN = 5_000_000
RETRY = 2 * SECOND


def the_map(stream, costs=None, **changes) -> dict:
    """A real feasibility map of the stream: one-second windows, sampled every tenth of a second."""
    rule = {"horizons_ns": (HORIZON,), "step_ns": 100 * MS, "upper_quantile_ppm": 900_000, "min_windows": 100,
            "min_share_ppm": 200_000, "placeholder": True}  # fmt: skip
    settings = Settings(**{**rule, **changes})
    return feasibility_map(stream, {"EUR.USD": EURUSD}, costs or plain_costs(), Calendar(), settings)


def trained(tmp_path, seed: int = 1, blocks: int = 40, costs=None):
    """Fit a ridge model to a stream with the planted effect, keep it in a registry, and load it back by its hash."""
    stream = planted(seed, blocks)
    made = build(stream, features(), labels(costs=costs))
    registry = ModelRegistry(tmp_path / "models")
    digest = registry.put(ridge_model(made, range(len(made.rows)), Fraction(1, 1000)))
    model = registry.load(digest, pair="EUR.USD", features=features().keys, label=made.label)
    return stream, model


def sleeve(model, feasibility, **changes) -> MlSleeve:
    settings = {"scorer": scorer_for(model), "features": features(), "feasibility": feasibility,
                "margin_micro": MARGIN, "retry_ns": RETRY}  # fmt: skip
    return MlSleeve("ml", **{**settings, **changes})


def net(result: dict, name: str = "ml") -> int:
    return result["sleeves"][name]["pairs"]["EUR.USD"]["net_micro"]


# ---- in the shadow runner --------------------------------------------------------------------------------------------


def test_it_trades_the_planted_effect_in_shadow_and_earns_exactly_what_the_effect_is_worth(tmp_path):
    train, model = trained(tmp_path)
    unseen = planted(2, 30)
    result, entries, _ = session(tmp_path / "j.jsonl", [sleeve(model, the_map(train))], unseen)
    counts = result["sleeves"]["ml"]
    assert counts["decisions"] == counts["orders"] == counts["fills"] == 2 * 29 and counts["refused"] == 0, (
        "one round trip for every block but the first, which comes before the features have their history"
    )
    assert net(result) == 29 * WIN > 0 and result["sleeves"]["ml"]["pairs"]["EUR.USD"]["quantity"] == 0
    decided = [e for e in entries if e["type"] == "decision"]
    assert {(e["leg"], e["quantity"], e["tif"]) for e in decided} == {("entry", SIZE, "FOK"), ("exit", SIZE, "FOK")}
    entered = {e["at_ns"]: e["side"] for e in decided if e["leg"] == "entry"}
    by_time = {q.recv_ts_ns: q for q in unseen}
    assert all((side == "BUY") == (by_time[at].bid_size > by_time[at].ask_size) for at, side in entered.items())
    left = sorted(e["at_ns"] for e in decided if e["leg"] == "exit")
    assert [out - at for at, out in zip(sorted(entered), left, strict=True)] == [HORIZON] * 29, "at the horizon"


def test_a_replay_of_the_same_stream_writes_the_same_journal_byte_for_byte(tmp_path):
    train, model = trained(tmp_path)
    unseen = planted(2, 30)
    first, again = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    session(first, [sleeve(model, the_map(train))], unseen)
    reloaded = ModelRegistry(tmp_path / "models").load(model.content_hash, pair="EUR.USD", features=features().keys,
                                                       label=model.label)  # fmt: skip
    session(again, [sleeve(reloaded, the_map(train))], list(unseen))
    assert first.read_bytes() == again.read_bytes() and len(first.read_bytes()) > 10_000
    assert b"." not in first.read_bytes().replace(b"EUR.USD", b"").replace(b"USD.JPY", b"").replace(b"S0000.", b""), (
        "and no float reached the journal"
    )


def test_where_there_is_no_effect_it_pays_the_cost_of_trading_exactly_as_the_control_sleeve_does(tmp_path):
    train, model = trained(tmp_path)
    no_effect = planted(2, 30, effect=False)  # the same one-sided books, and a price that never follows them
    control = ControlSleeve("control", seed=5, sizes={"EUR.USD": SIZE}, entry_per_million=30_000, hold_ns=HORIZON,
                            retry_ns=RETRY)  # fmt: skip
    result, _, _ = session(tmp_path / "j.jsonl", [sleeve(model, the_map(train)), control], no_effect)
    trips, control_trips = (result["sleeves"][name]["fills"] // 2 for name in ("ml", "control"))
    assert trips == 29 and control_trips > 5
    assert net(result) == -trips * COST, "every trade it was led into cost a round trip, to the unit"
    assert net(result, "control") == -control_trips * COST
    assert net(result) * control_trips == net(result, "control") * trips, "per trip, the control's cost: not better"


def test_it_never_enters_on_a_cell_the_feasibility_map_does_not_mark_viable(tmp_path):
    train, model = trained(tmp_path)
    still = the_map(planted(2, 30, effect=False))
    assert still["viable_cells"] == [] and the_map(train)["viable_cells"] == [["EUR.USD", HORIZON]]
    for feasibility in (still, {}, {**the_map(train), "viable_cells": [["EUR.USD", 2 * HORIZON], ["USD.JPY", HORIZON]]},
                        {**the_map(train), "kind": "something-else"}):  # fmt: skip
        made = sleeve(model, feasibility)
        result, _, _ = session(tmp_path / f"{len(feasibility)}.jsonl", [made], planted(2, 30))
        assert result["sleeves"]["ml"]["decisions"] == 0 and net(result) == 0 and not made.viable
        assert any("does not mark EUR.USD" in reason for reason in made.shadow_only)
    assert viable_cell(the_map(train), "EUR.USD", HORIZON) and not viable_cell(the_map(train), "EUR.USD", HORIZON + 1)


def test_it_trades_only_when_the_expected_outcome_is_above_its_margin(tmp_path):
    train, model = trained(tmp_path)
    for margin, trips in ((0, 29), (WIN - 100_000, 29), (WIN + 100_000, 0), (10 * WIN, 0)):
        made = sleeve(model, the_map(train), margin_micro=margin)
        result, _, _ = session(tmp_path / f"{margin}.jsonl", [made], planted(2, 30))
        assert result["sleeves"]["ml"]["fills"] == 2 * trips, margin
    flat = sleeve(flat_model(dataset(train)), the_map(train), margin_micro=0)
    assert session(tmp_path / "flat.jsonl", [flat], planted(2, 30))[0]["sleeves"]["ml"]["decisions"] == 0


# ---- what it may not do ----------------------------------------------------------------------------------------------


def test_on_placeholder_costs_or_a_placeholder_map_it_reports_that_it_may_not_leave_shadow(tmp_path):
    train, model = trained(tmp_path)
    made = sleeve(model, the_map(train))
    assert made.may_leave_shadow is False and made.shadow_only == (
        "the model was fitted to outcomes priced on a placeholder cost model",
        "the feasibility map was computed on placeholder costs or a placeholder rule",
    )
    measured = plain_costs(placeholder=False)
    _, real = trained(tmp_path / "measured", costs=measured)
    decided = the_map(train, measured, placeholder=False)
    assert decided["placeholder"] is False and real.placeholder is False
    assert sleeve(real, decided).may_leave_shadow is True and sleeve(real, decided).shadow_only == ()
    assert sleeve(real, the_map(train, measured)).shadow_only == (made.shadow_only[1],), "a rule nobody decided"
    assert sleeve(real, the_map(train)).shadow_only == (made.shadow_only[1],), "a map on placeholder costs"
    assert sleeve(model, decided).shadow_only == (made.shadow_only[0],)
    assert sleeve(real, {**decided, "placeholder": None}).may_leave_shadow is False, "anything but a plain no is a yes"
    assert not hasattr(made, "stage") and not hasattr(made, "promote"), "it reports; stages are the trader's"


def test_it_refuses_features_other_than_the_ones_its_model_was_trained_on(tmp_path):
    train, model = trained(tmp_path)
    other = features(dataclasses.replace(CONFIG, rate_window_ns=2 * SECOND))
    with pytest.raises(ValueError, match="not the ones its model was trained on"):
        sleeve(model, the_map(train), features=other)
    for wrong in ({"margin_micro": -1}, {"retry_ns": -1}):
        with pytest.raises(ValueError):
            sleeve(model, the_map(train), **wrong)
    with pytest.raises(ValueError, match="not a sleeve name"):
        MlSleeve("ML sleeve", scorer=scorer_for(model), features=features(), feasibility={}, margin_micro=0, retry_ns=0)


# ---- one decision at a time ------------------------------------------------------------------------------------------


class Always:
    """A scorer that wants the same thing on every row."""

    def __init__(self, model, side: Side = Side.BUY, expected: int = 9_000_000) -> None:
        self.model, self.side, self.expected, self.asked = model, side, expected, []

    def score(self, row: FeatureRow) -> Decision | None:
        self.asked.append(row)
        return Decision(self.side, self.expected, "stub")


def view(quantity: int = 0) -> SleeveView:
    positions = {"EUR.USD": Position("EUR.USD", quantity)} if quantity else {}
    return SleeveView("ml", MappingProxyType(positions), MappingProxyType({}), MappingProxyType({}))


def quote(at_ms: int, pair: str = "EUR.USD") -> Quote:
    return Quote(pair, 108_500, 108_510, 10**6, 10**6, T0 + at_ms * MS, at_ms + 1)


VIABLE = {"kind": "feasibility-map", "placeholder": True, "viable_cells": [["EUR.USD", HORIZON]]}


def warmed(tmp_path, **changes):
    """A sleeve with a stub scorer, shown three seconds of quotes it may not act on yet, and its state after them."""
    _, model = trained(tmp_path, blocks=6)
    made = sleeve(model, VIABLE, scorer=Always(model, **changes))
    state = made.initial()
    for at in range(0, 2_000, 100):
        state, actions = made.decide(state, quote(at), T0 + at * MS, view())
        assert actions == () and state.phase == FLAT
    return made, state


def test_it_enters_on_the_first_row_its_features_complete_and_leaves_one_horizon_after_deciding(tmp_path):
    made, state = warmed(tmp_path)
    assert made._scorer.asked == [], "no row yet: the history is not two seconds long"
    state, actions = made.decide(state, quote(2_000), T0 + 2 * SECOND, view())
    assert list(actions) == [Intent("EUR.USD", Side.BUY, SIZE, "entry", expected_micro=9_000_000)], (
        "with what the model expected of it, for the journal: nothing routes on it"
    )
    assert state.phase == ENTERING
    assert (state.decided_ns, state.tried_ns) == (T0 + 2 * SECOND, T0 + 2 * SECOND) and len(made._scorer.asked) == 1
    order_id = client_order_id(sleeve="ml", pair="EUR.USD", side=Side.BUY, decision_seq=21)
    state, _ = made.decide(state, Placed(actions[0], order_id), T0 + 2 * SECOND, view())
    assert state.order_id == order_id
    fill = Fill("S.1", order_id, "EUR.USD", Side.BUY, SIZE, 108_510, T0 + 2_100 * MS, venue_ts_ns=T0 + 2_005 * MS)
    state, actions = made.decide(state, fill, T0 + 2_100 * MS, view(SIZE))
    assert (state.phase, state.order_id, state.tried_ns, tuple(actions)) == (HOLDING, None, None, ())
    for at in (2_100, 2_500, 2_999):
        state, actions = made.decide(state, quote(at), T0 + at * MS, view(SIZE))
        assert actions == () and state.phase == HOLDING, "held, and not asked again while it holds"
    state, actions = made.decide(state, quote(3_000), T0 + 3 * SECOND, view(SIZE - 5_000))
    assert list(actions) == [Intent("EUR.USD", Side.SELL, SIZE - 5_000, "exit", attempt=0)], "what the book holds"
    assert state.phase == EXITING and state.exits == 1 and len(made._scorer.asked) == 1
    state, _ = made.decide(state, dataclasses.replace(fill, side=Side.SELL), T0 + 3_100 * MS, view(0))
    assert (state.phase, state.trips, state.exits, state.tried_ns) == (FLAT, 1, 0, None)


def test_an_order_that_came_to_nothing_is_not_sent_again_before_the_waiting_time(tmp_path):
    made, state = warmed(tmp_path)
    state, actions = made.decide(state, quote(2_000), T0 + 2 * SECOND, view())
    state, _ = made.decide(state, Refused(actions[0], "not immediately executable"), T0 + 2 * SECOND, view())
    assert state.phase == FLAT and state.tried_ns == T0 + 2 * SECOND
    for at in (2_100, 3_000, 3_999):
        state, actions = made.decide(state, quote(at), T0 + at * MS, view())
        assert actions == ()
    state, actions = made.decide(state, quote(4_000), T0 + 4 * SECOND, view())
    assert [a.leg for a in actions] == ["entry"], "after the waiting time it may try again"
    order_id = client_order_id(sleeve="ml", pair="EUR.USD", side=Side.BUY, decision_seq=44)
    state, _ = made.decide(state, Placed(actions[0], order_id), T0 + 4 * SECOND, view())
    dead = OrderEvent(order_id, OrderState.CANCELLED, T0 + 4_100 * MS, 1, 0, None, "no fill")
    state, _ = made.decide(state, dead, T0 + 4_100 * MS, view())
    assert (state.phase, state.order_id) == (FLAT, None)
    other = dataclasses.replace(dead, client_order_id=order_id[:-1] + ("0" if order_id[-1] != "0" else "1"))
    assert made.decide(state, other, T0 + 4_100 * MS, view())[0] == state, "not the order it awaits"


def test_an_exit_that_came_to_nothing_is_tried_again_under_a_new_attempt_number(tmp_path):
    made, state = warmed(tmp_path, side=Side.SELL)
    state, actions = made.decide(state, quote(2_000), T0 + 2 * SECOND, view())
    assert actions[0].side is Side.SELL
    fill = Fill("S.1", "hft-ml-0", "EUR.USD", Side.SELL, SIZE, 108_500, T0 + 2_100 * MS, venue_ts_ns=T0 + 2_005 * MS)
    state, _ = made.decide(state, fill, T0 + 2_100 * MS, view(-SIZE))
    state, first = made.decide(state, quote(3_000), T0 + 3 * SECOND, view(-SIZE))
    state, _ = made.decide(state, Refused(first[0], "inside the rollover window"), T0 + 3 * SECOND, view(-SIZE))
    assert state.phase == HOLDING and made.decide(state, quote(4_900), T0 + 4_900 * MS, view(-SIZE))[1] == ()
    state, second = made.decide(state, quote(5_000), T0 + 5 * SECOND, view(-SIZE))
    assert [(a.side, a.quantity, a.leg, a.attempt) for a in (*first, *second)] == [
        (Side.BUY, SIZE, "exit", 0), (Side.BUY, SIZE, "exit", 1),
    ]  # fmt: skip


def test_it_asks_nothing_about_another_pair_and_news_of_another_pair_changes_nothing_but_its_features(tmp_path):
    made, state = warmed(tmp_path)
    after, actions = made.decide(state, quote(2_000, pair="USD.JPY"), T0 + 2 * SECOND, view())
    assert actions == () and made._scorer.asked == [] and after.features.n == state.features.n + 1
    assert dataclasses.replace(after, features=state.features) == state


@given(st.lists(st.tuples(st.integers(1, 400), st.sampled_from([0, 0, SIZE, -SIZE])), max_size=40))
def test_a_decision_changes_nothing_but_the_state_it_returns(tmp_path_factory, steps):
    made, state = warmed(tmp_path_factory.mktemp("ml"))
    before = {name: value for name, value in vars(made).items() if name != "_scorer"}
    at, replayed, twin = 2_000, state, sleeve(made._scorer.model, VIABLE, scorer=Always(made._scorer.model))
    for wait, held in steps:
        at += wait
        event = quote(at)
        handed = state
        state, actions = made.decide(state, event, T0 + at * MS, view(held))
        assert made.decide(handed, event, T0 + at * MS, view(held)) == (state, actions), "the same call, the same"
        replayed, same = twin.decide(replayed, event, T0 + at * MS, view(held))
        assert (replayed, same) == (state, actions), "a second sleeve shown the same events ends in the same state"
    assert {name: value for name, value in vars(made).items() if name != "_scorer"} == before
