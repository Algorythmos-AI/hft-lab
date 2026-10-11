"""The shipped trader settings hold a place for a champion and for three challengers, one for each model kind.

The registered shadow weeks name up to three challengers side by side (HFT-EXP-0003, HFT-EXP-0004,
HFT-EXP-0006). The models here are written by hand, whole number by whole number: nothing is fitted, so no
library is needed and the answer of every one of them can be said beforehand.
"""
from __future__ import annotations

import dataclasses
from fractions import Fraction
from pathlib import Path

import yaml

from hftlab.journal import canonical
from hftlab.learn.train import flat_model, keep_report
from hftlab.lineup import Lineup, Named
from hftlab.lineup.serve import load, places
from hftlab.models import BUY, SELL, Model, ModelRegistry, Net, Ridge, Tree, Trees
from hftlab.models.net import BITS, WEIGHT_BITS
from tests.learn.kit import CONFIG, IMBALANCE, dataset, features, planted
from tests.lineup.kit import SERVING, effect, put
from tests.strategy.test_ml import the_map
from tests.support import ROOT
from tests.trader.kit import Desk

SHIPPED = ROOT / "config" / "trader.yaml"
PLACES = ("ml", "ml-challenger-1", "ml-challenger-2", "ml-challenger-3")
HEAVY = 250_000  # a size imbalance, in millionths: the planted stream shows 500,000 towards the side that will win
PAYS = 5_000_000  # what each model below expects of a one-sided book, in millionths of a dollar
W, ONE = 1 << WEIGHT_BITS, 1 << (BITS + WEIGHT_BITS)


def _only(value: int) -> tuple[int, ...]:
    """A row of ten weights with `value` on the size imbalance and nothing on the rest."""
    return tuple(value if place == IMBALANCE else 0 for place in range(10))


def ridge(scale: int) -> dict[str, object]:
    """A line through the imbalance: `PAYS` at 500,000 towards a side, and a loss of as much on a level book."""
    weights = {BUY: _only(scale), SELL: _only(-scale)}
    lines = {name: tuple(Fraction(weight) for weight in row) for name, row in weights.items()}
    zero = (Fraction(0),) * 10
    return Ridge(Fraction(1), 1, zero, zero, {BUY: Fraction(-PAYS), SELL: Fraction(-PAYS)}, lines).params()


def trees() -> dict[str, object]:
    """One split a side: is the imbalance beyond `HEAVY` towards it?"""
    buy = Tree((IMBALANCE,), (HEAVY,), (~0,), (~1,), (-PAYS * 1000, PAYS * 1000))
    sell = Tree((IMBALANCE,), (-HEAVY - 1,), (~0,), (~1,), (PAYS * 1000, -PAYS * 1000))
    return Trees(1000, 1, 10, {BUY: (buy,), SELL: (sell,)}, {"trees": 1}, "by hand").params()


def net() -> dict[str, object]:
    """Two rows of ten values. Each side is the newer row's imbalance towards it, in units of `HEAVY`, less one."""
    spread = tuple(HEAVY if place == IMBALANCE else 1 for place in range(10))
    return Net(
        window=2, width=10, kernel=1, clip=8, cap=8, centre=(0,) * 10, spread=spread, conv=(_only(W), _only(-W)),
        conv_bias=(0, 0), hidden=((0, 0, W, 0), (0, 0, 0, W)), hidden_bias=(0, 0), out={BUY: (W, 0), SELL: (0, W)},
        out_bias={BUY: -ONE, SELL: -ONE}, target_scale=PAYS, rows=1, settings={"window": 2, "kernel": 1},
        fitted_with="by hand",
    ).params()  # fmt: skip


def keep(registry: Path, kind: str, params: dict[str, object], stream) -> Named:
    """A model file written by hand, and the report that says how it is served, each kept under its hash."""
    base, serving = flat_model(dataset(planted(1, 2))), dict(SERVING)
    if kind == "seq":
        serving["window_rows"] = 2
        base = dataclasses.replace(base, features=features(dataclasses.replace(CONFIG, window_rows=2)).keys)
    model: Model = dataclasses.replace(base, kind=kind, params=params)
    report = {
        "kind": "evaluation-report", "version": 1, "not_a_result": 1, "pair": model.pair,
        "horizon_ns": model.label["horizon_ns"], "model": {"hash": model.content_hash, "kind": kind},
        "serving": {"features": serving, "feasibility": the_map(stream)},
    }  # fmt: skip
    ModelRegistry(registry / "models").put(model)
    return Named(model.content_hash, keep_report(registry / "reports", (canonical(report) + "\n").encode("ascii")))


def test_the_shipped_settings_serve_a_champion_and_three_challengers_of_three_kinds_in_shadow_and_the_day_replays(
    tmp_path,
):
    registry, state = tmp_path / "learn", tmp_path / "trader"
    shipped = {**yaml.safe_load(SHIPPED.read_text()), "registry": str(registry)}  # the one value that is a place
    assert tuple(places(shipped["sleeves"])) == PLACES, "the champion's place and three challengers', in this order"
    stream = planted(1, 40)
    champion = keep(registry, "ridge", ridge(20), stream)
    challengers = (keep(registry, "ridge", ridge(30), stream), keep(registry, "gbm", trees(), stream),
                   keep(registry, "seq", net(), stream))  # fmt: skip
    put(state, Lineup(champion=champion, challengers=challengers))

    made = Desk(state, paper=False, settings=shipped)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()

    named = dict(zip(PLACES, (champion, *challengers), strict=True))
    (start,) = made.of("start")
    assert start["serving"] == {name: one.model for name, one in named.items()} and start["mode"] == "shadow_only"
    assert len(set(start["serving"].values())) == 4, "four models, each in its own place"
    assert [load(one, registry)[0].kind for one in named.values()] == ["ridge", "ridge", "gbm", "seq"]
    assert [made.trader.stages.all()[name] for name in PLACES] == ["shadow"] * 4, "and every place is in shadow"
    assert made.pages.fired == [], "no place was refused its model"
    entries = [e for e in made.of("decision") if e["leg"] == "entry"]
    for name in PLACES:
        mine = [e for e in entries if e["sleeve"] == name]
        assert len(mine) > 15, f"{name} trades the one-sided book it was written to trade"
        assert all(type(e["expected_micro"]) is int and e["expected_micro"] > 0 for e in mine)
    by_place = {name: {e["expected_micro"] for e in entries if e["sleeve"] == name} for name in PLACES}
    assert by_place["ml"] == by_place["ml-challenger-2"] == by_place["ml-challenger-3"] == {PAYS}
    assert by_place["ml-challenger-1"] == {2 * PAYS}, "each expects what its own numbers say, to the unit"
    assert made.trader.broker_book is None, "and nothing of it could reach a broker"
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped and replayed.live_bytes > 10_000
