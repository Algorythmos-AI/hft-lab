"""The weekend training run: the same inputs give the same bytes, and nothing is registered that should not be."""
from __future__ import annotations

import json
import tracemalloc

import pytest

from hftlab.core import SimClock
from hftlab.journal import read
from hftlab.learn import train
from hftlab.learn.config import LearnConfigError, load
from hftlab.lineup import read as read_lineup
from hftlab.models import ModelRegistry, parse
from tests.learn.kit import DAY, SATURDAY, WIN, dataset, learn_settings, planted, record, weeks
from tests.support import PLACEHOLDER, ROOT
from tests.venues.kit import T0

SHIPPED = ["--cost-model", str(PLACEHOLDER / "cost_model.yaml"), "--calendar", str(ROOT / "config/calendar"),
           "--feasibility", str(ROOT / "config/feasibility.yaml")]  # fmt: skip
GB = 1_000_000_000


def go(folder, *more, at_ns: int = SATURDAY, store=None, registry=None, measure=train.peak_bytes, **changes) -> int:
    config = learn_settings(folder, **changes)
    found = registry or folder / "registry.yaml"
    if registry is None:
        found.write_text("history: []\n")
    argv = ["--config", str(config), *SHIPPED, "--store", str(store or folder / "rec"), "--trial-registry", str(found)]
    return train.main([*argv, *more], SimClock(at_ns), measure)


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    """One run over three synthetic weeks with the effect planted, and what it left behind."""
    folder = tmp_path_factory.mktemp("trained")
    record(folder / "rec", weeks(1))
    assert go(folder) == train.EXIT_OK
    return folder


def the_report(folder) -> dict:
    (path,) = (folder / "learn" / "reports").glob("*.json")
    return json.loads(path.read_bytes())


# ---- what a run leaves ---------------------------------------------------------------------------------------------


def test_a_run_keeps_a_model_and_a_report_under_their_hashes_and_registers_the_candidate(trained):
    settings = load(trained / "learn.yaml")
    (digest,) = ModelRegistry(settings.models).hashes()
    (path,) = settings.reports.glob("*.json")
    report = json.loads(path.read_bytes())
    model = parse((settings.models / f"{digest}.json").read_bytes())
    assert report["model"]["hash"] == digest == model.content_hash and model.kind == "ridge"
    assert model.dataset_hash == report["dataset"]["content_hash"] and model.pair == "EUR.USD"
    lineup = read_lineup(settings.lineup)
    assert [(one.model, one.report) for one in lineup.candidates] == [(digest, path.stem)]
    assert lineup.champion is None and lineup.challengers == (), "a candidate serves nowhere until a decision"
    said = json.loads((settings.state_dir / train.LAST_RUN).read_text())
    assert said["ok"] is True and said["at_ns"] == SATURDAY and said["candidates"][0]["model"] == digest
    assert said["days"] == sorted(said["days"]) and len(said["days"]) == 6


def test_the_report_holds_the_three_numbers_the_interval_the_deflated_sharpe_and_what_it_was_priced_on(trained):
    report = the_report(trained)
    three = report["three_way"]
    assert three["oracle"]["hindsight"] is True
    assert three["oracle"]["total_micro"] >= three["model"]["total_micro"] > 0 > three["random"]["total_micro"]
    assert 0 < three["model"]["wins"] <= three["model"]["trades"] < three["random"]["trades"], "it trades the signal"
    assert three["model"]["mean_micro"] <= WIN and three["no_trade"]["trades"] == 0
    assert three["rows"] == sum(fold["test_rows"] for fold in report["plan"]["folds"])
    assert report["interval"]["lower_micro"] <= report["interval"]["point_micro"] <= report["interval"]["upper_micro"]
    assert report["interval"]["days"] == 4, "the days of the two test weeks: a fold never tests the first week"
    assert report["trials"] == 1 == report["deflated_sharpe"]["trials"], "this configuration, counted by the ledger"
    assert isinstance(report["deflated_sharpe"]["ratio"], str), "a float is kept as the text that reads back to it"
    assert report["rows"] == {"dropped": report["dataset"]["dropped"], "counted_by_side": report["dataset"]["counted"]}
    assert set(report["rows"]["counted_by_side"]) == {"no_entry", "late_exit", "penalised"}
    assert report["placeholders"] == {"cost_model": 1, "feasibility": 1, "learn": 1, "sizes": 0}
    assert report["not_a_result"] == 1
    assert report["serving"]["feasibility"]["placeholder"] is True
    assert [fold["purged_rows"] for fold in report["plan"]["folds"]] == [0, 0] and len(report["plan"]["weeks"]) == 3
    assert report["config"]["exit_wait_ns"] == 2 * 10**9 and report["dataset"]["label"]["stuck_increments"] == 40
    assert "spread@v1" in report["summary"]["columns"] and "expected_micro" in report["summary"]["columns"]


def test_the_trial_count_is_the_larger_of_the_ledgers_and_the_signed_registrys(tmp_path, trained):
    signed = tmp_path / "registry.yaml"
    signed.write_text("history:\n  - {decision: HFT-DEC-0001, total: 7, status: accepted}\n")
    assert go(tmp_path, store=trained / "rec", registry=signed) == train.EXIT_OK
    assert the_report(tmp_path)["trials"] == 7 and the_report(tmp_path)["deflated_sharpe"]["trials"] == 7
    assert the_report(tmp_path)["model"] == the_report(trained)["model"], "the count deflates a statistic, not the fit"


def test_the_same_inputs_give_the_same_model_hash_and_the_same_report_bytes(tmp_path, trained):
    assert go(tmp_path, store=trained / "rec") == train.EXIT_OK
    first, second = (sorted((folder / "learn").rglob("*.json")) for folder in (trained, tmp_path))
    names = [path.name for path in first if path.name != train.LAST_RUN]
    assert names == [path.name for path in second if path.name != train.LAST_RUN] and len(names) == 2
    for one, other in zip(first, second, strict=True):
        if one.name != train.LAST_RUN:
            assert one.read_bytes() == other.read_bytes(), "a file is named by its hash, and the hash is of every byte"
    before = (tmp_path / "trader" / "lineup.jsonl").read_bytes()
    ledger = len(read(tmp_path / "learn" / "trials.jsonl")[1])
    assert go(tmp_path, store=trained / "rec") == train.EXIT_OK, "and again, into the same folders"
    assert (tmp_path / "trader" / "lineup.jsonl").read_bytes() == before, "the candidate is registered once"
    assert sorted(path.name for path in (tmp_path / "learn").rglob("*.json")) == sorted(path.name for path in second)
    assert len(read(tmp_path / "learn" / "trials.jsonl")[1]) == ledger + 1, "evaluated again: written down again"
    assert the_report(tmp_path)["trials"] == 1, "and still one trial: the same frozen configuration"


# ---- what it refuses -----------------------------------------------------------------------------------------------


def test_it_refuses_to_run_while_the_market_is_open_unless_it_is_a_dry_run(tmp_path, trained, capsys):
    assert go(tmp_path, at_ns=T0, store=trained / "rec") == train.EXIT_REFUSED
    assert "the market is open" in capsys.readouterr().err and not (tmp_path / "learn").exists()
    assert go(tmp_path, "--dry-run", at_ns=T0, store=trained / "rec") == train.EXIT_OK
    printed = capsys.readouterr()
    assert json.loads(printed.out) == the_report(trained), "a dry run prints the report a run would keep"
    assert "nothing registered" in printed.err
    assert sorted(path.name for path in (tmp_path / "learn").iterdir()) == ["trials.jsonl"], "the trial is counted"
    assert not (tmp_path / "trader").exists(), "no model, no report, no candidate"


def test_a_store_with_too_few_weeks_to_make_a_fold_is_refused_and_registers_nothing(tmp_path, capsys):
    record(tmp_path / "rec", weeks(1, on=(0, 1)))
    assert go(tmp_path) == train.EXIT_REFUSED
    assert "make no fold" in capsys.readouterr().err and not (tmp_path / "learn" / "models").exists()
    assert go(tmp_path, store=tmp_path / "nothing") == train.EXIT_REFUSED


def test_a_run_that_peaked_above_its_memory_budget_fails_and_registers_nothing(tmp_path, trained, capsys):
    assert go(tmp_path, store=trained / "rec", measure=lambda: 3 * GB) == train.EXIT_FAILED
    assert "over the budget" in capsys.readouterr().err
    said = json.loads((tmp_path / "learn" / train.LAST_RUN).read_text())
    assert said["ok"] is False and said["memory"] == {"peak_bytes": 3 * GB, "budget_bytes": GB, "within": False}
    assert not (tmp_path / "learn" / "models").exists() and not (tmp_path / "trader").exists()


def test_settings_with_a_key_missing_or_a_number_that_is_not_whole_are_refused(tmp_path, trained):
    assert go(tmp_path, store=trained / "rec", ridge_penalty="0") == train.EXIT_REFUSED
    assert go(tmp_path, store=trained / "rec", label={"stuck_increments": None}) == train.EXIT_REFUSED, "no default"
    (tmp_path / "bad.yaml").write_text("version: 1\nsurprise: 2\n")
    with pytest.raises(LearnConfigError, match=r"not keys of this file: \['surprise'\]; missing"):
        load(tmp_path / "bad.yaml")
    shipped = load(ROOT / "config/learn.yaml")
    assert shipped.placeholder and not shipped.rule_in_force and shipped.rule.decision is None
    assert not shipped.drift.in_force and shipped.memory_budget_bytes <= 2 * GB


# ---- the hold-out --------------------------------------------------------------------------------------------------


def test_a_held_out_week_is_in_no_fold_no_fit_and_no_summary(tmp_path):
    """Two stores that agree on every week but the last. With that week held out, nothing but its hash differs."""
    reports = []
    for name, last in (("one", 5), ("other", 9)):
        record(tmp_path / name / "rec", weeks(1, last=last))
        assert go(tmp_path / name, validation={"hold_out_weeks": 1}) == train.EXIT_OK
        reports.append(the_report(tmp_path / name))
    one, other = reports
    assert one["dataset"]["content_hash"] != other["dataset"]["content_hash"], "the stores do differ"
    for key in ("three_way", "interval", "plan", "summary", "serving", "hold_out", "deflated_sharpe"):
        assert one[key] == other[key], key
    assert one["hold_out"] == {"weeks": 1, "read": 0} and len(one["plan"]["held_out"]) == 1
    files = [next((tmp_path / name / "learn" / "models").glob("*.json")) for name in ("one", "other")]
    models = [parse(path.read_bytes()) for path in files]
    assert models[0].params == models[1].params, "the candidate was fitted to the same rows"
    assert models[0].dataset_hash != models[1].dataset_hash


def test_the_hold_out_is_read_once_through_the_ledger_and_a_second_read_is_refused(tmp_path, capsys):
    record(tmp_path / "rec", weeks(1))
    held = {"validation": {"hold_out_weeks": 1}}
    assert go(tmp_path, "--hold-out", **held) == train.EXIT_OK
    report = the_report(tmp_path)
    assert report["hold_out"]["read"] == 1 and report["hold_out"]["model"]["total_micro"] > 0
    kinds = [entry["what"] for entry in read(tmp_path / "learn" / "trials.jsonl")[1]]
    assert kinds == ["walk_forward", "hold_out"], "the read was written down before it happened"
    assert go(tmp_path, "--hold-out", **held) == train.EXIT_REFUSED
    assert "already read hold-out" in capsys.readouterr().err
    assert go(tmp_path, **held) == train.EXIT_OK, "without the read it runs as before"


# ---- memory --------------------------------------------------------------------------------------------------------


def peak_of(blocks: int) -> tuple[int, int]:
    stream = planted(3, blocks)
    tracemalloc.start()
    made = dataset(stream, checks=4)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak, len(made.rows)


def test_the_dataset_builders_memory_grows_no_faster_than_the_number_of_rows():
    (small, few), (large, many) = peak_of(100), peak_of(400)
    assert 3.9 < many / few < 4.1, "four times the rows"
    assert large < 5 * small, "four times the memory and a margin: twice as fast as the rows would be eight"


def test_a_synthetic_week_is_built_and_trained_far_inside_the_hosts_cap(tmp_path, capsys):
    """Five trading days, a burst of the planted stream on each, and a week before them to train on."""
    record(tmp_path / "rec", weeks(2, blocks=40, on=(0, 4, 5, 6, 7, 8)))
    tracemalloc.start()
    assert go(tmp_path) == train.EXIT_OK
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    said = json.loads((tmp_path / "learn" / train.LAST_RUN).read_text())
    with capsys.disabled():
        print(f"\n  synthetic week: {said['events']} events, {said['rows']} rows; peak traced {peak} bytes "
              f"({peak // said['rows']} per row); process peak {said['memory']['peak_bytes']} bytes")
    assert peak < 2 * GB and said["memory"]["within"] is True and said["memory"]["peak_bytes"] > 0
    assert said["events"] == 6 * 40 * 30 and DAY
