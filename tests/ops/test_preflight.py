"""The pre-flight gate: a day of samples decides whether the platform may be installed, and says why.

The gate protects two desks that are already in service, so what is tested is its refusals: too little
memory at the tightest minute, a day that is not a day, a disk that would leave the others under their floor,
and a file too old to speak for the host as it is now.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from hftlab.ops import preflight
from hftlab.ops.preflight import EXIT_FAIL, EXIT_PASS, EXIT_UNREADABLE, Sample, check, parse
from tests.ops.samples import START, a_day, line, recent, write
from tests.support import ROOT

CONFIG = ROOT / "config" / "preflight.json"
SHIPPED = preflight.load_thresholds(CONFIG)
NOW = START + 86_400 + 3_600  # an hour after the day of samples ended


def failing(report) -> list[str]:
    return [item.name for item in report.checks if not item.ok]


def with_one(samples: list[Sample], position: int, **numbers: int) -> list[Sample]:
    changed = list(samples)
    old = samples[position]
    fields = {name: getattr(old, name) for name in Sample.__dataclass_fields__}
    changed[position] = Sample(**{**fields, **numbers})
    return changed


# ---- the thresholds -------------------------------------------------------------------------------------------------


def test_the_shipped_thresholds_are_the_plans_numbers_and_each_says_where_it_comes_from():
    entries = json.loads(CONFIG.read_text())["thresholds"]
    assert set(entries) == set(preflight.THRESHOLDS)
    assert SHIPPED["headroom_min_kb"] == 1_500_000, "about 1.5 GB of headroom at the other desks' peak"
    assert SHIPPED["disk_floor_kb"] == 3_000_000, "the other desks' 3 GB free-disk floor"
    assert SHIPPED["span_min_seconds"] == 86_100, "one day"
    plan = {name for name, entry in entries.items() if entry["decided_by"] == "plan"}
    assert plan == {"headroom_min_kb", "disk_floor_kb", "span_min_seconds"}
    for name, entry in entries.items():
        assert entry["decided_by"] in ("plan", "engineering") and len(entry["source"]) > 20, name
        assert name not in plan or "ADR 0007" in entry["source"], f"{name} is the plan's and must cite it"


def test_the_disk_this_platform_may_take_covers_the_recording_quota_with_room_for_the_rest():
    quota = yaml.safe_load((ROOT / "config" / "recorder.yaml").read_text())["quota_bytes"]
    room_for_the_rest = 500_000_000  # the install, the journals and the logs
    assert SHIPPED["disk_budget_kb"] * 1024 >= quota + room_for_the_rest


@pytest.mark.parametrize(
    "change",
    [{"headroom_min_kb": {"value": 1.5, "decided_by": "plan", "source": "x"}},
     {"headroom_min_kb": {"value": 0, "decided_by": "plan", "source": "x"}},
     {"headroom_min_kb": {"value": 1, "decided_by": "nobody", "source": "x"}},
     {"headroom_min_kb": {"value": 1, "decided_by": "plan", "source": " "}},
     {"headroom_min_kb": {"value": 1, "decided_by": "plan"}}, {"headroom_min_kb": 1_500_000},
     {"an_extra_one": {"value": 1, "decided_by": "plan", "source": "x"}}],
)  # fmt: skip
def test_a_threshold_without_a_whole_value_or_without_its_origin_is_refused(tmp_path, change):
    document = json.loads(CONFIG.read_text())
    document["thresholds"].update(change)
    (tmp_path / "preflight.json").write_text(json.dumps(document))
    with pytest.raises(preflight.PreflightError):
        preflight.load_thresholds(tmp_path / "preflight.json")
    with pytest.raises(preflight.PreflightError, match="cannot read"):
        preflight.load_thresholds(tmp_path / "missing.json")


# ---- the verdict ----------------------------------------------------------------------------------------------------


def test_a_day_with_room_to_spare_passes_and_the_report_gives_the_numbers():
    report = check(a_day(), 0, SHIPPED, NOW)
    assert report.passed and failing(report) == []
    assert report.numbers["samples"] == 1_440 and report.numbers["span_seconds"] == 1_439 * 60
    assert report.numbers["headroom_kb"] == 2_400_000 and report.numbers["disk_left_after_budget_kb"] == 11_000_000
    screen = preflight.render(report)
    assert screen.startswith("pre-flight: PASS: the platform may be installed")
    assert "least available 2,400,000 kB of 4,028,000 kB" in screen and "23 h 59 min" in screen


def test_headroom_is_the_least_memory_available_at_any_minute_and_just_under_the_line_is_not_installed():
    peak = 870  # 14:30 UTC: the other desks at their busiest
    at_the_line = check(with_one(a_day(), peak, mem_available_kb=1_500_000), 0, SHIPPED, NOW)
    assert at_the_line.passed and at_the_line.numbers["headroom_at_s"] == START + peak * 60
    under = check(with_one(a_day(), peak, mem_available_kb=1_499_999), 0, SHIPPED, NOW)
    assert not under.passed and failing(under) == ["memory headroom"]
    screen = preflight.render(under)
    assert screen.startswith("pre-flight: FAIL: the platform is NOT installed")
    assert "least available 1,499,999 kB" in screen and "at 2026-10-12T14:30:00Z" in screen
    assert "a larger host, or the route without a gateway process" in screen


def test_an_average_that_looks_fine_does_not_hide_one_tight_minute():
    samples = with_one(a_day(mem_available_kb=3_000_000), 700, mem_available_kb=900_000)
    assert failing(check(samples, 0, SHIPPED, NOW)) == ["memory headroom"]


@pytest.mark.parametrize(
    ("samples", "broken"),
    [(a_day(1_379), ["samples", "a full day"]),
     (a_day(1_380, every=60), ["a full day"]),
     (a_day(1_440, every=30), ["a full day"]),
     (a_day(700) + a_day(700, start=START + 44_400), ["longest hole"]),
     (a_day()[::-1], ["samples", "a full day"]),
     (a_day(1_439) + a_day(1)[:1], ["samples", "a full day"])],
    ids=["too few", "an hour short", "half a day sampled twice as often", "a hole of forty minutes", "backwards",
         "a clock that stepped back"],
)  # fmt: skip
def test_a_day_that_is_not_a_day_does_not_pass(samples, broken):
    report = check(samples, 0, SHIPPED, max(s.at_s for s in samples) + 3_600)
    assert not report.passed and failing(report) == broken


def test_a_hole_the_peak_could_hide_in_fails_even_when_the_count_and_the_span_are_enough():
    morning, evening = a_day(700), a_day(700, start=START + 44_400)
    report = check(morning + evening, 0, SHIPPED, NOW)
    assert report.numbers["longest_hole_seconds"] == 44_400 - 699 * 60 and failing(report) == ["longest hole"]
    ten_minutes_apart = a_day(145, every=600)  # a hole of exactly the limit is allowed
    assert failing(check(ten_minutes_apart, 0, {**SHIPPED, "samples_min": 100}, NOW)) == []


def test_the_disk_must_keep_the_other_desks_floor_after_this_platform_has_taken_its_share():
    enough = check(with_one(a_day(), 5, disk_free_kb=6_000_000), 0, SHIPPED, NOW)
    assert enough.passed and enough.numbers["disk_left_after_budget_kb"] == 3_000_000
    short = check(with_one(a_day(), 5, disk_free_kb=5_999_999), 0, SHIPPED, NOW)
    assert failing(short) == ["disk floor"]
    assert "less this platform's 3,000,000 kB leaves 2,999,999 kB" in preflight.render(short)


def test_samples_too_old_to_speak_for_the_host_do_not_pass_and_neither_do_samples_from_the_future():
    week = 7 * 86_400
    last = a_day()[-1].at_s
    assert check(a_day(), 0, SHIPPED, last + week).passed
    assert failing(check(a_day(), 0, SHIPPED, last + week + 1)) == ["how old"]
    assert failing(check(a_day(), 0, SHIPPED, last - 1)) == ["how old"]


def test_no_samples_at_all_is_a_fail_not_a_pass():
    report = check([], 3, SHIPPED, NOW)
    assert not report.passed and report.numbers == {"samples": 0, "unreadable_lines": 3}


def test_swap_and_load_are_reported_and_decide_nothing():
    heavy = check(a_day(load_centi=950, swap_free_kb=12), 0, SHIPPED, NOW)
    assert heavy.passed and heavy.numbers["load_max_centi"] == 950
    screen = preflight.render(heavy)
    assert "load average over one minute: highest 9.50" in screen and "swap: least free 12 kB" in screen
    assert "check that they did" in screen, "whether the other desks' peak is in the window is the owner's to confirm"


# ---- reading the file -----------------------------------------------------------------------------------------------


def test_the_samplers_lines_are_read_exactly_and_what_is_not_a_sample_is_counted():
    sample = Sample(START + 60, 1_843_200, 4_028_000, 0, 142, 14_200_000)
    text = "\n".join([
        "# hft-preflight-sample: 1440 samples, one every 60 s, from 2026-10-12T00:00:00Z",
        line(sample),
        "",
        "# a sample could not be taken at 2026-10-12T00:02:00Z",
        "2026-10-12T00:03:00Z mem_available_kb=lots mem_total_kb=4028000 swap_free_kb=0 load1=0.10 load5=0.10 "
        "load15=0.10 disk_free_kb=1",
        "2026-13-45T00:04:00Z mem_available_kb=1 mem_total_kb=2 swap_free_kb=0 load1=0.10 load5=0.10 load15=0.10 "
        "disk_free_kb=1",
        line(sample)[:60],
    ])  # fmt: skip
    assert parse(text) == ([sample], 3)
    assert parse("") == ([], 0)


# ---- the command ----------------------------------------------------------------------------------------------------


def test_the_command_exits_zero_on_a_pass_one_on_a_fail_and_two_when_it_cannot_read(tmp_path, capsys):
    assert preflight.main(["--check", write(tmp_path, recent()), "--config", str(CONFIG)]) == EXIT_PASS
    assert capsys.readouterr().out.startswith("pre-flight: PASS")
    tight = write(tmp_path, recent(mem_available_kb=1_200_000))
    assert preflight.main(["--check", tight, "--config", str(CONFIG)]) == EXIT_FAIL
    assert "[NO] memory headroom: least available 1,200,000 kB" in capsys.readouterr().out
    assert preflight.main(["--check", str(tmp_path / "nothing.log"), "--config", str(CONFIG)]) == EXIT_UNREADABLE
    assert preflight.main(["--check", tight, "--config", str(tmp_path / "nothing.json")]) == EXIT_UNREADABLE
    assert capsys.readouterr().err.count("cannot read") == 2


def test_the_report_as_json_carries_the_verdict_and_the_numbers(tmp_path, capsys):
    assert preflight.main(["--check", write(tmp_path, recent()), "--config", str(CONFIG), "--json"]) == EXIT_PASS
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "pass" and report["numbers"]["headroom_kb"] == 2_400_000
    assert [item["name"] for item in report["checks"]] == [
        "samples", "a full day", "longest hole", "how old", "memory headroom", "disk floor",
    ]  # fmt: skip


def test_it_runs_on_a_bare_python_with_no_installed_package(tmp_path):
    """It runs on the host before the platform's environment exists, so it may need the standard library only."""
    program = (
        "import sys; sys.path.insert(0, sys.argv[1]); from hftlab.ops import preflight; "
        "assert 'yaml' not in sys.modules; sys.exit(preflight.main(sys.argv[2:]))"
    )
    arguments = ["--check", write(tmp_path, recent()), "--config", str(CONFIG)]
    command = [sys.executable, "-S", "-c", program, str(ROOT / "src"), *arguments]
    done = subprocess.run(command, capture_output=True, text=True, check=False, cwd=tmp_path)
    assert done.returncode == EXIT_PASS and done.stdout.startswith("pre-flight: PASS"), done.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hft-preflight.log"], "it wrote nothing"
