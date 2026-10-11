"""From the lineup to the trader: a model is served by its hash or not at all, and no place holds the others up."""
from __future__ import annotations

import pytest

from hftlab import lineup
from hftlab.calendar import Calendar
from hftlab.journal import read
from hftlab.lineup import Lineup, LineupError
from hftlab.lineup.serve import load, make, places
from hftlab.strategy import IdleSleeve, MlSleeve
from hftlab.trader.config import TraderConfigError, parse
from tests.learn.kit import WIN
from tests.lineup.kit import CHALLENGER, MARGIN, ML, desk, effect, place, put, register, settings
from tests.trader.kit import put_stage


@pytest.fixture(scope="module")
def kept(tmp_path_factory):
    """A registry with two models, each fitted to its own stream with the effect planted."""
    registry = tmp_path_factory.mktemp("registry")
    return registry, register(registry, 1), register(registry, 3)


def served(registry, found: Lineup, **more):
    entries = settings(registry)["sleeves"]
    return make(entries, found, registry=registry, calendar=Calendar(), others=lambda e: IdleSleeve(e["name"]), **more)


# ---- by hash, or not at all ----------------------------------------------------------------------------------------


def test_each_place_gets_the_model_the_lineup_names_and_an_empty_place_runs_idle(kept):
    registry, a, b = kept
    found = served(registry, Lineup(champion=a, challengers=(b,)))
    assert found.serving == {ML: a.model, CHALLENGER: b.model} and found.refused == {}
    assert [type(sleeve) for sleeve in found.sleeves] == [IdleSleeve, IdleSleeve, MlSleeve, MlSleeve]
    assert [sleeve.name for sleeve in found.sleeves] == ["idle", "control", ML, CHALLENGER]
    empty = served(registry, Lineup(challengers=(b,), candidates=(a,)))
    assert empty.serving == {ML: None, CHALLENGER: b.model}, "a candidate serves nowhere"
    assert type(empty.sleeves[2]) is IdleSleeve and empty.sleeves[2].name == ML
    assert served(registry, Lineup()).serving == {ML: None, CHALLENGER: None}


def test_a_file_that_is_not_what_its_name_says_is_refused_and_its_place_runs_idle(kept, tmp_path):
    registry, a, b = kept
    other = (registry / "models" / f"{b.model}.json").read_bytes()
    for what, damage, said in (
        ("models", lambda path: path.write_bytes(other), "holds model"),
        ("models", lambda path: path.unlink(), "cannot be read"),
        ("models", lambda path: path.write_bytes(path.read_bytes().replace(b"EUR.USD", b"EUR.GBP")), "cannot be read"),
        ("reports", lambda path: path.write_bytes(path.read_bytes() + b" "), "not the report it is named for"),
        ("reports", lambda path: path.unlink(), "cannot be read"),
    ):
        copy = tmp_path / f"{what}-{said[:6]}-{len(list(tmp_path.iterdir()))}"
        for folder in ("models", "reports"):
            (copy / folder).mkdir(parents=True)
            for path in (registry / folder).iterdir():
                (copy / folder / path.name).write_bytes(path.read_bytes())
        named = a.model if what == "models" else a.report
        damage(copy / what / f"{named}.json")
        with pytest.raises(LineupError, match=said):
            load(a, copy)
        found = served(copy, Lineup(champion=a, challengers=(b,)))
        assert found.serving == {ML: None, CHALLENGER: b.model} and said in found.refused[ML], "and only that place"
        assert type(found.sleeves[2]) is IdleSleeve
    crossed = lineup.Named(a.model, b.report)
    with pytest.raises(LineupError, match="is not the evaluation of model"):
        load(crossed, registry)
    assert served(None, Lineup(champion=a)).refused[ML].endswith("the settings name no registry")
    unreadable = served(registry, Lineup(), problem="the lineup journal does not verify")
    assert unreadable.serving == {ML: None, CHALLENGER: None} and set(unreadable.refused) == {ML, CHALLENGER}


def test_the_settings_name_places_and_each_place_once(kept):
    registry = kept[0]
    assert places(settings(registry)["sleeves"]) == {ML: ("champion", 0), CHALLENGER: ("challenger", 0)}
    for wrong, said in (
        ([place("ml", "champion"), place("other", "champion")], "same place"),
        ([place("ml", "challenger-0")], "champion or challenger-N"),
        ([place("ml", "latest")], "champion or challenger-N"),
        ([place("ml", "champion", scorer="thread")], "the scorer is in_process or process"),
        ([place("ml", "champion", margin_micro=-1)], "two whole numbers"),
        ([{**place("ml", "champion"), "model": "a" * 64}], "exactly the keys"),
    ):
        raw = settings(registry)
        raw["sleeves"] = [*raw["sleeves"][:2], *wrong]
        raw["rollout"] = {name: raw["rollout"]["idle"] for name in [e["name"] for e in raw["sleeves"]]}
        with pytest.raises(TraderConfigError, match=said):
            parse(raw)
    without = settings(registry)
    del without["registry"]
    with pytest.raises(TraderConfigError, match="needs `registry`"):
        parse(without)


# ---- in the trader -------------------------------------------------------------------------------------------------


def test_a_challenger_trades_in_shadow_beside_the_champion_and_the_day_replays_byte_for_byte(kept, tmp_path):
    registry, a, b = kept
    put(tmp_path, Lineup(champion=a, challengers=(b,)))
    made = desk(tmp_path, registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    (start,) = made.of("start")
    assert start["serving"] == {ML: a.model, CHALLENGER: b.model} and start["mode"] == "shadow_only"
    assert made.trader.stages.all()[ML] == made.trader.stages.all()[CHALLENGER] == "shadow"
    entries = [e for e in made.of("decision") if e["leg"] == "entry"]
    assert {e["sleeve"] for e in entries} >= {ML, CHALLENGER} and len(entries) > 30
    for entry in entries:
        ours = entry["sleeve"] in (ML, CHALLENGER)
        assert (type(entry["expected_micro"]) is int and entry["expected_micro"] > MARGIN) if ours else (
            entry["expected_micro"] is None
        ), "what the model expected, beside the decision; a sleeve without a model expects nothing"
    summary = {one["name"]: one for one in made.trader.summary()["sleeves"]}
    assert summary[ML]["net_total_micro"]["USD"] == summary[ML]["fills"] // 2 * WIN > 0, "the planted effect, exactly"
    assert summary[CHALLENGER]["net_total_micro"]["USD"] > 0 > summary["control"]["net_total_micro"]["USD"]
    assert made.trader.broker_book is None, "and nothing of it could reach a broker"
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical and replayed.stopped and replayed.live_bytes > 10_000


def test_a_changed_lineup_ends_the_session_and_the_next_one_serves_it(kept, tmp_path):
    registry, a, b = kept
    before = put(tmp_path, Lineup(champion=a, challengers=(b,)))
    made = desk(tmp_path, registry)
    made.run(5)
    put(tmp_path, before.promoted(b), lineup.PROMOTE, at_ns=2)
    made.ask("lineup", "", operator="the weekly decision")
    assert made.run(30) is False and made.clock.now_ns() < made.until, "it stopped by itself, on that beat"
    assert made.of("request")[-1]["what"] == "lineup" and made.of("request")[-1]["done"] == 1
    assert made.of("start")[-1]["serving"] == {ML: a.model, CHALLENGER: b.model}, "the session in hand is not touched"
    made.stop()
    made.session()
    assert made.of("start")[-1]["serving"] == {ML: b.model, CHALLENGER: None}
    models = [e for e in read(tmp_path / "stages.jsonl")[1] if e["type"] == "model"]
    assert [(e["sleeve"], e["model"]) for e in models] == [
        (ML, a.model), (CHALLENGER, b.model), (ML, b.model), (CHALLENGER, ""),
    ]  # fmt: skip
    made.run(5)
    made.stop()
    assert all(one.identical for one in made.replay(tmp_path / "scratch")), "each session with the models it ran with"


def test_a_place_whose_model_changed_loses_a_stage_that_places_orders(kept, tmp_path):
    registry, a, b = kept
    before = put(tmp_path, Lineup(champion=a, challengers=(b,)))
    first = desk(tmp_path, registry, paper=True)
    first.run(2)
    first.stop()
    put_stage(tmp_path, ML, "min_size", at_ns=first.clock.now_ns())
    put(tmp_path, before.promoted(b), lineup.PROMOTE, at_ns=2)
    made = desk(tmp_path, registry, paper=True, at_ns=first.clock.now_ns())
    assert made.trader.stages.stage(ML) == "stepped_down", "the model judged in shadow does not inherit min_size"
    (change,) = made.of("stage")
    assert (change["sleeve"], change["before"], change["cause"]) == (ML, "min_size", "MODEL")
    assert change["detail"] == b.model[:12]
    assert [key for key in made.pages.fired if key.startswith(f"stage.{ML}.")], "and the owner is told"
    again = desk(tmp_path, registry, paper=True, at_ns=first.clock.now_ns() + 1)
    assert again.of("stage") == [change] or len(again.of("stage")) == 1, "the same model again changes nothing"


def test_a_lineup_that_cannot_be_used_stops_no_other_sleeve_the_places_run_idle_and_the_owner_is_paged(kept, tmp_path):
    registry, a, b = kept
    put(tmp_path, Lineup(champion=a, challengers=(b,)))
    path = tmp_path / lineup.FILE
    path.write_bytes(path.read_bytes().replace(a.model.encode(), b.model.encode(), 1))  # a line was edited
    with pytest.raises(LineupError, match="does not verify"):
        lineup.read(path)
    made = desk(tmp_path, registry)
    made.run(30)
    made.stop()
    assert made.of("start")[0]["serving"] == {ML: None, CHALLENGER: None}
    assert [e["sleeve"] for e in made.of("lineup_refused")] == [ML, CHALLENGER]
    assert {"lineup.ml", "lineup.ml-challenger-1"} <= set(made.pages.fired)
    counts = {one["name"]: one for one in made.trader.summary()["sleeves"]}
    assert counts["control"]["fills"] > 0 and counts[ML]["decisions"] == counts[CHALLENGER]["decisions"] == 0
    (replayed,) = made.replay(tmp_path / "scratch")
    assert replayed.identical
