"""The staged rollout: a sleeve goes up by its rule and by nothing else, and comes down at once."""
from __future__ import annotations

import pytest

from hftlab.calendar import Calendar, Closure, last_rollover
from hftlab.journal import Journal, read, verify
from hftlab.trader import Rollout, Rule, Stages
from hftlab.trader.stages import DAY_NS, NEVER, StageError, day_of, is_trading_day
from tests.venues.kit import SECOND, T0

MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
MONDAY = last_rollover(T0) - 3 * DAY_NS  # T0 is a Thursday morning: Sunday's rollover, which opens Monday's trading day
EARNED = Rollout("shadow", Rule(2, "HFT-DEC-0007"), Rule(3, "HFT-DEC-0008"))


def stages(tmp_path, rollout=None, *, in_force=True, min_day_ns: int | None = 20 * HOUR, calendar=None) -> Stages:
    """The stage journal as it stands, opened the way a start opens it."""
    ladders = {"control": EARNED} if rollout is None else rollout
    journal = Journal(tmp_path / "stages.jsonl")
    return Stages(journal, ladders, calendar=calendar or Calendar(), in_force=in_force, min_day_ns=min_day_ns)


def a_day(found: Stages, opened: int, *, hours: int = 23, latched: bool = False, broken: bool = False, during=None):
    """One trading day: a session from its opening rollover, whatever `during` does to it, and its close."""
    found.begin(opened + MINUTE)
    if during is not None:
        during(found, opened + HOUR)
    close = day_of(opened + MINUTE)
    at = opened + MINUTE + hours * HOUR
    changes = found.close_day(close, max(at, close), latched=latched, broken=broken) if hours >= 23 else []
    found.end(max(at, close) if hours >= 23 else at)
    return changes


def entries(tmp_path, kind: str) -> list[dict]:
    return [entry for entry in read(tmp_path / "stages.jsonl")[1] if entry["type"] == kind]


# ---- no default promotes anything -----------------------------------------------------------------------------------


def test_a_rule_with_its_number_or_its_decision_missing_is_never_met(tmp_path):
    for number, rule in enumerate((NEVER, Rule(1, None), Rule(None, "HFT-DEC-0001"))):
        found = stages(tmp_path / str(number), {"control": Rollout("shadow", rule, rule)})
        for day in range(4):
            assert a_day(found, MONDAY + day * DAY_NS) == []
        assert found.stage("control") == "shadow" and found.clean_run("control") == 4
        folder = tmp_path / str(number)
        assert entries(folder, "stage") == [] and entries(folder, "held") == []
    assert Rollout() == Rollout("shadow", NEVER, NEVER), "and a ladder nobody wrote is that rule twice"


def test_a_ladder_cannot_start_above_shadow_and_a_rule_cannot_be_met_by_zero_days():
    for start in ("min_size", "normal", "stepped_down", "paper"):
        with pytest.raises(StageError, match="starts in shadow or off"):
            Rollout(start)
    assert Rollout("off").start == "off"
    for days in (0, -1, True, 1.0, "3"):
        with pytest.raises(StageError, match="clean_days"):
            Rule(days, "HFT-DEC-0001")
    for record in ("HFT-EXP-0001", "DEC-0001", "yes", 7):
        with pytest.raises(StageError, match="decision"):
            Rule(3, record)


def test_with_no_coverage_set_no_day_counts(tmp_path):
    found = stages(tmp_path, min_day_ns=None)
    for day in range(4):
        assert a_day(found, MONDAY + day * DAY_NS) == []
    assert found.clean_run("control") == 0
    assert {tuple(e["causes"]) for e in entries(tmp_path, "day")} == {("TOO_SHORT",)}


# ---- up, by the rule -------------------------------------------------------------------------------------------------


def test_a_sleeve_goes_up_one_step_after_its_clean_days_and_the_journal_names_the_decision(tmp_path):
    found = stages(tmp_path)
    assert a_day(found, MONDAY) == [] and found.stage("control") == "shadow"
    (change,) = a_day(found, MONDAY + DAY_NS)
    assert (change.sleeve, change.before, change.to, change.cause, change.detail) == (
        "control", "shadow", "min_size", "PROMOTED", "HFT-DEC-0007",
    )  # fmt: skip
    assert found.clean_run("control") == 0, "the days in shadow do not count towards the next step"
    assert a_day(found, MONDAY + 2 * DAY_NS) == [] and a_day(found, MONDAY + 3 * DAY_NS) == []
    assert found.stage("control") == "min_size" and found.clean_run("control") == 2
    again = stages(tmp_path)  # a restart reads the journal
    assert again.stage("control") == "min_size" and again.clean_run("control") == 2
    assert verify(tmp_path / "stages.jsonl").entries > 10


def test_a_step_up_is_refused_and_the_refusal_journalled_while_the_limits_are_not_in_force(tmp_path):
    found = stages(tmp_path, in_force=False)
    for day in range(3):
        assert a_day(found, MONDAY + day * DAY_NS) == []
    assert found.stage("control") == "shadow" and found.clean_run("control") == 3
    assert [(e["to"], e["cause"]) for e in entries(tmp_path, "held")] == [("min_size", "LIMITS_NOT_IN_FORCE")] * 2
    assert entries(tmp_path, "stage") == []


def test_a_step_up_waits_while_a_latch_is_tripped_or_a_break_stands(tmp_path):
    found = stages(tmp_path)
    a_day(found, MONDAY)
    assert a_day(found, MONDAY + DAY_NS, latched=True) == []
    assert a_day(found, MONDAY + 2 * DAY_NS, broken=True) == []
    assert [e["cause"] for e in entries(tmp_path, "held")] == ["STILL_LATCHED", "STILL_BROKEN"]
    assert len(a_day(found, MONDAY + 3 * DAY_NS)) == 1


@pytest.mark.parametrize("cause", ["LATCH", "BREAK"])
def test_a_day_in_which_something_went_wrong_is_not_clean_and_the_count_starts_again(tmp_path, cause):
    found = stages(tmp_path)
    a_day(found, MONDAY)
    assert a_day(found, MONDAY + DAY_NS, during=lambda s, at: s.mark(at, cause)) == []
    assert entries(tmp_path, "day")[-1]["causes"] == [cause] and found.clean_run("control") == 0
    assert a_day(found, MONDAY + 2 * DAY_NS) == []
    assert len(a_day(found, MONDAY + 3 * DAY_NS)) == 1


def test_what_went_wrong_for_another_sleeve_does_not_spoil_this_ones_day(tmp_path):
    found = stages(tmp_path, {"control": EARNED, "ml": EARNED})
    a_day(found, MONDAY, during=lambda s, at: s.mark(at, "LATCH", "ml"))
    days = {e["sleeve"]: e["clean"] for e in entries(tmp_path, "day")}
    assert days == {"control": 1, "ml": 0}


def test_a_day_the_trader_ran_too_little_of_or_was_killed_in_or_never_saw_the_close_of_is_not_clean(tmp_path):
    found = stages(tmp_path)
    found.begin(MONDAY + 10 * HOUR)  # started late: fourteen hours of a day that needs twenty
    found.close_day(day_of(MONDAY + HOUR), MONDAY + DAY_NS, latched=False, broken=False)
    found.end(MONDAY + DAY_NS)
    assert entries(tmp_path, "day")[-1]["causes"] == ["TOO_SHORT"]

    found.begin(MONDAY + DAY_NS + MINUTE)  # this session never says it stopped: the process was killed
    found = stages(tmp_path)
    found.begin(MONDAY + DAY_NS + 2 * HOUR)
    found.close_day(day_of(MONDAY + DAY_NS + HOUR), MONDAY + 2 * DAY_NS, latched=False, broken=False)
    found.end(MONDAY + 2 * DAY_NS)
    assert entries(tmp_path, "day")[-1]["causes"] == ["UNCLEAN_STOP"]
    assert entries(tmp_path, "dirty")[-1]["cause"] == "UNCLEAN_STOP"

    found = stages(tmp_path)  # stopped over Wednesday's close, started again on Thursday
    found.begin(MONDAY + 3 * DAY_NS + MINUTE)
    missed = entries(tmp_path, "day")[-1]
    assert (missed["day"], missed["causes"], missed["clean"]) == (MONDAY + 3 * DAY_NS, ["NOT_RUNNING"], 0)
    assert found.clean_run("control") == 0


def test_sessions_of_one_day_add_up_and_a_weekend_is_not_a_day_at_all(tmp_path):
    found = stages(tmp_path)
    found.begin(MONDAY + MINUTE)
    found.end(MONDAY + 11 * HOUR)  # stopped on purpose, for a deploy
    found.begin(MONDAY + 12 * HOUR)
    assert found.close_day(day_of(MONDAY + HOUR), MONDAY + DAY_NS, latched=False, broken=False) == []
    found.end(MONDAY + DAY_NS)
    assert entries(tmp_path, "day")[-1]["clean"] == 1 and entries(tmp_path, "day")[-1]["ran_ns"] > 22 * HOUR
    close = MONDAY + 5 * DAY_NS  # Friday 17:00 New York: the rollover that closes the week
    assert is_trading_day(Calendar(), close) and not is_trading_day(Calendar(), close + DAY_NS)
    assert a_day(found, close) == []
    assert close + DAY_NS not in {e["day"] for e in entries(tmp_path, "day")}, "Saturday is neither clean nor not"
    holiday = Calendar(closures=(Closure("a holiday", MONDAY, MONDAY + DAY_NS),))
    assert not is_trading_day(holiday, MONDAY + DAY_NS)


def test_a_weekend_does_not_break_a_run_of_clean_days(tmp_path):
    found = stages(tmp_path, {"control": Rollout("shadow", Rule(3, "HFT-DEC-0007"))})
    thursday = MONDAY + 3 * DAY_NS  # the rollover that opens Thursday's trading day
    a_day(found, thursday)
    a_day(found, thursday + DAY_NS)  # Friday's, which closes with the week
    sunday = thursday + 4 * DAY_NS
    assert len(a_day(found, sunday)) == 1, "Thursday, Friday and Monday are three trading days in a row"


# ---- down, at once ---------------------------------------------------------------------------------------------------


def test_a_latch_or_a_break_steps_a_paper_sleeve_down_at_once_and_only_a_paper_sleeve(tmp_path):
    found = stages(tmp_path)
    assert found.step_down("control", "LATCH", "ORDER_FAULT", MONDAY + HOUR) is None, "nothing to step in shadow"
    a_day(found, MONDAY)
    a_day(found, MONDAY + DAY_NS)
    assert found.stage("control") == "min_size" and found.above_shadow() == ["control"]
    change = found.step_down("control", "BREAK", "", MONDAY + 2 * DAY_NS + HOUR)
    assert (change.before, change.to, change.cause) == ("min_size", "stepped_down", "BREAK")
    assert found.step_down("control", "LATCH", "ORDER_FAULT", MONDAY + 2 * DAY_NS + 2 * HOUR) is None
    assert stages(tmp_path).stage("control") == "stepped_down" and found.above_shadow() == ["control"]


def test_the_way_back_from_stepped_down_is_the_first_rule_again_counted_from_the_step_down(tmp_path):
    found = stages(tmp_path)
    a_day(found, MONDAY)
    a_day(found, MONDAY + DAY_NS)
    a_day(found, MONDAY + 2 * DAY_NS, during=lambda s, at: s.step_down("control", "LATCH", "ORDER_FAULT", at))
    assert entries(tmp_path, "day")[-1]["causes"] == ["STAGE_CHANGED"] and found.stage("control") == "stepped_down"
    assert a_day(found, MONDAY + 3 * DAY_NS) == []
    (change,) = a_day(found, MONDAY + 4 * DAY_NS)  # the second clean day stepped down
    assert (change.before, change.to) == ("stepped_down", "min_size")


def test_an_operator_can_take_a_sleeve_down_and_never_up(tmp_path):
    found = stages(tmp_path)
    for to in ("min_size", "normal", "stepped_down", "shadow", "paper"):
        assert found.demote("control", to, "owner", MONDAY + HOUR) is None, to
    change = found.demote("control", "off", "owner", MONDAY + HOUR)
    assert (change.to, change.cause, change.detail) == ("off", "OPERATOR", "owner")
    assert found.demote("control", "shadow", "owner", MONDAY + 2 * HOUR) is None, "off to shadow would be a step up"
    assert found.demote("nobody", "off", "owner", MONDAY + 2 * HOUR) is None
    for day in range(4):
        assert a_day(found, MONDAY + day * DAY_NS) == []
    assert stages(tmp_path).stage("control") == "off", "and no rule leads out of off"


def test_the_trading_day_is_named_by_the_new_york_rollover_that_closes_it():
    opened = last_rollover(T0)
    assert day_of(T0) == day_of(opened) == day_of(opened + DAY_NS - 1) == last_rollover(T0 + DAY_NS)
    assert day_of(opened + DAY_NS) == last_rollover(T0 + 2 * DAY_NS)
