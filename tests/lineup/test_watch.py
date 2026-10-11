"""The drift look: a week like the training one asks for nothing, a planted shift takes the sleeve down to shadow."""
from __future__ import annotations

import ast
import json

from hftlab import lineup
from hftlab.calendar import Calendar
from hftlab.core import SimClock
from hftlab.learn.config import load
from hftlab.lineup import Lineup, watch
from hftlab.trader.config import parse
from tests.learn.kit import learn_settings, planted
from tests.lineup.kit import CHALLENGER, ML, desk, effect, place, put, register, settings
from tests.support import ROOT, called_name, imports, sources
from tests.trader.kit import Desk as TraderDesk
from tests.trader.kit import Pages, put_stage
from tests.venues.kit import MS, T0

# The registered models were fitted to two minutes of one morning, so the three clock features of any other
# minute fall in other bins: a threshold of the whole is one they cannot pass, and a wider spread still can.
LIMITS = {"min_rows": 10, "feature_shift_ppm": 999_999, "calibration_gap_micro": 1_000_000, "spread_ratio_x100": 150}


class World:
    def __init__(self, folder, *, drift: dict | None = LIMITS, paper: bool = False, margin: int | None = None):
        self.registry, self.state, self.pages = folder / "learn", folder / "trader", Pages()
        self.a, self.b = register(self.registry, 1), register(self.registry, 3)
        put(self.state, Lineup(champion=self.a, challengers=(self.b,)))
        raw = settings(self.registry, state_dir=str(self.state))
        if margin is not None:
            raw["sleeves"] = [*raw["sleeves"][:2], place(ML, "champion", margin_micro=margin), raw["sleeves"][3]]
        self.learn = load(learn_settings(folder, **({"drift": drift} if drift else {})))
        self.desk = watch.Desk(self.learn, parse(raw), Calendar(), self.pages)
        self.options = {"paper": paper, "settings": raw}

    def trader(self, **more):
        return TraderDesk(self.state, **{**self.options, **more})

    def look(self, at_ns: int, **options):
        return watch.run(self.desk, at_ns, **options)[1]

    def requests(self) -> list[dict]:
        return [json.loads(path.read_text()) for path in sorted((self.state / "requests").glob("*.json"))]


def stream(made, seed: int, blocks: int, *, wider: int = 0, jump: bool = True) -> int:
    """The planted stream into the desk's market, with the spread `wider` price units wider on each side."""
    for quote in planted(seed, blocks, effect=jump, start_ns=made.quoted + MS):
        made.market.script_quote(
            quote.recv_ts_ns, "EUR.USD", quote.bid - wider, quote.ask + wider, quote.bid_size, quote.ask_size
        )
    made.market.script_quote(made.quoted + MS, "USD.JPY", 151_230, 151_240)
    made.quoted += blocks * 3_000 * MS
    return blocks * 3


def names(found) -> dict[str, list[str]]:
    return {one.sleeve: [f"{f.monitor}:{f.subject}" for f in one.breached] for one in found}


def test_a_week_like_the_training_one_asks_for_nothing_and_pages_nobody(tmp_path, capsys):
    world = World(tmp_path)
    made = desk(world.state, world.registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    found = world.look(made.clock.now_ns())
    assert names(found) == {ML: [], CHALLENGER: []} and [one.model for one in found] == [world.a.model, world.b.model]
    by_subject = {f"{f.monitor}:{f.subject}": f for f in found[0].findings}
    assert by_subject["spread_regime:spread@v1"].value == 100 and by_subject["feature_shift:spread@v1"].value == 0
    told = by_subject["calibration_decay:bucket-3"]
    assert told.value is not None and -100 < told.value <= 100, "what it expected of its trades is what they made"
    assert world.pages.fired == [] and not (world.state / "requests").exists()
    assert world.pages.resolved == ["drift.ml", "drift.ml-challenger-1"], "an earlier page would be taken back"
    said = json.loads(capsys.readouterr().out)
    assert said["kind"] == "drift-report" and said["in_force"] == 1 and said["unread"] == {}
    assert said["thresholds"] == LIMITS and [one["sleeve"] for one in said["watches"]] == [ML, CHALLENGER]
    assert not (world.learn.reports).exists() or all(
        json.loads(path.read_bytes())["kind"] != "drift-report" for path in world.learn.reports.iterdir()
    ), "a look that found nothing keeps nothing"


def test_a_planted_spread_shift_asks_the_trader_to_take_the_sleeve_down_to_shadow_and_the_trader_does(tmp_path):
    world = World(tmp_path, paper=True, margin=10**12)  # a champion on paper that is flat: it enters on nothing
    first = world.trader()
    first.run(2)
    first.stop()
    put_stage(world.state, ML, "min_size", at_ns=first.clock.now_ns())
    made = world.trader(at_ns=first.clock.now_ns())
    assert made.trader.stages.stage(ML) == "min_size"
    made.run(stream(made, 7, 20, wider=5), quotes=False)  # the spread the model was priced at, doubled
    made.stop()

    assert names(world.look(made.clock.now_ns(), dry_run=True))[ML], "a dry run finds it"
    assert world.pages.fired == [] and not (world.state / "requests").exists(), "and asks for nothing"
    found = world.look(made.clock.now_ns())
    assert set(names(found)[ML]) == {"feature_shift:spread@v1", "spread_regime:spread@v1"}
    assert {f.value for f in found[0].breached} == {1_000_000, 200}
    assert world.pages.fired == ["drift.ml", "drift.ml-challenger-1"], "one page per sleeve that breached"
    asked = world.requests()
    assert [(one["what"], one["subject"], one["value"], one["operator"]) for one in asked] == [
        ("stage", ML, "shadow", "the drift monitor"), ("stage", CHALLENGER, "shadow", "the drift monitor"),
    ]  # fmt: skip
    (kept,) = [path for path in world.learn.reports.iterdir() if b"drift-report" in path.read_bytes()]
    assert json.loads(kept.read_bytes())["watches"][0]["breached"], "the report the page names is kept by its hash"

    again = world.trader(at_ns=made.clock.now_ns())  # the trader takes the requests at its next beat
    again.run(3)
    again.stop()
    done = {one["subject"]: one["done"] for one in again.of("request")}
    assert done == {ML: 1, CHALLENGER: 0}, "the champion comes down; the challenger was in shadow already"
    (change,) = [e for e in again.of("stage") if e["sleeve"] == ML]
    assert (change["before"], change["to"], change["cause"], change["detail"]) == (
        "min_size", "shadow", "OPERATOR", "the drift monitor",
    )  # fmt: skip
    assert again.trader.stages.stage(ML) == "shadow"


def test_expectations_that_stopped_coming_true_breach_calibration_for_the_sleeve_that_held_them(tmp_path):
    world = World(tmp_path)
    made = desk(world.state, world.registry)
    made.run(stream(made, 7, 20, jump=False), quotes=False)  # the one-sided book, and no move after it
    made.stop()
    found = names(world.look(made.clock.now_ns()))
    assert found == {ML: ["calibration_decay:bucket-3"], CHALLENGER: ["calibration_decay:bucket-3"]}
    assert world.pages.fired == ["drift.ml", "drift.ml-challenger-1"] and len(world.requests()) == 2


def test_with_a_threshold_null_everything_is_computed_and_nothing_is_breached(tmp_path, capsys):
    world = World(tmp_path, drift={**LIMITS, "min_rows": None})
    made = desk(world.state, world.registry)
    made.run(stream(made, 7, 20, wider=5, jump=False), quotes=False)
    made.stop()
    found = world.look(made.clock.now_ns())
    assert names(found) == {ML: [], CHALLENGER: []}, "one null and no monitor is in force"
    values = {f"{f.monitor}:{f.subject}": f.value for f in found[0].findings}
    assert values["spread_regime:spread@v1"] == 200 and all(f.threshold is None for f in found[0].findings)
    assert world.pages.fired == [] and world.pages.resolved == [] and not (world.state / "requests").exists()
    assert json.loads(capsys.readouterr().out)["in_force"] == 0


def test_a_place_whose_model_cannot_be_read_is_reported_and_the_others_are_still_looked_at(tmp_path, capsys):
    world = World(tmp_path)
    (world.registry / "reports" / f"{world.a.report}.json").unlink()
    found = world.look(T0)
    assert [one.sleeve for one in found] == [CHALLENGER]
    assert set(json.loads(capsys.readouterr().out)["unread"]) == {ML}


def test_with_the_shipped_settings_it_looks_at_nothing_and_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    pages = Pages()
    argv = [f"--{name}={ROOT / 'config' / file}" for name, file in
            (("config", "learn.yaml"), ("trader", "trader.yaml"), ("calendar", "calendar"))]  # fmt: skip
    assert watch.main(argv, SimClock(T0), pages) == 0
    said = json.loads(capsys.readouterr().out)
    assert said["in_force"] == 0 and said["watches"] == [] and said["lineup"] == ""
    assert list(tmp_path.iterdir()) == [] and pages.fired == [] and pages.resolved == []


def test_the_look_holds_no_venue_and_its_one_request_is_a_stage_of_shadow():
    tree = dict(sources())["src/hftlab/lineup/watch.py"]
    for _, name in imports(tree, "src/hftlab/lineup/watch.py"):
        assert not name.startswith(("hftlab.exec", "hftlab.safety", "hftlab.shadow", "hftlab.risk")), name
    asked = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and called_name(node) == "leave_request"]
    (only,) = asked
    assert [ast.unparse(arg) for arg in only.args[1:]] == ["'stage'", "watch.sleeve", "SHADOW", "OPERATOR"]
    assert watch.SHADOW == "shadow" and lineup.FILE == "lineup.jsonl"
