"""The status command: one reading of the recorder's status file and the journals, judged, with a dead-man rule.

Every reading here is of files a real recorder wrote into tmp_path, so the tests also hold the recorder and the
reader to the same idea of what a status file says.
"""
from __future__ import annotations

import ast
import itertools
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.calendar import Calendar, Closure
from hftlab.calendar.week import is_week_open
from hftlab.core import SimClock
from hftlab.ops import host, status
from hftlab.ops.alerts import Alerts
from hftlab.ops.status import EXIT_BAD, EXIT_DEAD, EXIT_OK, EXIT_UNREADABLE, exit_code, read, render, trading_weeks
from hftlab.record.store import StoreSettings, append_line, measure
from hftlab.venues.fake import FakeConfig
from tests.ops.kit import DAY, OPEN, SATURDAY, Host, a_shadow_session, sources
from tests.record.kit import HOUR, MS, SECOND, a_disk, everything
from tests.support import ROOT, called_name

MINUTE = 60 * SECOND
WEEK_NS = 4 * DAY + 23 * HOUR + 45 * MINUTE  # Sunday 17:15 to Friday 17:00


def codes(reading) -> list[str]:
    return [problem.code for problem in reading.problems]


def edit_status(root, **changes) -> None:
    path = root / "status.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


# ---- before anything has been recorded ------------------------------------------------------------------------------


def test_a_host_where_nothing_was_ever_recorded_is_not_a_failure_even_with_the_market_open(tmp_path):
    reading = read(sources(tmp_path), OPEN)
    assert reading.recorder is None and reading.shadow is None and reading.problems == ()
    assert reading.market_open and reading.level == "ok" and exit_code(reading) == EXIT_OK
    screen = render(reading)
    assert "recorder   never started" in screen and "health     OK" in screen
    assert screen.splitlines()[0] == "HFT Lab status at 2026-10-14T03:00:00Z"


# ---- the recorder ---------------------------------------------------------------------------------------------------


def test_a_running_recorder_is_read_as_it_is(tmp_path):
    rig = Host(tmp_path).record(12)
    now = rig.clock.now_ns()
    reading = read(rig.sources, now)
    recorder = reading.recorder
    assert reading.problems == () and exit_code(reading) == EXIT_OK
    assert (recorder.state, recorder.said, recorder.venue, recorder.connection) == (
        "running", "recording", "ibkr_paper", "RECONCILING",
    )  # fmt: skip
    assert 0 <= recorder.age_ns < 5 * SECOND and recorder.run == 1
    assert recorder.records_today == len(everything(rig.root)) > 1_000, "what is on disk, counted from block heads"
    assert 0 < recorder.records_this_run <= recorder.records_today, "the status file is two seconds behind the disk"
    assert recorder.last_record_ns is not None and now - recorder.last_record_ns < 6 * SECOND
    assert recorder.gaps_unexplained_today == 0 and recorder.silences_open == 0
    assert recorder.used_bytes == measure(rig.root) and recorder.quota_bytes == 1 << 30
    assert recorder.longest_between_polls_ns == recorder.recent_between_polls_ns == 20 * MS and recorder.polls > 100
    screen = render(reading)
    assert 'recorder   running; last beat 2 s ago, saying "recording"; run 1; venue ibkr_paper' in screen
    assert "loop       longest between two polls 20.0 ms; recent 20.0 ms" in screen
    assert "gaps       0 unexplained today; 0 silence(s) open now" in screen and "of 1,073.7 MB (0%)" in screen


def test_the_dead_man_rule_a_recorder_that_went_silent_with_the_market_open_has_its_own_exit_code(tmp_path):
    rig = Host(tmp_path).record(12)  # and then it is killed: no stop, no last word
    beat = read(rig.sources, rig.clock.now_ns()).recorder.beat_ns
    on_the_line = read(rig.sources, beat + 60 * SECOND)
    assert on_the_line.recorder.state == "running" and exit_code(on_the_line) == EXIT_OK
    dead = read(rig.sources, beat + 61 * SECOND)
    assert dead.recorder.state == "stopped" and dead.recorder.said == "recording"
    assert codes(dead) == ["recorder_stale"] and dead.problems[0].dead_man and dead.level == "bad"
    assert exit_code(dead) == EXIT_DEAD == 3
    assert '[DEAD-MAN] recorder_stale: The recorder last said "recording" 61 s ago' in render(dead)


def test_the_same_silence_with_the_market_shut_is_a_warning_and_not_a_failure(tmp_path):
    rig = Host(tmp_path, start_ns=SATURDAY).record(12)
    quiet = read(rig.sources, rig.clock.now_ns() + HOUR)
    assert not quiet.market_open and codes(quiet) == ["recorder_not_running"]
    assert quiet.level == "warn" and exit_code(quiet) == EXIT_OK


def test_a_holiday_shuts_the_market_for_the_dead_man_rule_too(tmp_path):
    rig = Host(tmp_path).record(12)
    holiday = Calendar(closures=(Closure("a holiday", OPEN - HOUR, OPEN + DAY),))
    reading = read(sources(tmp_path, calendar=holiday), rig.clock.now_ns() + HOUR)
    assert not reading.market_open and exit_code(reading) == EXIT_OK


def test_a_recorder_stopped_on_purpose_is_a_failure_only_while_the_market_is_open(tmp_path):
    rig = Host(tmp_path).record(6).stop()
    stopped = read(rig.sources, rig.clock.now_ns() + 10 * SECOND)
    assert stopped.recorder.state == "stopped" and stopped.recorder.said == "stopped"
    assert codes(stopped) == ["recorder_stopped"] and exit_code(stopped) == EXIT_BAD
    assert not stopped.problems[0].dead_man, "it said it was stopping: that is not the dead-man rule"
    weekend = read(rig.sources, SATURDAY)
    assert weekend.problems == () and exit_code(weekend) == EXIT_OK


def test_a_recorder_stopped_by_its_quota_says_so(tmp_path):
    tight = StoreSettings(quota_bytes=6_000, keep_ns=HOUR, block_records=8, fsync=False)
    rig = Host(tmp_path, store=tight)
    rig.broker.market.generate_quotes("EUR.USD", start_ns=OPEN + MS, count=3_000, interval_ns=10 * MS, mid=108_505,
                                      spread=10, step=5)  # fmt: skip
    assert rig.recorder.run(lambda: rig.clock.advance(20 * MS) > OPEN + 60 * SECOND) == 3
    reading = read(rig.sources, rig.clock.now_ns() + 5 * MINUTE)
    assert codes(reading) == ["recorder_quota"] and exit_code(reading) == EXIT_BAD
    assert "disk_near_quota" not in codes(reading), "at the quota there is nothing left to warn about"


def test_a_recorder_stopped_by_a_refused_write_says_that_and_not_quota(tmp_path):
    disk = a_disk()
    rig = Host(tmp_path, writer=disk)

    def until_full() -> bool:
        disk.full = rig.clock.advance(20 * MS) > OPEN + 2 * SECOND
        return False

    rig.broker.market.generate_quotes("EUR.USD", start_ns=OPEN + MS, count=3_000, interval_ns=10 * MS, mid=108_505,
                                      spread=10, step=5)  # fmt: skip
    assert rig.recorder.run(until_full) == 4
    reading = read(rig.sources, rig.clock.now_ns() + SECOND)
    assert codes(reading) == ["recorder_io_error"] and exit_code(reading) == EXIT_BAD


def test_a_store_written_by_a_dry_run_of_the_fake_broker_is_not_taken_for_the_market(tmp_path):
    rig = Host(tmp_path, venue="fake").record(6)
    reading = read(rig.sources, rig.clock.now_ns())
    assert codes(reading) == ["recorder_not_on_paper_venue"] and exit_code(reading) == EXIT_BAD


def test_a_recorder_without_its_gateway_while_the_market_is_open_is_a_failure(tmp_path):
    outage = FakeConfig(outages=((OPEN + 2 * SECOND, OPEN + 10 * MINUTE),))
    rig = Host(tmp_path, config=outage).record(12, quotes=False)
    reading = read(rig.sources, rig.clock.now_ns())
    assert reading.recorder.state == "running" and reading.recorder.connection in ("DISCONNECTED", "CONNECTING")
    assert "connection_down" in codes(reading) and exit_code(reading) == EXIT_BAD


def test_unexplained_gaps_are_counted_from_the_journal_and_from_the_silences_still_open_once_each(tmp_path):
    rig = Host(tmp_path).record(12)
    now = rig.clock.now_ns()
    journal = rig.root / "2026-10-14" / "journal.jsonl"
    for pair, start, reason in [("EUR.USD", OPEN + 1, "unexplained"), ("EUR.USD", OPEN + 2, "connection_lost")]:
        gap = {"type": "gap", "pair": pair, "start_ns": start, "end_ns": start + 9 * SECOND, "reason": reason}
        append_line(journal, (json.dumps(gap) + "\n").encode())
    assert read(rig.sources, now).recorder.gaps_unexplained_today == 1
    still_open = [
        {"type": "gap", "pair": "USD.JPY", "start_ns": OPEN + 3, "end_ns": now, "reason": "unexplained"},
        {"type": "gap", "pair": "EUR.USD", "start_ns": OPEN + 1, "end_ns": now, "reason": "unexplained"},  # journalled
        {"type": "gap", "pair": "USD.JPY", "start_ns": OPEN + 4, "end_ns": now, "reason": "market_closed"},
    ]
    edit_status(rig.root, silent=still_open)
    reading = read(rig.sources, now)
    assert reading.recorder.gaps_unexplained_today == 2 and reading.recorder.silences_open == 3
    assert codes(reading) == ["gaps_unexplained"] and reading.level == "warn" and exit_code(reading) == EXIT_OK
    later = read(rig.sources, now + 5 * MINUTE)  # a dead recorder's list of open silences is old news
    assert later.recorder.gaps_unexplained_today == 1


def test_a_recording_close_to_its_quota_is_a_warning(tmp_path):
    rig = Host(tmp_path).record(12)
    used = measure(rig.root)
    edit_status(rig.root, quota_bytes=used * 100 // 80)  # at about 80% of the quota
    assert codes(read(rig.sources, rig.clock.now_ns())) == []
    edit_status(rig.root, quota_bytes=used * 100 // 95)  # at about 95%
    reading = read(rig.sources, rig.clock.now_ns())
    assert codes(reading) == ["disk_near_quota"] and exit_code(reading) == EXIT_OK


def test_a_status_file_that_cannot_be_read_beside_recordings_is_a_failure(tmp_path):
    rig = Host(tmp_path).record(6)
    (rig.root / "status.json").write_text("{not json")
    reading = read(rig.sources, rig.clock.now_ns())
    assert codes(reading) == ["recorder_status_unreadable"] and exit_code(reading) == EXIT_BAD
    (rig.root / "status.json").unlink()
    assert codes(read(rig.sources, rig.clock.now_ns())) == ["recorder_status_unreadable"]


def test_a_segment_that_is_being_compressed_or_is_damaged_does_not_break_the_screen(tmp_path):
    rig = Host(tmp_path).record(6)
    whole = read(rig.sources, rig.clock.now_ns()).recorder.records_today
    (rig.root / "2026-10-14" / "04.seg").write_bytes(b"not a segment")
    reading = read(rig.sources, rig.clock.now_ns())
    assert reading.recorder.records_today == whole and "recorder   running" in render(reading)


# ---- the shadow run and the switches ---------------------------------------------------------------------------------


def test_the_shadow_journal_gives_the_sessions_and_each_sleeves_counts_for_the_last_one(tmp_path):
    first = a_shadow_session(tmp_path)
    reading = read(sources(tmp_path), OPEN)
    assert reading.shadow.sessions == 1 and reading.shadow.entries > 10 and reading.problems == ()
    counted = {name: dict(counts) for name, counts in reading.shadow.sleeves.items()}
    expected = {name: {k: v for k, v in s.items() if k != "pairs"} for name, s in first["sleeves"].items()}
    assert counted == expected and counted["control"]["fills"] > 0 and counted["idle"]["decisions"] == 0
    second = a_shadow_session(tmp_path, seed=2)
    again = read(sources(tmp_path), OPEN)
    assert again.shadow.sessions == 2
    last = {k: v for k, v in second["sleeves"]["control"].items() if k != "pairs"}
    assert dict(again.shadow.sleeves["control"]) == last
    screen = render(again)
    assert "shadow     2 session(s)" in screen and "sleeve control, this session:" in screen


def test_a_shadow_journal_that_does_not_verify_is_a_failure_and_is_not_read_past(tmp_path):
    a_shadow_session(tmp_path)
    path = sources(tmp_path).shadow_journal
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n")  # one entry taken out of the chain
    reading = read(sources(tmp_path), OPEN)
    assert reading.shadow is None and codes(reading) == ["shadow_journal_broken"] and exit_code(reading) == EXIT_BAD


def test_the_switches_are_read_from_the_state_directory_and_only_read(tmp_path):
    state = sources(tmp_path).state
    state.mkdir(parents=True)
    assert (read(sources(tmp_path), OPEN).kill, read(sources(tmp_path), OPEN).flatten) == (False, False)
    (state / "KILL").touch()
    reading = read(sources(tmp_path), OPEN)
    assert (reading.kill, reading.flatten) == (True, False) and "KILL SET; FLATTEN off" in render(reading)
    (state / "FLATTEN").touch()
    assert read(sources(tmp_path), OPEN).flatten and sorted(p.name for p in state.iterdir()) == ["FLATTEN", "KILL"]
    assert reading.problems == (), "a switch is the owner's decision, not a fault"


# ---- the trading weeks -----------------------------------------------------------------------------------------------


def test_the_week_that_is_open_comes_first_then_the_one_after():
    first, second = trading_weeks(OPEN)
    assert (first.session, status.stamp(first.start_ns), status.stamp(first.end_ns)) == (
        "2026-W42", "2026-10-11T21:15:00Z", "2026-10-16T21:00:00Z",
    )  # fmt: skip
    assert (second.session, status.stamp(second.start_ns), status.stamp(second.end_ns)) == (
        "2026-W43", "2026-10-18T21:15:00Z", "2026-10-23T21:00:00Z",
    )  # fmt: skip


def test_at_the_weekend_the_next_week_to_open_comes_first():
    assert [week.session for week in trading_weeks(SATURDAY)] == ["2026-W43", "2026-W44"]
    friday_close = trading_weeks(OPEN)[0].end_ns
    assert trading_weeks(friday_close - 1)[0].session == "2026-W42"
    assert trading_weeks(friday_close)[0].session == "2026-W43", "17:00 on Friday is already the weekend"
    sunday_open = trading_weeks(SATURDAY)[0].start_ns
    assert trading_weeks(sunday_open - 1)[0].session == trading_weeks(sunday_open)[0].session == "2026-W43"


def test_the_weeks_follow_new_york_across_the_end_of_summer_time():
    """New York leaves summer time on Sunday 1 November 2026: the week that opens that evening is an hour later."""
    before, after = trading_weeks(OPEN + 14 * DAY)  # Wednesday 28 October
    stamps = [status.stamp(at) for at in (before.start_ns, before.end_ns, after.start_ns, after.end_ns)]
    assert stamps == ["2026-10-25T21:15:00Z", "2026-10-30T21:00:00Z", "2026-11-01T22:15:00Z", "2026-11-06T22:00:00Z"]
    assert (before.session, after.session) == ("2026-W44", "2026-W45")


@given(st.integers(min_value=OPEN - 400 * DAY, max_value=OPEN + 400 * DAY))
def test_at_any_time_the_first_week_is_the_open_one_or_the_next_and_every_week_is_a_whole_week(now):
    weeks = trading_weeks(now, 3)
    assert (weeks[0].start_ns <= now < weeks[0].end_ns) == is_week_open(now)
    assert now < weeks[0].end_ns, "a week that is over is never listed"
    for week, following in itertools.pairwise(weeks):
        assert week.end_ns < following.start_ns
    for week in weeks:
        assert week.end_ns - week.start_ns == WEEK_NS
        assert is_week_open(week.start_ns) and is_week_open(week.end_ns - 1) and not is_week_open(week.end_ns)


# ---- paging ----------------------------------------------------------------------------------------------------------


class Pages:
    def __init__(self) -> None:
        self.sent = []

    def __call__(self, page) -> bool:
        self.sent.append((page.key, page.priority, page.resolved))
        return True


def test_a_bad_problem_pages_once_and_its_end_pages_once(tmp_path):
    rig = Host(tmp_path).record(12)
    pages = Pages()
    alerts = Alerts(tmp_path / "var" / "alerts", pages, rig.clock)
    beat = read(rig.sources, rig.clock.now_ns()).recorder.beat_ns
    for seconds in (61, 120, 400):  # the check runs every few minutes while the recorder stays dead
        status.page(read(rig.sources, beat + seconds * SECOND), alerts)
    assert pages.sent == [("hft:recorder_stale", 5, False)], "the dead-man rule is the most urgent, and it pages once"
    rig.record(6)  # the unit's restart brought it back
    status.page(read(rig.sources, rig.clock.now_ns()), alerts)
    status.page(read(rig.sources, rig.clock.now_ns()), alerts)
    assert pages.sent[1:] == [("hft:recorder_stale", 2, True)] and alerts.firing() == []


def test_a_warning_does_not_page_and_another_commands_alert_is_not_this_ones_to_take_back(tmp_path):
    rig = Host(tmp_path).record(12)
    pages = Pages()
    alerts = Alerts(tmp_path / "var" / "alerts", pages, rig.clock)
    alerts.fire("deploy_failed", "Deploy failed", "Some other command's alert.")
    edit_status(rig.root, quota_bytes=measure(rig.root))  # at 100% of the quota: a warning
    reading = read(rig.sources, rig.clock.now_ns())
    assert codes(reading) == ["disk_near_quota"]
    status.page(reading, alerts)
    assert pages.sent == [("hft:deploy_failed", 4, False)] and alerts.firing() == ["hft:deploy_failed"]


def test_every_problem_this_command_calls_bad_is_one_it_pages_for_and_can_take_back():
    tree = ast.parse((ROOT / "src" / "hftlab" / "ops" / "status.py").read_text())
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and called_name(node) == "Problem" and ast.unparse(node.args[1]) == "BAD":
            bad.add(ast.literal_eval(node.args[0]))
    assert bad == set(status.PAGED) and len(bad) == len(status.PAGED)
    alerts = Alerts(ROOT / "nowhere", Pages(), SimClock(OPEN))
    for code in status.PAGED:
        assert alerts.key(code) == f"{host.ALERT_PREFIX}{code}", "every code is a well-formed alert key"


# ---- the command -----------------------------------------------------------------------------------------------------


def test_the_shipped_settings_load_and_take_the_places_from_the_files_that_own_them(configured, tmp_path):
    found = status.load_sources(*(status.Path(configured[n]) for n in (1, 3, 5, 7)))
    assert found.record_root == tmp_path / "var" / "record" and found.state == tmp_path / "var"
    assert found.shadow_journal == tmp_path / "var" / "shadow" / "live.jsonl"
    assert found.dead_man_ns == 60 * SECOND and found.disk_warn_percent == 90
    shipped = status.load_sources()
    assert shipped.record_root == status.Path("var/record")
    assert shipped.shadow_journal == status.Path("var/shadow/live.jsonl")


def test_the_command_prints_the_screen_and_exits_by_what_it_found(configured, tmp_path, capsys):
    assert status.main(configured, clock=SimClock(OPEN)) == EXIT_OK
    assert "recorder   never started" in capsys.readouterr().out
    rig = Host(tmp_path).record(12)
    assert status.main(configured, clock=rig.clock) == EXIT_OK
    assert "recorder   running" in capsys.readouterr().out
    assert status.main(configured, clock=SimClock(rig.clock.now_ns() + 5 * MINUTE)) == EXIT_DEAD
    assert "[DEAD-MAN] recorder_stale" in capsys.readouterr().out
    assert status.main([*configured, "--quiet"], clock=SimClock(rig.clock.now_ns() + 5 * MINUTE)) == EXIT_DEAD
    assert capsys.readouterr().out.startswith("status: [bad] recorder_stale: ")
    assert status.main([*configured, "--quiet"], clock=rig.clock) == EXIT_OK and capsys.readouterr().out == ""


def test_the_reading_as_json_is_the_same_reading(configured, tmp_path, capsys):
    rig = Host(tmp_path).record(12)
    assert status.main([*configured, "--json"], clock=rig.clock) == EXIT_OK
    document = json.loads(capsys.readouterr().out)
    assert document["recorder"]["state"] == "running" and document["level"] == "ok" and document["exit_code"] == 0
    assert document["weeks"][0]["session"] == "2026-W42" and document["problems"] == []


def test_with_alert_the_command_pages_on_the_change_and_says_where_the_page_went(configured, tmp_path, capsys):
    rig = Host(tmp_path).record(12)
    late = SimClock(rig.clock.now_ns() + 5 * MINUTE)
    assert status.main([*configured, "--alert", "--quiet"], clock=late) == EXIT_DEAD
    first = capsys.readouterr().err
    assert "alerts: no address is configured, so not sent. ALERT [hft:recorder_stale]" in first
    assert status.main([*configured, "--alert", "--quiet"], clock=late) == EXIT_DEAD
    assert capsys.readouterr().err == "", "the same problem is not paged twice"
    assert json.loads((tmp_path / "var" / "alerts" / "state.json").read_text())["hft:recorder_stale"]["firing"]


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [("--config", "missing.yaml", "cannot read"), ("--calendar", "config/calender", "is not a directory"),
     ("--recorder-config", "config/ops.yaml", "needs a root")],
)  # fmt: skip
def test_a_setting_it_cannot_read_is_its_own_exit_code(configured, option, value, message, capsys):
    arguments = list(configured)
    arguments[arguments.index(option) + 1] = value
    assert status.main(arguments, clock=SimClock(OPEN)) == EXIT_UNREADABLE
    assert message in capsys.readouterr().err
