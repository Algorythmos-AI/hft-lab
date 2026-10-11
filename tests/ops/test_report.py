"""The daily summary and the weekly scorecard: every number worked out by hand from journals written by hand.

The journals are synthetic and are written with the functions the platform writes them with. October 2025:
New York is four hours behind UTC, so the 17:00 rollover is 21:00 UTC and a week opens on Sunday at 21:15 UTC.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from hftlab import lineup
from hftlab.calendar import Calendar
from hftlab.core import SimClock
from hftlab.journal import Journal, canonical
from hftlab.learn.train import keep_report
from hftlab.lineup import Lineup, Named
from hftlab.ops import report
from hftlab.ops.report import Disk, Places, Span, daily, gather, weekly
from tests.support import ROOT

S = 1_000_000_000
USD = 1_000_000  # one dollar, in millionths


def utc(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    """A moment of October 2025, in UTC nanoseconds."""
    return int(datetime(2025, 10, day, hour, minute, second, tzinfo=UTC).timestamp()) * S


def gap(pair: str, start_ns: int, end_ns: int, reason: str) -> dict:
    return {"type": "gap", "pair": pair, "start_ns": start_ns, "end_ns": end_ns, "reason": reason}


TUESDAY = Span("2025-10-14", utc(13, 21), utc(14, 21))
WEEK = Span("2025-W42", utc(10, 21), utc(17, 21))
DAYS = tuple(Span(f"2025-10-{day}", utc(day - 1, 21), utc(day, 21)) for day in (13, 14, 15, 16, 17))
AFTER_FRIDAY = utc(17, 21, 10)  # ten minutes after the Friday close: when the unit runs
PRIVATE = ("DU1234567", "gw-host-17", "internal", "sam@laptop", "free text", "refused the login")

# ---- the recorder's journals -----------------------------------------------------------------------------------------

STORE = {
    "2025-10-12": [
        {"type": "start", "run": 1, "at_ns": utc(12, 21, 15), "next_index": 0},
        {"type": "instruments", "instruments": [{"pair": "EUR.USD"}, {"pair": "USD.JPY"}]},
    ],
    "2025-10-14": [
        gap("EUR.USD", utc(14, 2), utc(14, 2, 0, 30), "unexplained"),
        gap("USD.JPY", utc(14, 3), utc(14, 3, 10), "connection_lost"),
        {"type": "notice", "code": "2104", "message": "account DU1234567 at gw-host-17.internal refused the login",
         "at_ns": utc(14, 4)},
        {"type": "stop", "reason": "requested", "at_ns": utc(14, 10)},
        {"type": "start", "run": 2, "at_ns": utc(14, 10, 5), "next_index": 9},
        gap("EUR.USD", utc(14, 9, 59, 50), utc(14, 10, 5, 10), "process_down"),
        gap("USD.JPY", utc(14, 9, 59, 58), utc(14, 10, 5, 2), "process_down"),
        gap("USD.JPY", utc(11, 0), utc(12, 21, 15, 1), "market_closed"),
        {"type": "carry", "hour": 21, "at_ns": utc(14, 21, 0, 1)},
    ],
}  # fmt: skip


def store_lines() -> list[dict]:
    return [line for day in sorted(STORE) for line in STORE[day]]


# ---- the trader's journals -------------------------------------------------------------------------------------------

SLEEVES = ["idle", "control", "ml"]


def stop(control: int, ml: dict[str, int], broker: int | None, *, closed: int = 1) -> dict:
    """A session's last word: each sleeve's result of the day in the shadow book, and the desk's in both books."""
    desk = {code: amount + (control if code == "USD" else 0) for code, amount in ml.items()}
    paper = None if broker is None else {"net_today_micro": {"USD": broker}}
    books = {"shadow": {"net_today_micro": desk}, "broker": paper}
    nets = {"idle": {"USD": 0}, "control": {"USD": control}, "ml": ml}
    return {"summary": {"sleeves": [{"name": name, "net_today_micro": nets[name]} for name in SLEEVES], "books": books},
            "day_closed": closed}  # fmt: skip


def fills(journal: Journal, kind: str, sleeve: str, count: int) -> None:
    for number in range(count):
        journal.write(kind, sleeve=sleeve, fill_id=f"{kind}-{sleeve}-{journal.entries}-{number}", pair="EUR.USD")


def session(journal: Journal, stages: dict[str, str], mode: str = "paper") -> None:
    journal.write("start", sleeves=SLEEVES, stages=stages, mode=mode)


def write_tuesday(folder) -> None:
    """Two sessions: a latch, a step down, a break that ended, and the day's close seen by the second."""
    journal = Journal(folder / "days" / "2025-10-14" / "decisions.jsonl")
    journal.open()
    session(journal, {"idle": "shadow", "control": "min_size", "ml": "shadow"})
    fills(journal, "shadow_fill", "control", 4)
    fills(journal, "broker_fill", "control", 4)
    fills(journal, "shadow_fill", "ml", 2)
    journal.write("latch", latch="DAILY_LOSS_SLEEVE/control/USD", tripped=1)
    journal.write("stage", sleeve="control", before="min_size", to="stepped_down", cause="LATCH", detail="sam@laptop")
    journal.write("latch", latch="free text from a log line", tripped=1)
    journal.write("break", broken=1, blind=0)
    journal.write("break", broken=0, blind=0)
    journal.write("latch", latch="DAILY_LOSS_SLEEVE/control/USD", tripped=0)
    journal.write("request", what="reset", subject="DAILY_LOSS_SLEEVE/control/USD", value="", operator="sam@laptop")
    journal.write("slippage", lines=[
        {"sleeve": "control", "pair": "EUR.USD", "count": 2, "sum_increments": 3, "lowest_increments": 1,
         "highest_increments": 2, "inexact": 0, "broker_only": 0, "shadow_only": 1, "unequal": 0},
    ])  # fmt: skip
    journal.write("stop", **stop(-3 * USD, {"USD": USD}, -2 * USD, closed=0))
    session(journal, {"idle": "shadow", "control": "stepped_down", "ml": "shadow"})
    fills(journal, "shadow_fill", "control", 2)
    fills(journal, "broker_fill", "control", 2)
    journal.write("slippage", lines=[
        {"sleeve": "control", "pair": "EUR.USD", "count": 1, "sum_increments": -2, "lowest_increments": -2,
         "highest_increments": -2, "inexact": 1, "broker_only": 0, "shadow_only": 0, "unequal": 0},
        {"sleeve": "idle", "pair": "EUR.USD", "count": 0, "sum_increments": 0, "lowest_increments": 0,
         "highest_increments": 0, "inexact": 0, "broker_only": 0, "shadow_only": 0, "unequal": 0},
    ])  # fmt: skip
    journal.write("stop", **stop(-12_340_000, {"USD": 4 * USD, "JPY": -150 * USD}, -11_900_000))
    journal.close()


def write_plain_day(folder, label: str, control: int, ml: int, *, killed: bool = False) -> None:
    journal = Journal(folder / "days" / label / "decisions.jsonl")
    journal.open()
    session(journal, {"idle": "shadow", "control": "stepped_down", "ml": "shadow"})
    fills(journal, "shadow_fill", "control", 2)
    fills(journal, "shadow_fill", "ml", 1)
    if not killed:
        journal.write("stop", **stop(control, {"USD": ml}, control))
    journal.close()


def write_stages(folder, clean: dict[int, dict[str, int]]) -> None:
    journal = Journal(folder / "stages.jsonl")
    journal.open()
    for day, verdicts in clean.items():
        for sleeve, verdict in verdicts.items():
            journal.write("day", day=day, sleeve=sleeve, stage="shadow", clean=verdict, causes=[], ran_ns=1)
    journal.close()


CLEAN = {
    utc(13, 21): {"idle": 1, "control": 1, "ml": 1},
    utc(14, 21): {"idle": 1, "control": 0, "ml": 1},
    utc(15, 21): {"idle": 1, "control": 1, "ml": 1},
    utc(17, 21): {"idle": 1, "control": 1, "ml": 1},
}


def a_week(tmp_path) -> Places:
    """A host's journals for the week of 13 to 17 October 2025, and where they are."""
    trader, record, learn = tmp_path / "trader", tmp_path / "record", tmp_path / "learn"
    for day, lines in STORE.items():
        (record / day).mkdir(parents=True)
        (record / day / "journal.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    write_plain_day(trader, "2025-10-13", -10 * USD, 4 * USD)
    write_tuesday(trader)
    write_plain_day(trader, "2025-10-15", -5_500_000, -20_250_000)
    write_plain_day(trader, "2025-10-16", 0, 0, killed=True)
    write_plain_day(trader, "2025-10-17", -1 * USD, 2 * USD)
    write_stages(trader, CLEAN)
    model = {"hash": "c" * 64, "kind": "ridge", "fitted_rows": 1234}
    evaluation = {"kind": "evaluation-report", "version": 1, "not_a_result": 1, "pair": "EUR.USD",
                  "horizon_ns": 60 * S, "trials": 3, "model": model}  # fmt: skip
    kept = keep_report(learn / "reports", (canonical(evaluation) + "\n").encode("ascii"))
    a, b, c = Named("a" * 64, "1" * 64), Named("b" * 64, "2" * 64), Named("c" * 64, kept)
    path = trader / lineup.FILE
    found = lineup.append(path, Lineup().registered(a), action=lineup.REGISTER, at_ns=utc(4, 7), report=a.report)
    found = lineup.append(path, found.kept(), action=lineup.KEEP, at_ns=utc(5, 16), reasons=("RULE_NOT_IN_FORCE",))
    found = lineup.append(
        path, found.promoted(a), action=lineup.PROMOTE, at_ns=utc(5, 16, 1), decision="HFT-DEC-0007"
    )
    found = lineup.append(path, found.registered(c), action=lineup.REGISTER, at_ns=utc(11, 7), report=kept)
    found = lineup.append(
        path, found.rolled_back(), action=lineup.ROLL_BACK, at_ns=utc(12, 16),
        reasons=("LOSS_LATCH", "the operator said so"), week="2025-10-05", report=b.report,
    )  # fmt: skip
    lineup.append(path, found, action=lineup.DRILL, at_ns=utc(12, 16, 5), reasons=("passed",))
    quotas = (2_000_000_000, 1_000_000_000)
    return Places(record, tmp_path, Calendar(), trader, path, learn / "reports", *quotas, "control")


# ---- the daily summary -----------------------------------------------------------------------------------------------


def test_the_recorders_share_of_the_open_time_is_the_runs_less_every_gap_that_is_not_a_closed_market():
    found = report.recording(store_lines(), Calendar(), TUESDAY)
    eur, jpy = found.pairs
    # Tuesday is open for its 86,400 s. The recorder was down for 300 s (10:00:00 to 10:05:00).
    # EUR.USD: an unexplained 30 s, and a restart gap of 320 s that covers those 300 s: 350 s lost.
    assert (eur.pair, eur.open_ns, eur.recorded_ns) == ("EUR.USD", 86_400 * S, 86_050 * S)
    assert (eur.unexplained, eur.unexplained_ns, eur.share_ppm) == (1, 30 * S, 995_949)
    # USD.JPY: a lost connection of 600 s, and a restart gap of 304 s that covers the 300 s: 904 s lost.
    assert (jpy.recorded_ns, jpy.unexplained, jpy.share_ppm) == (85_496 * S, 0, 989_537)
    assert (found.starts, found.stops) == (1, 1), "the gap over the weekend is the market's, and costs nothing"


def test_a_time_the_journals_say_nothing_about_has_no_recording_part_and_a_shut_market_has_no_share():
    assert report.recording(store_lines(), Calendar(), Span("2025-10-08", utc(7, 21), utc(8, 21))) is None
    assert report.recording([], Calendar(), TUESDAY) is None
    saturday = report.recording(store_lines(), Calendar(), Span("sat", utc(11, 0), utc(12, 0)))
    assert saturday is not None and [pair.share_ppm for pair in saturday.pairs] == [None, None]
    monday = report.recording(store_lines(), Calendar(), DAYS[0])
    # the week opens at 21:15, a quarter of an hour into Monday's trading day, and the recorder started with it
    assert [(pair.open_ns, pair.recorded_ns) for pair in monday.pairs] == [(85_500 * S, 85_500 * S)] * 2


def test_the_daily_summary_of_a_day_with_two_sessions_a_latch_a_break_and_a_step_down(tmp_path):
    places = a_week(tmp_path)
    found = daily(gather(places, ["2025-10-14"]), places.calendar, TUESDAY)
    traded = found.trading
    assert (traded.sessions, traded.mode, traded.closed, traded.saw_close) == (2, "paper", True, True)
    idle, control, ml = traded.sleeves
    assert (control.stage, control.shadow_fills, control.broker_fills) == ("stepped_down", 6, 6)
    assert control.net_micro == {"USD": -12_340_000}, "the day's last word, not the sum of two sessions' words"
    assert (ml.stage, ml.shadow_fills, ml.broker_fills) == ("shadow", 2, 0)
    assert ml.net_micro == {"JPY": -150 * USD, "USD": 4 * USD}
    assert (idle.shadow_fills, idle.net_micro) == (0, {"USD": 0}), "a zero that was said is a zero"
    assert traded.shadow_net_micro == {"JPY": -150 * USD, "USD": -8_340_000}
    assert traded.broker_net_micro == {"USD": -11_900_000}
    (line,) = traded.slippage  # 2 orders at +3 in all, then 1 at -2: three orders, +1 in all
    assert (line.count, line.sum_increments, line.lowest_increments, line.highest_increments) == (3, 1, -2, 2)
    assert (line.inexact, line.shadow_only, line.broker_only, line.unequal) == (1, 1, 0, 0)
    assert traded.breaks == 1 and traded.latches_reset == ("DAILY_LOSS_SLEEVE/control/USD",)
    assert traded.latches_tripped == ("DAILY_LOSS_SLEEVE/control/USD", "[text]")
    assert traded.stage_changes == (("control", "min_size", "stepped_down", "LATCH"),)
    assert found.clean == {"idle": True, "control": False, "ml": True}
    assert found.absent == {}
    text = report.render_daily(found)
    for line in (
        "HFT Lab daily summary: trading day 2025-10-14, 2025-10-13T21:00:00Z to 2025-10-14T21:00:00Z. "
        "Paper account only.",
        "  EUR.USD: recorded 86,050 s of 86,400 s open (995,949 per million); 1 unexplained gap(s), 30 s",
        "  USD.JPY: recorded 85,496 s of 86,400 s open (989,537 per million); 0 unexplained gap(s), 0 s",
        "trader     2 session(s); paper; saw the day close",
        "  control: stage stepped_down; 6 shadow fill(s), 6 broker fill(s); shadow result -12.340000 USD; "
        "not a clean day",
        "  ml: stage shadow; 2 shadow fill(s), 0 broker fill(s); shadow result -150.000000 JPY, 4.000000 USD; "
        "clean day",
        "  desk: shadow book -150.000000 JPY, -8.340000 USD; broker book -11.900000 USD",
        "  slippage control EUR.USD: 3 order(s) compared, mean 0, lowest -2, highest 2 increment(s), shadow less "
        "broker; 1 inexact; 0 broker only, 1 shadow only, 0 unequal",
        "  reconciliation breaks 1; latches tripped: DAILY_LOSS_SLEEVE/control/USD, [text]; latches reset: "
        "DAILY_LOSS_SLEEVE/control/USD",
        "  stage changes: control min_size to stepped_down (LATCH)",
        "disk       the tapes: 0 bytes of 1,000,000,000 bytes (0%)",
    ):  # fmt: skip
        assert line in text.splitlines(), line
    on_disk = sum(path.stat().st_size for path in places.record_root.rglob("*.jsonl"))
    assert f"disk       the recording: {on_disk:,} bytes of 2,000,000,000 bytes (0%)" in text.splitlines()


def test_a_missing_part_is_absent_and_said_to_be_absent_and_is_never_a_zero(tmp_path):
    places = a_week(tmp_path)
    thursday = daily(gather(places, ["2025-10-16"]), places.calendar, DAYS[3])
    assert thursday.recording is None and thursday.trading.closed is False
    control = thursday.trading.sleeves[1]
    assert (control.shadow_fills, control.net_micro) == (2, None), "its fills are counted; its result is not said"
    assert thursday.trading.shadow_net_micro is None and thursday.clean is None
    assert set(thursday.absent) == {"recorder", "results", "clean day"}
    text = report.render_daily(thursday)
    assert "recorder   ABSENT" in text and "trader     1 session(s); paper; NOT STOPPED" in text
    assert "  control: stage stepped_down; 2 shadow fill(s), 0 broker fill(s); shadow result ABSENT" in text
    assert "  desk: shadow book ABSENT; broker book ABSENT" in text
    assert "absent     results: the day's last session did not say it stopped" in text
    assert " 0.000000 " not in text, "nothing that is missing is shown as a zero amount"

    nothing = daily(report.Read(), Calendar(), TUESDAY)
    assert nothing.recording is None and nothing.trading is None and nothing.disks == ()
    text = report.render_daily(nothing)
    assert "recorder   ABSENT" in text and "trader     ABSENT" in text
    assert "absent     recorder: no journal of the recording" in text and "absent     trader: no session" in text

    session_only = Journal(tmp_path / "s" / "days" / "2025-10-14" / "decisions.jsonl")
    session_only.open()
    session(session_only, {"idle": "shadow", "control": "shadow", "ml": "shadow"}, mode="shadow_only")
    session_only.write("stop", **stop(-USD, {"USD": 0}, None))
    session_only.close()
    shadow = daily(gather(Places(tmp_path / "none", tmp_path, Calendar(), tmp_path / "s", None, None, None, None, "c"),
                          ["2025-10-14"]), Calendar(), TUESDAY)  # fmt: skip
    assert shadow.trading.broker_net_micro is None and "broker book" in shadow.absent
    assert "broker book ABSENT" in report.render_daily(shadow) and "disk       the tapes: 0 bytes no quota set" in (
        report.render_daily(shadow)
    )


def test_a_journal_that_does_not_verify_leaves_its_part_absent_and_is_not_repaired(tmp_path):
    places = a_week(tmp_path)
    path = places.trader / "days" / "2025-10-14" / "decisions.jsonl"
    before = path.read_bytes().replace(b'"broken":1', b'"broken":0', 1)
    path.write_bytes(before)
    found = daily(gather(places, ["2025-10-14"]), places.calendar, TUESDAY)
    assert found.trading is None and "does not verify" in found.absent["trader"]
    assert path.read_bytes() == before


# ---- the weekly scorecard --------------------------------------------------------------------------------------------


def test_the_windows_are_the_trading_day_and_the_week_that_closed_last_in_new_yorks_time():
    calendar = Calendar()
    assert report.last_trading_day(calendar, utc(14, 21, 10)) == TUESDAY
    assert report.last_trading_day(calendar, utc(14, 20, 59)) == DAYS[0], "Tuesday has not closed yet"
    assert report.last_trading_day(calendar, utc(19, 12)) == DAYS[4], "on a Sunday the last trading day is Friday"
    assert report.last_trading_week(calendar, AFTER_FRIDAY) == (WEEK, DAYS)
    assert report.last_trading_week(calendar, utc(17, 20, 59))[0].label == "2025-W41", "this week has not closed yet"
    winter = int(datetime(2025, 12, 5, 22, 10, tzinfo=UTC).timestamp()) * S  # New York is five hours behind by then
    week, days = report.last_trading_week(calendar, winter)
    assert week.end_ns == winter - 600 * S and days[-1].label == "2025-12-05" and len(days) == 5


def test_the_weekly_scorecard_of_a_week_with_a_rollback_sums_the_days_that_said_a_result(tmp_path):
    places = a_week(tmp_path)
    found = weekly(gather(places, [day.label for day in DAYS]), places.calendar, WEEK, DAYS, "control")
    assert found.days == tuple(day.label for day in DAYS) == found.days_traded
    idle, control, ml = found.sleeves
    # control: -10 -12.34 -5.50 -1 over the four days that said a result; Thursday's session was killed
    assert (control.net_micro, control.days_with_result) == ({"USD": -28_840_000}, 4)
    assert control.shadow_fills == 2 + 6 + 2 + 2 + 2 and control.broker_fills == 6
    assert control.less_control_micro is None
    # ml: 4 + 4 - 20.25 + 2 dollars, and -150 yen on Tuesday
    assert ml.net_micro == {"USD": -10_250_000, "JPY": -150 * USD}
    assert ml.less_control_micro == {"USD": 18_590_000, "JPY": -150 * USD}, "-10.25 less -28.84"
    assert idle.less_control_micro == {"USD": 28_840_000}
    # the desk: control and ml together in the shadow book; the broker book carries the control sleeve alone
    assert found.shadow_net_micro == {"USD": -39_090_000, "JPY": -150 * USD}
    assert found.broker_net_micro == {"USD": -10 * USD - 11_900_000 - 5_500_000 - USD}
    assert found.days_with_broker_result == 4
    assert found.breaks == 1 and found.days_clean == 3, "Monday, Wednesday and Friday; Thursday was never closed"
    assert [(change.action, change.champion, change.challengers) for change in found.lineup] == [
        ("register", "a" * 12, 0), ("roll_back", None, 1), ("drill", None, 1),
    ]  # fmt: skip
    trained = found.lineup[0].training
    assert (trained.pair, trained.horizon_ns, trained.kind, trained.fitted_rows, trained.trials) == (
        "EUR.USD", 60 * S, "ridge", 1234, 3,
    )  # fmt: skip
    assert trained.not_a_result is True and found.lineup[1].reasons == ("LOSS_LATCH", "[text]")
    assert found.lineup[1].training is None and "training report" in found.absent
    # the week from Friday 21:00: open from Sunday 21:15 to Friday 21:00 is 431,100 s. The recorder ran from the
    # open to its last word on Tuesday at 21:00:01, 171,901 s, less 300 s down: EUR.USD lost another 30 s and 20 s
    eur = found.recording.pairs[0]
    assert (eur.open_ns, eur.recorded_ns, eur.share_ppm) == (431_100 * S, 171_551 * S, 397_937)
    gates = found.gates
    assert (gates.days_recorded, gates.days_every_gap_explained) == (3, 2), (
        "Monday, Tuesday and the one second of Wednesday; Tuesday has a gap nobody explained"
    )
    assert (gates.days_traded, gates.days_clean) == (4, 3)
    assert (gates.weekly_cycles, gates.drills_passed) == (2, 1), "two weekends with a training and a decision"
    text = report.render_weekly(found)
    for line in (
        "HFT Lab weekly scorecard: week 2025-W42, 2025-10-10T21:00:00Z to 2025-10-17T21:00:00Z. "
        "Paper account only.",
        "  control: stage stepped_down; 14 shadow fill(s), 6 broker fill(s); shadow result -28.840000 USD "
        "over 4 of 5 day(s)",
        "  ml: stage shadow; 6 shadow fill(s), 0 broker fill(s); shadow result -150.000000 JPY, -10.250000 USD "
        "over 4 of 5 day(s); less control -150.000000 JPY, 18.590000 USD",
        "  desk: shadow book -150.000000 JPY, -39.090000 USD; broker book -28.400000 USD over 4 day(s)",
        "lineup     2025-10-11T07:00:00Z register; champion aaaaaaaaaaaa; 0 challenger(s), 1 candidate(s); "
        f"decision record none; report {found.lineup[0].report}: EUR.USD, 60 s, ridge, 1,234 row(s), 3 trial(s); "
        "NOT A RESULT: placeholders",
        "lineup     2025-10-12T16:00:00Z roll_back (LOSS_LATCH, [text]); champion none; 1 challenger(s), "
        "0 candidate(s); decision record none; report 222222222222: ABSENT",
        "  F2 days with every gap explained: 2 of 3 recorded day(s), towards 5",
        "  F4 clean trading days: 3 of 4 closed day(s), towards 5",
        "  F6 weekly cycles: 2 weekend(s) with a training and a decision, towards 2; 1 rollback drill(s) passed",
        "  this week: 3 of 5 trading day(s) clean for every sleeve",
        "absent     results: no result for 2025-10-16: the day's last session did not say it stopped",
    ):  # fmt: skip
        assert line in text.splitlines(), line


def test_a_week_without_a_control_sleeve_a_lineup_or_a_stage_journal_says_so(tmp_path):
    journal = Journal(tmp_path / "t" / "days" / "2025-10-15" / "decisions.jsonl")
    journal.open()
    journal.write("start", sleeves=["ml"], stages={"ml": "shadow"}, mode="shadow_only")
    said = {"sleeves": [{"name": "ml", "net_today_micro": {"USD": 7}}], "books": {}}
    journal.write("stop", summary=said, day_closed=1)
    journal.close()
    places = Places(tmp_path / "none", tmp_path, Calendar(), tmp_path / "t", None, None, None, None, "control")
    found = weekly(gather(places, [day.label for day in DAYS]), places.calendar, WEEK, DAYS, "control")
    (ml,) = found.sleeves
    assert ml.net_micro == {"USD": 7} and ml.less_control_micro is None and found.days_traded == ("2025-10-15",)
    assert found.lineup is None and found.days_clean is None and found.recording is None
    assert found.gates == report.Gates(None, None, None, None, None, None)
    assert {"recorder", "control sleeve", "lineup", "broker book"} <= set(found.absent)
    text = report.render_weekly(found)
    assert "lineup     ABSENT" in text and "recorder   ABSENT" in text
    assert "  F2 days with every gap explained: ABSENT" in text and "  F6 weekly cycles: ABSENT" in text
    line = "  ml: stage shadow; 0 shadow fill(s), 0 broker fill(s); shadow result 0.000007 USD over 1 of 1 day(s)"
    assert f"{line}; less control ABSENT" in text


# ---- what never enters a report --------------------------------------------------------------------------------------


def test_no_account_id_host_name_or_free_text_from_a_journal_reaches_a_report_a_page_or_its_file(tmp_path):
    places = a_week(tmp_path)
    read = gather(places, [day.label for day in DAYS])
    made = [report.render_daily(daily(read, places.calendar, TUESDAY)),
            report.render_weekly(weekly(read, places.calendar, WEEK, DAYS)),
            json.dumps(report.as_dict(daily(read, places.calendar, TUESDAY))),
            json.dumps(report.as_dict(weekly(read, places.calendar, WEEK, DAYS)))]  # fmt: skip
    for text in made:
        for private in (*PRIVATE, str(tmp_path), "RULE_NOT"):
            assert private not in text, private
    assert report.name("DU1234567") == "DU1234567", "shaped like a name: it is the page that masks an account id"
    page = report.page_of("daily", "2025-10-14", "control DU1234567 lost\nU7654321 too")
    assert page.message == "control [account] lost\n[account] too" and page.key == "hft:report.daily"
    assert (page.title, page.priority, page.resolved) == ("Daily summary 2025-10-14", 2, False)
    for value in ("a b", "", "x" * 65, "né", 7, None, "semi;colon", "new\nline", "-flag"):
        assert report.name(value) == "[text]", value


def test_the_report_holds_whole_numbers_and_text_and_no_float(tmp_path):
    places = a_week(tmp_path)
    read = gather(places, [day.label for day in DAYS])

    def walk(value) -> None:
        assert not isinstance(value, float), value
        for inner in value.values() if isinstance(value, dict) else value if isinstance(value, list | tuple) else ():
            walk(inner)

    walk(report.as_dict(daily(read, places.calendar, TUESDAY)))
    walk(report.as_dict(weekly(read, places.calendar, WEEK, DAYS)))
    assert report._amount({"USD": -1}) == "-0.000001 USD" and report._amount({}) == "nothing in any currency"
    assert Disk("x", 1_999, 2_000).percent == 99 and Disk("x", 5, None).percent is None


def test_a_long_report_is_cut_at_a_line_for_the_page_and_whole_in_its_file(tmp_path):
    text = "\n".join(f"line {number} " + "x" * 90 for number in range(60))
    page = report.page_of("weekly", "2025-W42", text)
    assert len(page.message) <= report.PAGE_MAX + 60 and page.message.endswith("its file on the host)")
    assert all(line in text.splitlines() for line in page.message.splitlines()[:-1]), "cut between two lines"
    path = report.keep(tmp_path, "weekly", "2025-W42", text)
    assert path == tmp_path / "reports" / "weekly-2025-W42.txt" and path.read_text() == text + "\n"
    assert report.keep(tmp_path, "weekly", "2025-W42", "again").read_text() == "again\n", "the same week replaces it"


# ---- the command -----------------------------------------------------------------------------------------------------


@pytest.fixture
def host_config(tmp_path, configured) -> list[str]:
    """The shipped settings pointed at a week of journals under tmp_path/var."""
    places = a_week(tmp_path / "var")
    (tmp_path / "trader.yaml").write_text(
        f"state_dir: {places.trader}\nregistry: {tmp_path / 'var' / 'learn'}\ntape: {{quota_bytes: 1000}}\n"
    )
    (tmp_path / "learn.yaml").write_text("promotion: {baseline_sleeve: control}\n")
    more = ["--trader-config", str(tmp_path / "trader.yaml"), "--learn-config", str(tmp_path / "learn.yaml")]
    return [*configured, *more]


def test_after_a_friday_close_the_unit_writes_and_sends_the_daily_summary_and_the_weekly_scorecard(
    host_config, tmp_path, capsys
):
    pages = []
    code = report.main(["due", "--send", *host_config], SimClock(AFTER_FRIDAY), lambda page: pages.append(page) or True)
    assert code == 0 and [page.key for page in pages] == ["hft:report.daily", "hft:report.weekly"]
    daily_file = tmp_path / "var" / "reports" / "daily-2025-10-17.txt"
    weekly_file = tmp_path / "var" / "reports" / "weekly-2025-W42.txt"
    for page, file in zip(pages, (daily_file, weekly_file), strict=True):  # a page's lines are a file's, spaces joined
        assert page.message.splitlines() == [" ".join(line.split()) for line in file.read_text().splitlines()]
    assert "shadow result -28.840000 USD over 4 of 5 day(s)" in weekly_file.read_text()
    assert "disk       the tapes: 0 bytes of 1,000 bytes (0%)" in daily_file.read_text()
    said = capsys.readouterr()
    assert "HFT Lab weekly scorecard: week 2025-W42" in said.out and f"report: written to {weekly_file}" in said.err
    for private in PRIVATE:
        assert private not in said.out + daily_file.read_text() + weekly_file.read_text() + pages[1].message


def test_on_another_day_the_unit_sends_the_daily_summary_alone_and_a_failed_send_is_its_own_exit_code(
    host_config, tmp_path
):
    pages = []
    assert report.main(["due", "--send", *host_config], SimClock(utc(14, 21, 10)), lambda page: pages.append(page)) == 1
    assert [page.title for page in pages] == ["Daily summary 2025-10-14"], "a Tuesday: no scorecard"
    assert (tmp_path / "var" / "reports" / "daily-2025-10-14.txt").is_file(), "written although it was not delivered"
    assert not list((tmp_path / "var" / "reports").glob("weekly-*"))
    assert report.main(["weekly", *host_config], SimClock(utc(14, 21, 10))) == 0, "asked for by name, any day"
    assert (tmp_path / "var" / "reports" / "weekly-2025-W41.txt").is_file() and len(pages) == 1, "and not sent"


def test_with_no_address_configured_the_page_goes_to_the_log_and_settings_that_cannot_be_read_are_refused(
    host_config, tmp_path, capsys
):
    assert report.main(["daily", "--send", *host_config], SimClock(utc(14, 21, 10))) == 0
    assert "alerts: no address is configured, so not sent" in capsys.readouterr().err
    assert report.main(["daily", "--config", str(tmp_path / "missing.yaml")], SimClock(AFTER_FRIDAY)) == 2
    assert "report: cannot read" in capsys.readouterr().err


def test_the_shipped_settings_name_every_place_the_report_reads():
    places = report.load_places(
        ROOT / "config/ops.yaml", ROOT / "config/recorder.yaml", ROOT / "config/shadow.yaml", ROOT / "config/calendar",
        ROOT / "config/trader.yaml", ROOT / "config/learn.yaml",
    )  # fmt: skip
    assert (str(places.trader), str(places.lineup), str(places.reports)) == (
        "var/trader", "var/trader/lineup.jsonl", "var/learn/reports",
    )  # fmt: skip
    assert (places.recording_quota, places.tape_quota, places.control) == (2_000_000_000, 1_000_000_000, "control")
