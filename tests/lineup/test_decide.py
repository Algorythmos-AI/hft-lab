"""The weekly decision: nothing is promoted without its rule, a breached champion is replaced, and both are provable."""
from __future__ import annotations

import ast
import hashlib
import json

import pytest

from hftlab import lineup
from hftlab.calendar import Calendar
from hftlab.core import SimClock
from hftlab.learn.config import load
from hftlab.lineup import Lineup, decide
from hftlab.lineup.decide import DecideRefused, Desk, Evidence, Gates, read_week, weigh
from hftlab.lineup.serve import make
from hftlab.strategy import IdleSleeve, MlSleeve
from hftlab.trader.config import parse
from tests.learn.kit import WIN, learn_settings
from tests.lineup.kit import CHALLENGER, DAY, ML, MONDAY, SUNDAY, UNITS, Days, desk, effect, put, register, settings
from tests.support import ROOT, called_name, imports, sources
from tests.trader.kit import Pages
from tests.venues.kit import T0

RULE = {"decision": "HFT-DEC-0007", "min_trades": 20, "min_days": 5, "max_drawdown_micro": 100_000, "draws": 400,
        "level_ppm": 900_000, "seed": 7}  # fmt: skip
GOOD, COSTS = [14, 9, -6, 12, 14, 8], [-6, -6, -6, -6, -6]  # per day, in price units: a challenger and a champion
WEEK = "2025-10-19"
EUR = "EUR.USD"


class World:
    """A state directory, a registry with three models, a decision record, and the desk a decision is made at."""

    def __init__(self, folder, *, rule: dict | None = RULE, limits: bool = True, record: str = "approved_by: owner\n"):
        self.folder, self.pages = folder, Pages()
        self.registry = folder / "learn"
        self.a, self.b, self.c = (register(self.registry, seed, not_a_result=0) for seed in (1, 3, 5))
        self.records = folder / "decisions"
        self.records.mkdir()
        if record:
            (self.records / "HFT-DEC-0007-the-first-rule.md").write_text(f"# HFT-DEC-0007\n\n{record}")
        self.learn = load(learn_settings(folder, **({"promotion": rule} if rule else {})))
        raw = settings(self.registry, state_dir=str(folder / "trader"))
        self.desk = Desk(self.learn, parse(raw), limits, self.records, self.pages)
        self.days = Days(folder / "trader")
        self.path = self.learn.lineup

    def lineup(self, found: Lineup) -> Lineup:
        return put(self.folder / "trader", found)

    def good_week(self, monday: int = MONDAY, **more) -> None:
        serving = {ML: self.a.model, CHALLENGER: self.b.model}
        self.days.week(monday, serving, {ML: COSTS, CHALLENGER: GOOD}, **more)

    def decide(self, at_ns: int = SUNDAY, **options):
        return decide.run(self.desk, at_ns, **options)[1]

    def last(self) -> dict:
        return lineup.history(self.path)[-1]


# ---- promote -------------------------------------------------------------------------------------------------------


def test_a_challenger_that_passes_the_rule_and_every_gate_is_promoted_with_the_report_it_rests_on(tmp_path):
    world = World(tmp_path)
    world.lineup(Lineup(champion=world.a, challengers=(world.b,), candidates=(world.c,)))
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "promote" and outcome.said == f"promote: {world.b.model[:12]}"
    after = lineup.read(world.path)
    assert after.champion == world.b and after.previous == (world.a,) and after.challengers == (world.c,)
    line = world.last()
    assert (line["action"], line["decision"], line["week"], line["at_ns"]) == ("promote", "HFT-DEC-0007", WEEK, SUNDAY)
    report = (world.learn.reports / f"{line['report']}.json").read_bytes()
    assert hashlib.sha256(report).hexdigest() == line["report"], "the journal names the report by its hash"
    said = json.loads(report)
    assert said["action"] == "promote" and said["week"] == WEEK and len(said["days"]) == 5 and said["sessions"] == 5
    assert said["trades"] == {ML: {EUR: 25}, CHALLENGER: {EUR: 30}}
    assert said["gates"] == {"limits": 1, "record": 1, "unfit": {}}
    (judged,) = said["challengers"]
    assert judged["reasons"] == [] and judged["evidence"]["advantage_lower_micro"] == 145 * UNITS
    assert world.pages.fired == [f"lineup.promote.{WEEK}"], "every change of the lineup pages"
    (request,) = (tmp_path / "trader" / "requests").glob("*.json")
    assert json.loads(request.read_text())["what"] == "lineup", "and a running trader is asked to start from it"
    with pytest.raises(DecideRefused, match="already decided"):
        world.decide()
    assert lineup.read(world.path) == after, "a week is decided once"


CHANGES = {
    "the rule is not in force": ({"rule": None}, "rule_not_in_force"),
    "every number is written and no decision record is named": ({"rule": {**RULE, "decision": None}},
                                                                "rule_not_in_force"),
    "no decision record of that id": ({"record": ""}, "decision_record_missing"),
    "a record the owner did not approve": ({"record": "approved_by: nobody\n"}, "decision_record_missing"),
    "the risk limits are not in force": ({"limits": False}, "limits_not_in_force"),
}


@pytest.mark.parametrize("why", sorted(CHANGES))
def test_the_same_week_promotes_nothing_when(why, tmp_path):
    options, reason = CHANGES[why]
    world = World(tmp_path, **options)
    world.lineup(Lineup(champion=world.a, challengers=(world.b,)))
    world.good_week()
    outcome = world.decide()
    assert outcome.action == "keep" and reason in outcome.reasons and outcome.promoted is None
    after = lineup.read(world.path)
    assert after.champion == world.a and after.previous == () and after.challengers == ()
    assert world.last()["action"] == "keep" and world.last()["decision"] is None
    rule = options.get("rule", RULE)
    if rule is None or rule["decision"] is None:
        assert outcome.said == "keep: rule not in force" and world.learn.rule_in_force is False
    if rule is not None and rule["decision"] is None:
        (judged,) = outcome.challengers
        assert judged.evidence["advantage_lower_micro"] == 145 * UNITS, "the week is still measured, for the report"
        assert "rule_not_registered" in judged.reasons and "rule_not_in_force" in judged.reasons


def test_a_break_a_placeholder_report_a_week_not_served_whole_or_too_little_evidence_each_keep(tmp_path):
    world = World(tmp_path)
    before = world.lineup(Lineup(champion=world.a, challengers=(world.b,)))
    world.good_week(breaks=1)
    assert world.decide(dry_run=True).reasons == ("reconciliation_breaks",)
    _, evidence = read_week(tmp_path / "trader", SUNDAY)
    held, rule, baseline = world.desk.held, world.learn.rule, "control"
    clean = Evidence(evidence.by_day, evidence.served, {}, 0, evidence.sessions)
    recorded = {world.b.model: {"recorded": 5}}
    passing = Gates(True, True, True, {}, recorded)
    assert weigh(before, clean, held, rule, passing, baseline).action == "promote", "the week itself was good"
    for gates, said in (
        (Gates(True, True, True, {world.b.model: "report_rests_on_placeholders"}, recorded),
         "report_rests_on_placeholders"),
        (Gates(True, True, True, {world.b.model: "model_cannot_be_loaded"}, recorded), "model_cannot_be_loaded"),
        (Gates(False, True, True, {}, recorded), "rule_not_in_force"),
        (Gates(True, True, True), "too_few_own_days"),  # a report that does not say where its days came from
        (Gates(True, True, True, {}, {world.b.model: {"imported:vendor-a": 400, "recorded": 4}}), "too_few_own_days"),
    ):
        assert weigh(before, clean, held, rule, gates, baseline).reasons == (said,)
    other = Evidence(clean.by_day, {**clean.served, CHALLENGER: {world.b.model, ""}}, {}, 0, 5)
    assert weigh(before, other, held, rule, passing, baseline).reasons == ("model_not_served_all_week",)
    fees = Evidence(clean.by_day, clean.served, {}, 0, 5, {CHALLENGER: 1})
    assert weigh(before, fees, held, rule, passing, baseline).reasons == ("fees_not_in_quote_currency",)
    four = {EUR: dict(list(evidence.by_day[CHALLENGER][EUR].items())[:4])}
    short = Evidence({ML: evidence.by_day[ML], CHALLENGER: four}, clean.served, {}, 0, 5)
    assert set(weigh(before, short, held, rule, passing, baseline).reasons) >= {"too_few_days", "days_differ"}
    yen = {day: [-10**9] for day in evidence.by_day[ML][EUR]}  # what a sleeve made in another pair is in yen
    apart = Evidence({ML: {**evidence.by_day[ML], "USD.JPY": yen}, CHALLENGER: evidence.by_day[CHALLENGER]},
                     clean.served, {}, 0, 5)  # fmt: skip
    assert weigh(before, apart, held, rule, passing, baseline).challengers == (
        weigh(before, clean, held, rule, passing, baseline).challengers
    ), "the two are compared in the challenger's pair: two currencies are never added"
    both = Evidence({ML: evidence.by_day[ML], CHALLENGER: {**evidence.by_day[CHALLENGER], "USD.JPY": yen}},
                    clean.served, {}, 0, 5)  # fmt: skip
    assert "traded_more_than_one_pair" in weigh(before, both, held, rule, passing, baseline).reasons
    assert "no_place_configured" in weigh(before, clean, {ML: ("champion", 0)}, rule, passing, baseline).reasons
    assert lineup.read(world.path) == before and world.pages.fired == [], "a dry run and a pure function wrote nothing"
    placeholder = register(world.registry, 9, not_a_result=1)
    assert decide.unfit(Lineup(challengers=(placeholder, world.b)), world.registry) == {
        placeholder.model: "report_rests_on_placeholders"
    }


def test_with_no_champion_the_first_challenger_must_beat_the_control_sleeve(tmp_path):
    world = World(tmp_path)
    world.lineup(Lineup(challengers=(world.b,)))
    world.days.week(MONDAY, {ML: None, CHALLENGER: world.b.model}, {"control": COSTS, CHALLENGER: GOOD})
    assert world.decide().action == "promote" and lineup.read(world.path).previous == ()
    worse = World(tmp_path / "worse")
    worse.lineup(Lineup(challengers=(worse.b,)))
    worse.days.week(MONDAY, {ML: None, CHALLENGER: worse.b.model}, {"control": COSTS, CHALLENGER: [-5] * 6})
    kept = worse.decide()
    assert kept.action == "keep" and "result_not_above_zero" in kept.reasons, "losing less is no reason to trade"


# ---- the shipped settings ------------------------------------------------------------------------------------------


def test_with_the_shipped_settings_it_says_keep_rule_not_in_force_and_changes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    pages = Pages()
    argv = [f"--{name}={ROOT / 'config' / file}" for name, file in
            (("config", "learn.yaml"), ("trader", "trader.yaml"), ("risk", "risk.yaml"), ("calendar", "calendar"))]
    argv.append(f"--records={ROOT / 'research' / 'decisions'}")
    assert decide.main(argv, SimClock(SUNDAY), pages) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"action": "keep", "changed": False, "reasons": ["rule_not_in_force", "no_challenger"],
                   "report": None, "said": "keep: rule not in force", "week": WEEK}  # fmt: skip
    assert list(tmp_path.iterdir()) == [] and pages.fired == [], "not a file, not a page"
    assert decide.main(argv, SimClock(MONDAY), pages) == 2, "and never while the market is open"
    assert decide.main([*argv, "--drill"], SimClock(SUNDAY), pages) == 2, "nothing registered: nothing to drill with"
    assert list(tmp_path.iterdir()) == []
    shipped = load(ROOT / "config" / "learn.yaml")
    assert shipped.rule_in_force is False and shipped.rule.decision is None and shipped.drift.in_force is False
    assert not decide.approved(ROOT / "research" / "decisions", "HFT-DEC-0001"), "no decision record exists yet"


def test_a_rule_not_in_force_still_moves_a_candidate_into_shadow_and_never_into_the_champions_place(tmp_path):
    world = World(tmp_path, rule=None, limits=False)
    world.lineup(Lineup(candidates=(world.b,)))
    world.days.week(MONDAY, {ML: None, CHALLENGER: None}, {"control": COSTS})
    outcome = world.decide()
    assert outcome.said == "keep: rule not in force" and world.pages.fired == [f"lineup.keep.{WEEK}"]
    after = lineup.read(world.path)
    assert after == Lineup(challengers=(world.b,), entries=2, head=after.head) and after.champion is None
    entries = settings(world.registry)["sleeves"]
    served = make(entries, after, registry=world.registry, calendar=Calendar(), others=lambda e: IdleSleeve(e["name"]))
    assert served.serving == {ML: None, CHALLENGER: world.b.model} and type(served.sleeves[3]) is MlSleeve
    world.days.week(MONDAY + 7 * DAY, {ML: None, CHALLENGER: world.b.model}, {"control": COSTS, CHALLENGER: GOOD})
    again = world.decide(SUNDAY + 7 * DAY)
    assert again.action == "keep" and lineup.read(world.path).champion is None, "however good its week was"
    assert lineup.read(world.path).challengers == (), "it has had its week"


# ---- roll back -----------------------------------------------------------------------------------------------------


def test_a_promoted_champion_that_trips_its_loss_latch_is_replaced_by_the_one_before_it(tmp_path):
    world = World(tmp_path)
    world.lineup(Lineup(champion=world.a, challengers=(world.b,), candidates=(world.c,)))
    world.good_week()
    assert world.decide().action == "promote"
    # The week after: B is the champion, C runs in shadow. B breaches the sleeve's daily loss limit on Wednesday.
    serving = {ML: world.b.model, CHALLENGER: world.c.model}
    results = {ML: [-40, -40], CHALLENGER: GOOD}
    world.days.week(MONDAY + 7 * DAY, serving, results, latches=("DAILY_LOSS_SLEEVE/ml/USD",))
    without = Desk(world.learn, world.desk.trader, False, tmp_path / "nowhere", world.pages)  # no limits, no record
    outcome = decide.run(without, SUNDAY + 7 * DAY)[1]
    assert outcome.action == "roll_back" and outcome.said.startswith("roll back")
    after = lineup.read(world.path)
    assert after.champion == world.a and after.previous == () and world.b not in (*after.challengers, *after.candidates)
    assert after.challengers == (), "nothing is promoted on the decision that rolls back, however good C's week"
    line = world.last()
    assert (line["action"], line["reasons"], line["decision"]) == ("roll_back", ["champion_latch_breached"], None)
    assert world.pages.fired[-1] == "lineup.roll_back.2025-10-26"
    assert len(list((tmp_path / "trader" / "requests").glob("*.json"))) == 2


def test_a_rollback_needs_no_rule_and_a_latch_of_another_model_or_another_sleeve_is_not_the_champions(tmp_path):
    world = World(tmp_path, rule=None, limits=False, record="")
    world.lineup(Lineup(champion=world.b, previous=(world.a,)))
    world.days.week(MONDAY, {ML: world.b.model, CHALLENGER: None}, {ML: COSTS}, latches=("DRAWDOWN_SLEEVE/ml/USD",))
    assert world.decide(dry_run=True).action == "roll_back"
    for serving, latch in (
        ({ML: world.a.model, CHALLENGER: None}, "DAILY_LOSS_SLEEVE/ml/USD"),  # the model before it tripped
        ({ML: world.b.model, CHALLENGER: None}, "DAILY_LOSS_SLEEVE/control/USD"),
        ({ML: world.b.model, CHALLENGER: None}, "ORDER_FAULT"),
    ):
        other = World(tmp_path / str(len(list(tmp_path.iterdir()))), rule=None, limits=False)
        other.lineup(Lineup(champion=world.b, previous=(world.a,)))
        other.days.week(MONDAY, serving, {ML: COSTS}, latches=(latch,))
        assert other.decide(dry_run=True).action == "keep", latch
    assert world.decide().action == "roll_back" and lineup.read(world.path).champion == world.a
    alone = World(tmp_path / "alone", rule=None)
    alone.lineup(Lineup(champion=alone.a))
    alone.days.week(MONDAY, {ML: alone.a.model, CHALLENGER: None}, {}, latches=("DAILY_LOSS_SLEEVE/ml/USD",))
    assert alone.decide().action == "roll_back" and lineup.read(alone.path).champion is None, "the place runs idle"


def test_the_drill_proves_the_rollback_on_a_copy_and_leaves_the_lineup_in_force_as_it_was(tmp_path):
    world = World(tmp_path, rule=None, limits=False)
    before = world.lineup(Lineup(champion=world.a, candidates=(world.b,)))
    assert decide.drill(world.desk, SUNDAY) == 0
    after = lineup.read(world.path)
    assert after.same_models(before) and after.entries == before.entries + 1
    line = world.last()
    assert (line["action"], line["reasons"]) == ("drill", ["passed"])
    assert line["evidence"] == {"subject": world.b.model, "rolled_back": 1, "restored": 1, "gone": 1, "loads": 1}
    assert json.loads((world.learn.reports / f"{line['report']}.json").read_bytes())["passed"] == 1
    assert world.pages.fired == [f"lineup.drill.{SUNDAY}"] and not (tmp_path / "trader" / "requests").exists()
    assert world.decide().said == "keep: rule not in force", "a drill is not the week's decision"
    (world.registry / "models" / f"{world.a.model}.json").unlink()  # the champion to go back to cannot be loaded
    world.lineup(lineup.read(world.path).registered(world.c))
    assert decide.drill(world.desk, SUNDAY + 1) == 1
    assert world.last()["reasons"] == ["failed"] and world.last()["evidence"]["loads"] == 0
    assert lineup.read(world.path).champion == world.a, "and a failed drill moves nothing either"


# ---- reading the week ----------------------------------------------------------------------------------------------


def test_a_round_trip_is_counted_once_after_its_fees_on_the_day_it_closed_and_only_in_its_week(tmp_path):
    days = Days(tmp_path)
    days.session(MONDAY - 3 * DAY, {}, [("control", 3)])  # the Friday before: read, and not of this week
    days.session(MONDAY, {}, [("control", 5), ("ml", -2)], fee=1_500)
    days.session(MONDAY + DAY, {}, [("control", -1)])
    week, found = read_week(tmp_path, SUNDAY)
    assert week.label == WEEK and week.days == ("2025-10-13", "2025-10-14")
    per = UNITS * 10
    assert [list(day) for day in found.by_day["control"][EUR].values()] == [[5 * per - 3_000], [-per]]
    assert list(found.by_day["ml"][EUR].values()) == [[-2 * per - 3_000]] and found.sessions == 2 and found.breaks == 0
    (tmp_path / "days" / "2025-10-14" / "decisions.jsonl").write_bytes(b'{"type":"start"}\n')
    with pytest.raises(DecideRefused, match="do not verify"):
        read_week(tmp_path, SUNDAY)


def test_what_it_reads_of_a_real_trader_week_is_the_shadow_books_result_to_the_unit(tmp_path):
    registry = tmp_path / "learn"
    a, b = register(registry, 1), register(registry, 3)
    put(tmp_path, Lineup(champion=a, challengers=(b,)))
    made = desk(tmp_path, registry)
    made.run(effect(made, 7, 20), quotes=False)
    made.stop()
    _, found = read_week(tmp_path, T0 + 3 * DAY)
    summary = {one["name"]: one for one in made.trader.summary()["sleeves"]}
    for name in (ML, CHALLENGER, "control"):
        for pair, currency in ((EUR, "USD"), ("USD.JPY", "JPY")):
            trips = [result for day in found.by_day[name].get(pair, {}).values() for result in day]
            assert sum(trips) == summary[name]["net_total_micro"][currency], "each pair in its own currency"
        assert sum(len(d) for p in found.by_day[name].values() for d in p.values()) == summary[name]["fills"] // 2
    assert found.pairs("control") == [EUR, "USD.JPY"] and found.pairs(ML) == [EUR]
    assert found.served == {ML: {a.model}, CHALLENGER: {b.model}} and found.sessions == 1 and not found.fees_elsewhere
    assert all(result == WIN for result in next(iter(found.by_day[ML][EUR].values()))), "each trip, the planted effect"


# ---- structure -----------------------------------------------------------------------------------------------------


def test_nothing_in_the_lineup_package_holds_a_venue_or_calls_one():
    found = [(rel, tree) for rel, tree in sources() if rel.startswith("src/hftlab/lineup/")]
    files = ["__init__.py", "decide.py", "journal.py", "serve.py", "watch.py"]
    assert [rel.rsplit("/", 1)[1] for rel, _ in found] == files
    event = "hftlab.venues.port.ConnectionEvent"  # an event of a tape, which the drift look folds into features
    for rel, tree in found:
        for _, name in imports(tree, rel):
            assert name.split(".")[0] not in {"ib_async", "socket", "ssl", "asyncio", "http", "urllib"}, (rel, name)
            assert not name.startswith(("hftlab.exec", "hftlab.safety", "hftlab.shadow")), (rel, name)
            assert not name.startswith("hftlab.venues") or (rel.endswith("watch.py") and name == event), (rel, name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert called_name(node) not in {"place", "cancel", "mark_reconciled", "connect", "propose"}, rel
