"""Datasets, reports, the feasibility map and the weekly decision know which days are the platform's own (ADR 0028)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hftlab.calendar import Calendar
from hftlab.core import SimClock
from hftlab.feasibility import __main__ as feasibility_cli
from hftlab.labels import NO_ENTRY, LabelSpec
from hftlab.learn import train
from hftlab.learn.config import load
from hftlab.learn.dataset import UNSTATED, DatasetError, DaySource, build_from_days
from hftlab.lineup import Lineup
from hftlab.lineup import read as read_lineup
from hftlab.lineup.decide import Gates, fitted_days, read_week, weigh
from hftlab.models import parse
from hftlab.ops import report as scorecard
from hftlab.record.store import day_of, days
from hftlab.shadow import __main__ as shadow_cli
from tests.history.kit import a_folder, run
from tests.learn.kit import CONFIG, HORIZON, SATURDAY, dataset, learn_settings, planted, record, weeks
from tests.lineup import test_decide
from tests.shadow.test_runner import shadow_config
from tests.support import PLACEHOLDER, ROOT
from tests.venues.kit import EURUSD, T0, plain_costs

SHIPPED = ["--cost-model", str(PLACEHOLDER / "cost_model.yaml"), "--calendar", str(ROOT / "config/calendar"),
           "--feasibility", str(ROOT / "config/feasibility.yaml")]  # fmt: skip
SIZED = {"time": 1, "bid": 2, "ask": 3, "bid_size": 4, "ask_size": 5}
A, B = "imported:vendor-a", "imported:vendor-b"


def exported(folder, quotes, *, sizes: bool = True):
    """The quotes as a file somebody's export tool might have written: a count of nanoseconds, prices as decimals."""
    lines = []
    for quote in quotes:
        prices = [EURUSD.format_price(quote.bid), EURUSD.format_price(quote.ask)]
        lines.append(",".join([str(quote.recv_ts_ns), *prices, *([str(quote.bid_size), str(quote.ask_size)] * sizes)]))
    columns = SIZED if sizes else {key: SIZED[key] for key in ("time", "bid", "ask")}
    how = {"header": False, "time_format": "epoch_ns", "columns": columns, "size_unit": "1" if sizes else None}
    return a_folder(folder, {"ticks.csv": "\n".join(lines) + "\n"}, **how)


def history(tmp_path, name: str, quotes, label: str = "vendor-a", **options):
    run(tmp_path / "hist", exported(tmp_path / f"in-{name}", quotes, **options), label)
    return tmp_path / "hist"


def from_days(folders):
    return build_from_days(folders, "EUR.USD", CONFIG, LabelSpec(HORIZON), plain_costs(), Calendar())


# ---- a dataset -------------------------------------------------------------------------------------------------------


def test_a_mixed_dataset_reports_exactly_how_many_rows_and_days_came_from_each_source(tmp_path):
    record(tmp_path / "rec", weeks(1, 6, on=(0, 1)))
    history(tmp_path, "a", weeks(2, 6, on=(4, 5)))
    history(tmp_path, "b", weeks(3, 6, on=(11,)), "vendor-b")
    folders = [*days(tmp_path / "rec"), *days(tmp_path / "hist")]
    made = from_days(folders)
    ours = "recorded"
    source_of_day = {"2025-10-09": ours, "2025-10-10": ours, "2025-10-13": A, "2025-10-14": A, "2025-10-20": B}
    assert [(one.day, one.source) for one in made.sources] == list(source_of_day.items())
    counted: dict[str, int] = {}
    for row in made.rows:  # counted here by the day each row's decision was made on, with none of the dataset's code
        source = source_of_day[day_of(row.known_ns)]
        counted[source] = counted.get(source, 0) + 1
    assert made.split() == {"rows": counted, "days": {A: 2, B: 1, "recorded": 2}}
    assert sum(counted.values()) == len(made.rows) and all(count > 0 for count in counted.values())
    first_week = [position for position, row in enumerate(made.rows) if day_of(row.known_ns) < "2025-10-11"]
    assert made.split(first_week) == {"rows": {"recorded": counted["recorded"]}, "days": {"recorded": 2}}
    assert made.split([]) == {"rows": {}, "days": {}}
    said = made.describe()["sources"]
    assert said["split"] == made.split() and said["placeholder_sizes"] == 0
    assert [day[:2] for day in said["days"]] == [[day, source] for day, source in source_of_day.items()]
    assert sum(day[2] for day in said["days"]) == made.events


def test_where_the_days_came_from_is_in_the_dataset_s_hash_and_unstated_is_never_counted_as_recorded(tmp_path):
    stream = weeks(1, 6, on=(0, 1))
    record(tmp_path / "rec", stream)
    history(tmp_path, "a", stream)
    ours, theirs = from_days(days(tmp_path / "rec")), from_days(days(tmp_path / "hist"))
    assert [row.features for row in ours.rows] == [row.features for row in theirs.rows]  # the same quotes
    assert [(row.buy_micro, row.sell_micro) for row in ours.rows] == [(r.buy_micro, r.sell_micro) for r in theirs.rows]
    assert ours.content_hash != theirs.content_hash
    assert set(ours.split()["rows"]) == {"recorded"} and set(theirs.split()["rows"]) == {A}
    nobody_said = dataset(stream)
    assert nobody_said.split() == {"rows": {UNSTATED: len(nobody_said.rows)}, "days": {UNSTATED: 1}}
    assert nobody_said.content_hash not in (ours.content_hash, theirs.content_hash)
    with pytest.raises(DatasetError, match="every event"):
        dataset(stream, sources=[DaySource("2025-10-09", "recorded", len(stream) - 1)])


def test_a_placeholder_size_fills_no_order_so_a_day_without_sizes_has_no_trade_and_no_paying_window(tmp_path, capsys):
    stream = planted(1, 20)
    history(tmp_path, "none", stream, sizes=False)
    made = from_days(days(tmp_path / "hist"))
    assert made.placeholder_sizes and len(made.rows) > 100
    assert all(row.buy_how == row.sell_how == NO_ENTRY and row.buy_micro == row.sell_micro == 0 for row in made.rows)
    assert made.counted["no_entry"] == 2 * len(made.rows)
    run(tmp_path / "sized", exported(tmp_path / "in-sized", stream))
    sized = from_days(days(tmp_path / "sized"))
    assert not sized.placeholder_sizes and sized.counted["no_entry"] < len(sized.rows)  # with sizes, orders fill
    assert feasibility_cli.main(["--store", str(tmp_path / "hist"), *SHIPPED[:4], "--config", SHIPPED[5]]) == 0
    out, err = capsys.readouterr()
    found = json.loads(out)
    cells = found["pairs"]["EUR.USD"]["cells"]
    assert sum(cell["windows"] for cell in cells) > 0
    assert all(cell["paying"] == 0 and cell["unfillable"] == cell["windows"] and not cell["viable"] for cell in cells)
    assert found["placeholder"] is True and found["sources"]["placeholder_size_days"] == ["2025-10-09"]
    assert "PLACEHOLDER sizes" in err


# ---- the feasibility map ---------------------------------------------------------------------------------------------


def test_the_feasibility_map_prints_the_same_split_of_its_days_and_its_events(tmp_path, capsys):
    record(tmp_path / "rec", weeks(1, 4, on=(0, 1)))
    history(tmp_path, "a", weeks(2, 4, on=(1, 4)))  # one date the recording has too, and one it has not
    common = [*SHIPPED[:4], "--config", SHIPPED[5]]
    stores = ["--store", str(tmp_path / "rec"), "--imports", str(tmp_path / "hist")]
    assert feasibility_cli.main([*stores, *common]) == 0
    out, err = capsys.readouterr()
    found = json.loads(out)
    assert found["days"] == ["2025-10-09", "2025-10-10", "2025-10-13"]  # the date both hold is read from the recording
    assert found["sources"] == {"days": {A: 1, "recorded": 2}, "events": {A: 120, "recorded": 240},
                                "placeholder_size_days": []}  # fmt: skip
    assert found["stream"]["events"] == 360 and "by source" in err and A in err


# ---- a training run --------------------------------------------------------------------------------------------------


def go(folder, *more, store, **changes) -> int:
    config = learn_settings(folder, **changes)
    registry = folder / "registry.yaml"
    registry.write_text("history: []\n")
    argv = ["--config", str(config), *SHIPPED, "--store", str(store), "--trial-registry", str(registry)]
    return train.main([*argv, *more], SimClock(SATURDAY))


def the_report(folder) -> dict:
    (path,) = (folder / "learn" / "reports").glob("*.json")
    return json.loads(path.read_bytes())


def test_a_report_says_for_every_result_how_many_rows_were_recorded_and_how_many_imported_by_label(tmp_path, capsys):
    """Two imported weeks and then one recorded: the folds test an imported week and the recorded one."""
    record(tmp_path / "rec", weeks(1, on=(11, 12)))
    history(tmp_path, "a", weeks(2, on=(0, 1, 4, 5, 11)))  # 2025-10-20 is recorded too: the import of it is left out
    assert go(tmp_path, "--imports", str(tmp_path / "hist"), store=tmp_path / "rec") == train.EXIT_OK
    report, err = the_report(tmp_path), capsys.readouterr().err
    sources, made = report["sources"], report["dataset"]["sources"]
    assert set(sources) == {"three_way", "interval", "deflated_sharpe", "hold_out", "model"}
    assert sources["model"]["days"] == {A: 4, "recorded": 2} == made["split"]["days"]
    assert sum(sources["model"]["rows"].values()) == report["model"]["fitted_rows"]
    assert sources["three_way"]["days"] == {A: 2, "recorded": 2}  # the second week and the third; never the first
    assert sum(sources["three_way"]["rows"].values()) == report["three_way"]["rows"]
    assert sources["interval"] == sources["deflated_sharpe"] == sources["three_way"]
    assert sources["hold_out"] == {"rows": {}, "days": {}} and report["hold_out"]["read"] == 0
    assert [day[:2] for day in made["days"]] == [
        ["2025-10-09", A], ["2025-10-10", A], ["2025-10-13", A], ["2025-10-14", A],
        ["2025-10-20", "recorded"], ["2025-10-21", "recorded"],
    ]  # fmt: skip
    assert report["placeholders"]["sizes"] == 0
    assert f"fitted: {A} {sources['model']['rows'][A]} row(s) of 4 day(s), recorded" in err
    assert "1 imported day(s) left out, their dates are recorded too: 2025-10-20" in err
    said = json.loads((tmp_path / "learn" / train.LAST_RUN).read_text())
    assert said["sources"]["days"] == {A: 4, "recorded": 2} and said["imported_days_left_out"] == ["2025-10-20"]
    settings = load(tmp_path / "learn.yaml")
    model = parse((settings.models / f"{report['model']['hash']}.json").read_bytes())
    assert model.dataset_hash == report["dataset"]["content_hash"]  # the model is named by a dataset that says so
    assert model.sources == sources["model"], "and the model file says the split itself, as its report does"
    assert f"says in its own file that it was fitted to: {A} {sources['model']['rows'][A]} row(s) of 4 day(s)" in err


def test_the_same_quotes_recorded_and_imported_are_two_datasets_two_reports_and_two_models(tmp_path):
    stream = weeks(1)
    record(tmp_path / "ours" / "rec", stream)
    run(tmp_path / "theirs" / "hist", exported(tmp_path / "in", stream), "vendor-a")
    assert go(tmp_path / "ours", store=tmp_path / "ours" / "rec") == train.EXIT_OK
    assert go(tmp_path / "theirs", "--imports", str(tmp_path / "theirs" / "hist"), store=tmp_path / "none") == 0
    ours, theirs = the_report(tmp_path / "ours"), the_report(tmp_path / "theirs")
    assert ours["three_way"] == theirs["three_way"] and ours["dataset"]["rows"] == theirs["dataset"]["rows"]
    assert ours["dataset"]["content_hash"] != theirs["dataset"]["content_hash"]
    assert ours["model"]["hash"] != theirs["model"]["hash"]
    assert set(ours["sources"]["model"]["days"]) == {"recorded"} and set(theirs["sources"]["model"]["days"]) == {A}


def test_a_run_that_read_a_day_without_sizes_says_it_is_not_a_result(tmp_path):
    record(tmp_path / "rec", weeks(1, on=(0, 1, 4, 5)))
    history(tmp_path, "a", weeks(2, on=(11, 12)), sizes=False)
    assert go(tmp_path, "--imports", str(tmp_path / "hist"), store=tmp_path / "rec") == train.EXIT_OK
    report = the_report(tmp_path)
    assert report["placeholders"]["sizes"] == 1 and report["not_a_result"] == 1
    assert report["dataset"]["sources"]["placeholder_sizes"] == 1


# ---- the weekly decision ---------------------------------------------------------------------------------------------


def challenger_from(tmp_path, name: str, *more, store) -> test_decide.World:
    """A world whose challenger is the candidate a real training run registered, after a week that passes the rule."""
    folder = tmp_path / name
    assert go(folder, *more, store=store) == train.EXIT_OK
    (candidate,) = read_lineup(folder / "trader" / "lineup.jsonl").candidates
    world = test_decide.World(folder)
    world.b = candidate
    world.lineup(Lineup(champion=world.a, challengers=(candidate,)))
    world.good_week()
    return world


def test_a_model_trained_only_on_imports_cannot_satisfy_the_promotion_rule_s_data_requirement(tmp_path):
    """The same quotes, the same kind of model, the same good week in shadow: recorded is promoted, imported not."""
    stream = weeks(1)
    record(tmp_path / "rec", stream)
    run(tmp_path / "hist", exported(tmp_path / "in", stream), "vendor-a")
    imported = challenger_from(tmp_path, "theirs", "--imports", str(tmp_path / "hist"), store=tmp_path / "none")
    recorded = challenger_from(tmp_path, "ours", store=tmp_path / "rec")
    verdicts = {}
    for name, world in (("imported", imported), ("recorded", recorded)):
        report = json.loads((world.folder / "learn" / "reports" / f"{world.b.report}.json").read_bytes())
        fitted = {world.b.model: fitted_days(report)}
        _, evidence = read_week(world.folder / "trader", test_decide.SUNDAY)
        # every other gate open, and the report's placeholders set aside: what is left is the rule and the data
        gates = Gates(True, True, True, {}, fitted)
        before = read_lineup(world.path)
        verdicts[name] = weigh(before, evidence, world.desk.held, world.learn.rule, gates, "control")
        assert fitted[world.b.model] == ({A: 6} if name == "imported" else {"recorded": 6})
        assert gates.own_days(world.b.model) == (0 if name == "imported" else 6)
    assert verdicts["recorded"].action == "promote" and verdicts["recorded"].promoted == recorded.b
    assert (verdicts["imported"].action, verdicts["imported"].reasons) == ("keep", ("too_few_own_days",))
    assert verdicts["imported"].challengers[0].evidence["own_days"] == 0
    # and the command itself, with nothing set aside, keeps it and says how many days came from where
    outcome = imported.decide()
    assert outcome.action == "keep" and "too_few_own_days" in outcome.reasons
    kept = json.loads((imported.learn.reports / f"{imported.last()['report']}.json").read_bytes())
    assert kept["fitted_days_by_source"] == {imported.b.model: {A: 6}}


def test_a_report_that_does_not_say_where_its_days_came_from_has_no_recorded_day():
    days_said = ({"recorded": "6"}, ["recorded"], {"recorded": True}, {"recorded": -1}, None)
    silent = [{}, {"sources": None}, {"sources": {"model": {"rows": {"recorded": 9}}}}]
    for report in [*silent, *({"sources": {"model": {"days": said}}} for said in days_said)]:
        assert fitted_days(report).get("recorded", 0) == 0
    assert fitted_days({"sources": {"model": {"days": {"recorded": 3, A: 40}}}}) == {A: 40, "recorded": 3}
    assert T0  # the planted streams above start there


def test_the_weekly_scorecard_s_training_line_counts_the_fitted_rows_that_were_not_recorded():
    rows = {"recorded": 60, A: 30, UNSTATED: 10}
    said = {"kind": "evaluation-report", "pair": "EUR.USD", "horizon_ns": 60 * 10**9, "trials": 1, "not_a_result": 0,
            "model": {"kind": "ridge", "fitted_rows": 100}, "sources": {"model": {"rows": rows}}}  # fmt: skip
    assert scorecard.training(said).not_recorded_rows == 40
    assert scorecard.training({**said, "sources": {"model": {"rows": {"recorded": 100}}}}).not_recorded_rows == 0
    assert scorecard.training({key: value for key, value in said.items() if key != "sources"}).not_recorded_rows == 0


def test_a_shadow_run_over_imported_days_says_so_in_its_journal_and_never_says_recording(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    history(tmp_path, "a", planted(1, 4))
    (day,) = days(tmp_path / "hist")
    assert shadow_cli.main(["--day", str(day), "--config", shadow_config(tmp_path)]) == 0
    out, err = capsys.readouterr()
    assert f"over history:2025-10-09={A}" in err and "IMPORTED history" in err and "recording:" not in err
    first = json.loads(Path(json.loads(out)["journal"]).read_text().splitlines()[0])
    assert first["type"] == "start" and first["source"] == f"history:2025-10-09={A}"
