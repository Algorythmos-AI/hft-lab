"""The weekly decision and the model file's own split: a model with too few own days is kept, whichever file says so.

A report says the days its model was fitted to, by source, and a model file of version 2 says them under its own
hash (ADR 0022, ADR 0028). The decision counts a day of this platform's own recordings only where the two agree.
A file of version 1 does not say; its report alone is read, as it was before the field existed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

from hftlab import lineup
from hftlab.journal import canonical
from hftlab.learn.train import keep_report
from hftlab.lineup import Lineup, Named
from hftlab.lineup.decide import vouched_days
from hftlab.lineup.serve import load
from hftlab.models import Model, ModelRegistry, parse
from tests.lineup.kit import register
from tests.lineup.test_decide import World

THEIRS = "imported:vendor-a"


def kept_as(world: World, model: Model, said: Named) -> Named:
    """The model in the registry, beside a copy of the report `said` names that is the evaluation of this model."""
    report = json.loads((world.registry / "reports" / f"{said.report}.json").read_bytes())
    report["model"]["hash"] = model.content_hash
    ModelRegistry(world.registry / "models").put(model)
    data = (canonical(report) + "\n").encode("ascii")
    return Named(model.content_hash, keep_report(world.registry / "reports", data))


def decided(world: World, challenger: Named):
    world.b = challenger
    world.lineup(Lineup(champion=world.a, challengers=(challenger,)))
    world.good_week()
    outcome = world.decide()
    return outcome, json.loads((world.learn.reports / f"{world.last()['report']}.json").read_bytes())


def test_a_model_whose_own_file_says_too_few_own_days_is_kept_however_many_days_were_imported(tmp_path):
    world = World(tmp_path)
    days = {THEIRS: 400, "recorded": 4}  # the rule asks for five
    few = register(world.registry, 3, not_a_result=0, fitted_days=days)
    assert load(few, world.registry)[0].fitted("days") == days, "the model file says it, not only the report"
    outcome, said = decided(world, few)
    assert outcome.action == "keep" and outcome.reasons == ("too_few_own_days",) and outcome.promoted is None
    assert outcome.challengers[0].evidence["own_days"] == 4
    assert said["fitted_days_by_source"] == said["model_file_days_by_source"] == {few.model: days}
    assert lineup.read(world.path).champion == world.a


def test_a_report_that_claims_more_own_days_than_its_model_file_says_counts_for_nothing(tmp_path):
    world = World(tmp_path)
    honest = register(world.registry, 3, not_a_result=0)  # its report says five recorded days
    model, report, _ = load(honest, world.registry)
    imports = dataclasses.replace(model, sources={"rows": {THEIRS: 500}, "days": {THEIRS: 5}})
    crossed = kept_as(world, imports, honest)
    assert vouched_days(model, report) == {"recorded": 5} and vouched_days(imports, report) == {}
    outcome, said = decided(world, crossed)
    assert outcome.action == "keep" and outcome.reasons == ("too_few_own_days",)
    assert outcome.challengers[0].evidence["own_days"] == 0, "the two files disagree: no day is counted"
    assert said["fitted_days_by_source"] == {crossed.model: {}}
    assert said["model_file_days_by_source"] == {crossed.model: {THEIRS: 5}}, "and the report shows what each said"


def test_a_model_file_of_the_older_version_is_judged_by_its_report_alone_and_its_own_split_is_said_unknown(tmp_path):
    world = World(tmp_path)
    honest = register(world.registry, 3, not_a_result=0)
    model, report, _ = load(honest, world.registry)
    body = {key: value for key, value in json.loads(model.data()).items() if key not in ("hash", "sources")}
    body["version"] = 1
    digest = hashlib.sha256(canonical(body).encode("ascii")).hexdigest()
    before = parse((canonical({"hash": digest, **body}) + "\n").encode("ascii"))
    assert before.sources is None and vouched_days(before, report) == {"recorded": 5}
    outcome, said = decided(world, kept_as(world, before, honest))
    assert outcome.action == "promote" and outcome.promoted is not None and outcome.promoted.model == digest
    assert said["model_file_days_by_source"] == {digest: "unknown"} and said["fitted_days_by_source"] == {
        digest: {"recorded": 5}
    }, "unknown is said as unknown: never as no days"
    short = World(tmp_path / "short")
    silent = register(short.registry, 3, not_a_result=0, fitted_days={})
    old = parse((canonical({"hash": digest, **body}) + "\n").encode("ascii"))
    outcome, _ = decided(short, kept_as(short, old, silent))
    assert outcome.action == "keep" and outcome.reasons == ("too_few_own_days",), "and a report that says none has none"
