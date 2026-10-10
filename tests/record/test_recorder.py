"""The recorder against the fake broker: what it writes, what it survives, and that it cannot trade."""
from __future__ import annotations

import ast
import errno
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from hftlab.calendar import Calendar
from hftlab.core import InstrumentKind, SimClock
from hftlab.record import __main__ as cli
from hftlab.record import format as fmt
from hftlab.record.recorder import EXIT_IO, EXIT_OK, EXIT_QUOTA, Recorder, RecorderSettings
from hftlab.record.segment import SegmentWriter
from hftlab.record.store import StoreSettings, days, read_day, read_journal
from hftlab.replay import check
from hftlab.venues.fake import FakeConfig, build
from hftlab.venues.port import ConnectionState
from hftlab.venues.supervisor import Backoff
from hftlab.venues.view import MarketDataView
from tests.record.kit import HOUR, MS, PAIRS, ROOMY, SECOND, T0, a_disk, a_store, everything
from tests.support import ROOT, called_name, sources
from tests.venues.kit import INSTRUMENTS

SETTINGS = RecorderSettings(pairs=(("EUR.USD", InstrumentKind.CASH), ("USD.JPY", InstrumentKind.CASH)))
WALK = {"interval_ns": 10 * MS, "mid": 108_505, "spread": 10, "step": 5}


class Rig:
    """A recorder over a view of the fake broker, with a simulated clock the test moves."""

    def __init__(
        self, tmp_path, config: FakeConfig | None = None, store_settings: StoreSettings = ROOMY, **store_options
    ) -> None:
        self.clock = SimClock(T0)
        self.broker = build(self.clock, INSTRUMENTS, config or FakeConfig(), seed=5, backoff=Backoff())
        self.market = self.broker.market
        self.root = tmp_path / "rec"
        self.seen: list = []
        # the recorder opens the store when it starts
        self.store = a_store(self.root, store_settings, opened=False, **store_options)
        self.recorder = Recorder(
            MarketDataView(self.broker), self.store, self.clock, SETTINGS,
            status_path=self.root / "status.json", on_event=self.seen.append,
        )  # fmt: skip

    def run(self, duration_ns: int, step_ns: int = 20 * MS) -> None:
        end = self.clock.now_ns() + duration_ns
        while self.clock.now_ns() < end:
            self.clock.advance(step_ns)
            self.recorder.step()

    def kinds(self) -> list[int]:
        return [record.kind for record in everything(self.root)]


def started(tmp_path, **options) -> Rig:
    rig = Rig(tmp_path, **options)
    rig.recorder.start()
    return rig


# ---- what it writes -------------------------------------------------------------------------------------------------


def test_a_run_is_a_start_the_connection_the_quotes_in_order_and_a_stop(tmp_path):
    rig = started(tmp_path)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=300, **WALK)
    rig.run(4 * SECOND)
    rig.recorder.stop()
    found = everything(rig.root)
    assert [r.index for r in found] == list(range(len(found)))
    assert found[0].kind == fmt.START and found[0].a == 1 and found[-1].kind == fmt.STOP
    states = [fmt.STATES[r.a].name for r in found if r.kind == fmt.CONNECTION]
    assert states == ["CONNECTING", "RECONCILING"]
    quotes = [r for r in found if r.kind == fmt.QUOTE]
    assert len(quotes) > 100 and [q.venue_seq for q in quotes] == sorted(q.venue_seq for q in quotes)
    assert all(q.b - q.a == 10 and q.a % 5 == 0 for q in quotes), "the book as it was, in integers"


def test_what_is_recorded_is_exactly_what_the_venue_reported(tmp_path):
    rig = started(tmp_path)
    rig.market.script_quote(T0 + 5 * MS, "EUR.USD", 108_500, 108_510, 700_000, 900_000)
    rig.market.script_quote(T0 + 6 * MS, "USD.JPY", 151_230, 151_240, 100_000, 200_000)
    rig.run(100 * MS)
    rig.recorder.stop()
    quotes = [(r, pairs) for day in days(rig.root) for r, pairs in read_day(day) if r.kind == fmt.QUOTE]
    events = [fmt.to_event(r, pairs) for r, pairs in quotes]
    assert [(e.pair, e.bid, e.ask, e.bid_size, e.ask_size) for e in events] == [
        ("EUR.USD", 108_500, 108_510, 700_000, 900_000), ("USD.JPY", 151_230, 151_240, 100_000, 200_000),
    ]  # fmt: skip
    assert all(e.recv_ts_ns == T0 + 20 * MS for e in events), "the receive time is the injected clock's"


def test_a_quiet_book_is_recorded_as_quiet_once_a_second_and_a_busy_one_is_not(tmp_path):
    rig = started(tmp_path)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=1_000, **WALK)
    rig.run(5 * SECOND)
    rig.recorder.stop()
    beats = [fmt.to_event(r, PAIRS) for r in everything(rig.root) if r.kind == fmt.HEARTBEAT]
    assert {b.pair for b in beats} == {"USD.JPY"} and len(beats) == 5
    assert [b.recv_ts_ns - T0 for b in beats] == [n * SECOND for n in range(1, 6)]
    assert all(b.server_ts_ns == b.recv_ts_ns for b in beats), "the venue's own clock, as it answered"
    assert [g for g in read_journal(rig.root / "2026-10-14") if g["type"] == "gap"] == []


def test_no_heartbeat_is_written_while_the_venue_does_not_answer(tmp_path):
    rig = started(tmp_path, config=FakeConfig(outages=((T0 + 2 * SECOND, T0 + 30 * SECOND),)))
    rig.run(20 * SECOND)
    rig.recorder.stop()
    beats = [r.recv_ts_ns - T0 for r in everything(rig.root) if r.kind == fmt.HEARTBEAT]
    assert beats and max(beats) <= 2 * SECOND + 20 * MS


# ---- what it survives -----------------------------------------------------------------------------------------------


def test_it_records_through_a_disconnect_and_the_journal_explains_the_silence(tmp_path):
    rig = started(tmp_path, config=FakeConfig(outages=((T0 + 2 * SECOND, T0 + 20 * SECOND),)))
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=6_000, **WALK)
    rig.run(40 * SECOND)
    rig.recorder.stop()
    found = everything(rig.root)
    states = [fmt.STATES[r.a].name for r in found if r.kind == fmt.CONNECTION]
    assert states[:3] == ["CONNECTING", "RECONCILING", "DISCONNECTED"] and states[-1] == "RECONCILING"
    quote_times = [r.recv_ts_ns - T0 for r in found if r.kind == fmt.QUOTE]
    assert any(t < 2 * SECOND for t in quote_times) and any(t > 32 * SECOND for t in quote_times)
    assert not any(2 * SECOND + 20 * MS < t < 20 * SECOND for t in quote_times)
    gaps = [g for g in read_journal(rig.root / "2026-10-14") if g["type"] == "gap"]
    assert {g["pair"] for g in gaps} == {"EUR.USD", "USD.JPY"} and {g["reason"] for g in gaps} == {"connection_lost"}
    assert [r.index for r in found] == list(range(len(found)))


def test_a_killed_recorder_loses_at_most_the_last_quarter_second_and_the_next_run_explains_the_gap(tmp_path):
    rig = started(tmp_path)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=2_000, **WALK)
    rig.run(10 * SECOND)
    in_memory = [e for e in rig.seen if getattr(e, "recv_ts_ns", 0)]
    on_disk = everything(rig.root)  # the process dies here: no stop, no flush
    lost = len(rig.seen) - len(on_disk)
    assert 0 <= lost <= ROOMY.block_records and in_memory[-1].recv_ts_ns - on_disk[-1].recv_ts_ns <= 250 * MS + 20 * MS

    second = Rig(tmp_path)
    second.clock.advance(70 * SECOND)
    second.recorder.start()
    second.market.script_quote(T0 + 70 * SECOND + 30 * MS, "EUR.USD", 108_600, 108_610)
    second.run(SECOND)
    second.recorder.stop()
    found = everything(rig.root)
    assert [r.index for r in found] == list(range(len(found))) and [r.a for r in found if r.kind == fmt.START] == [1, 2]
    gaps = [g for g in read_journal(rig.root / "2026-10-14") if g["type"] == "gap" and g["pair"] == "EUR.USD"]
    assert [g["reason"] for g in gaps] == ["process_down"]


def test_the_status_file_says_what_the_recorder_is_doing(tmp_path):
    rig = started(tmp_path)
    rig.run(6 * SECOND)
    status = json.loads((rig.root / "status.json").read_text())
    assert status["status"] == "recording" and status["connection"] == "RECONCILING" and status["venue"] == "fake"
    assert status["run"] == 1 and status["pid"] == os.getpid() and status["used_bytes"] <= status["quota_bytes"]
    assert status["at_ns"] >= T0 + 5 * SECOND and not list(rig.root.glob("*.partial"))
    rig.recorder.stop()
    assert json.loads((rig.root / "status.json").read_text())["status"] == "stopped"


def test_at_the_quota_it_stops_says_so_loudly_and_exits_with_its_own_code(tmp_path, capsys):
    tight = StoreSettings(quota_bytes=6_000, keep_ns=HOUR, block_records=8, fsync=False)
    rig = Rig(tmp_path, store_settings=tight)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=3_000, **WALK)

    def advance() -> bool:
        rig.clock.advance(20 * MS)
        return rig.clock.now_ns() > T0 + 60 * SECOND

    assert rig.recorder.run(advance) == EXIT_QUOTA
    assert "RECORDER STOPPED: QUOTA REACHED" in capsys.readouterr().err
    assert sum(p.stat().st_size for p in rig.root.rglob("*.seg")) <= 6_000
    assert json.loads((rig.root / "status.json").read_text())["status"] == "quota"
    assert any(line["type"] == "quota" for line in read_journal(rig.root / "2026-10-14"))
    assert rig.clock.now_ns() < T0 + 60 * SECOND, "it stopped when the quota was reached, not at the end"


def test_run_returns_zero_when_asked_to_stop(tmp_path):
    rig = Rig(tmp_path)
    turns = iter([False, False, True])
    assert rig.recorder.run(lambda: next(turns)) == EXIT_OK and rig.kinds()[-1] == fmt.STOP


def until_the_disk_fills(rig: Rig, disk, at_ns: int = T0 + 2 * SECOND, also=lambda: None):
    """A `should_stop` that moves the clock, and fills the disk once the clock passes `at_ns`."""

    def advance() -> bool:
        rig.clock.advance(20 * MS)
        if rig.clock.now_ns() > at_ns and not disk.full:
            disk.full = True
            also()
        return rig.clock.now_ns() > T0 + 60 * SECOND

    return advance


def test_a_write_the_system_refuses_stops_it_with_its_own_code_status_and_journal_line(tmp_path, capsys):
    """A full disk is not the quota. It must not look like one, and it must not escape as a crash."""
    disk = a_disk()
    rig = Rig(tmp_path, writer=disk)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=3_000, **WALK)

    assert rig.recorder.run(until_the_disk_fills(rig, disk)) == EXIT_IO
    said = capsys.readouterr().err
    assert "RECORDER STOPPED: I/O ERROR" in said and "ENOSPC" in said and "QUOTA" not in said
    assert rig.clock.now_ns() < T0 + 3 * SECOND, "it stopped when the write was refused, not at the end"
    assert json.loads((rig.root / "status.json").read_text())["status"] == "io-error"
    journal = read_journal(rig.root / "2026-10-14")
    assert [line["error"] for line in journal if line["type"] == "io-error"] == ["ENOSPC"]
    assert journal[-1]["type"] == "stop" and journal[-1]["reason"] == "io-error"
    assert not [line for line in journal if line["type"] == "quota"]
    found = everything(rig.root)
    assert len(found) > 100 and [r.index for r in found] == list(range(len(found)))
    assert fmt.STOP not in rig.kinds(), "nothing is appended after a block the disk may have cut short"
    assert rig.broker.state is ConnectionState.DISCONNECTED, "the source is let go of"


def test_after_a_refused_write_the_next_run_continues_without_a_hole_and_the_day_still_checks_out(tmp_path, capsys):
    disk = a_disk()
    rig = Rig(tmp_path, writer=disk)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=3_000, **WALK)
    assert rig.recorder.run(until_the_disk_fills(rig, disk)) == EXIT_IO
    on_disk = len(everything(rig.root))

    second = Rig(tmp_path)  # the unit's back-off has passed and there is room again
    second.clock.advance(70 * SECOND)
    second.recorder.start()
    second.market.script_quote(T0 + 70 * SECOND + 30 * MS, "EUR.USD", 108_600, 108_610)
    second.run(SECOND)
    second.recorder.stop()
    found = everything(rig.root)
    assert len(found) > on_disk and [r.index for r in found] == list(range(len(found)))
    assert [r.a for r in found if r.kind == fmt.START] == [1, 2] and found[-1].kind == fmt.STOP
    gaps = [g for g in read_journal(rig.root / "2026-10-14") if g["type"] == "gap" and g["pair"] == "EUR.USD"]
    assert [g["reason"] for g in gaps] == ["process_down"]
    report = check(rig.root / "2026-10-14", Calendar(), 5 * SECOND)
    assert report.ok and report.unexplained == 0, report.problems
    assert json.loads((rig.root / "status.json").read_text())["status"] == "stopped"


def test_when_not_even_the_journal_or_the_status_can_be_written_it_still_stops_with_the_code(
    tmp_path, capsys, monkeypatch
):
    disk = a_disk(half=False)
    rig = Rig(tmp_path, writer=disk)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=3_000, **WALK)

    def refuse(*_arguments: object) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    def everything_else_fills_too() -> None:
        monkeypatch.setattr("hftlab.record.store.append_line", refuse)
        monkeypatch.setattr(os, "replace", refuse)

    code = rig.recorder.run(until_the_disk_fills(rig, disk, T0 + 7 * SECOND, also=everything_else_fills_too))
    monkeypatch.undo()
    said = capsys.readouterr().err
    assert code == EXIT_IO and "RECORDER STOPPED: I/O ERROR" in said
    assert said.count("the status file could not be written") == 1, "said once, not at every try"
    assert json.loads((rig.root / "status.json").read_text())["status"] == "recording", "the last one that got out"
    assert not [line for line in read_journal(rig.root / "2026-10-14") if line["type"] in ("io-error", "stop")]
    assert not list(rig.root.glob("*.partial")), "the half of a status file is not left lying about"
    found = everything(rig.root)
    assert found and [r.index for r in found] == list(range(len(found)))


def test_a_disk_that_is_full_before_the_first_record_gives_the_same_code(tmp_path, capsys):
    disk = a_disk(half=False)
    disk.full = True
    rig = Rig(tmp_path, writer=disk)
    assert rig.recorder.run(lambda: True) == EXIT_IO
    assert "RECORDER STOPPED: I/O ERROR" in capsys.readouterr().err
    assert json.loads((rig.root / "status.json").read_text())["status"] == "io-error" and everything(rig.root) == []
    disk.full = False
    second = Rig(tmp_path)
    assert second.recorder.run(lambda: True) == EXIT_OK and second.kinds() == [fmt.START, fmt.STOP]


def test_a_status_file_that_cannot_be_written_does_not_stop_the_recording(tmp_path, capsys):
    rig = Rig(tmp_path)
    (tmp_path / "in-the-way").write_text("a file where the status directory should be")
    rig.recorder._status_path = tmp_path / "in-the-way" / "status.json"
    rig.recorder.start()
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=1_500, **WALK)
    rig.run(12 * SECOND)
    rig.recorder.stop()
    assert capsys.readouterr().err.count("the status file could not be written") == 1
    assert len([r for r in everything(rig.root) if r.kind == fmt.QUOTE]) > 1_000


class BrokenSource:
    """A quote source whose socket fails: an error of the operating system that has nothing to do with the disk."""

    name = "broken"
    state = ConnectionState.DISCONNECTED

    def __init__(self) -> None:
        self.disconnected = False

    def connect(self) -> None: ...

    def disconnect(self) -> None:
        self.disconnected = True

    def poll(self, timeout_ns: int = 0) -> list:
        raise ConnectionResetError(errno.ECONNRESET, os.strerror(errno.ECONNRESET))


def test_an_error_of_the_quote_source_is_not_mistaken_for_the_disk(tmp_path, capsys):
    source, store = BrokenSource(), a_store(tmp_path / "rec", opened=False)
    recorder = Recorder(source, store, SimClock(T0), SETTINGS, status_path=tmp_path / "rec" / "status.json")
    with pytest.raises(ConnectionResetError):
        recorder.run(lambda: False)
    assert "I/O ERROR" not in capsys.readouterr().err
    assert [r.kind for r in everything(tmp_path / "rec")] == [fmt.START, fmt.STOP] and source.disconnected
    assert json.loads((tmp_path / "rec" / "status.json").read_text())["status"] == "stopped"


# ---- how long the loop is away from the source ----------------------------------------------------------------------


def test_the_status_reports_the_longest_and_the_recent_time_between_two_polls(tmp_path):
    """The flush syncs to disk on the loop's own thread. What that costs in receive time is measured, not assumed."""
    stall = {"ns": 0}

    class SlowDisk(SegmentWriter):
        def _sync(self) -> None:  # every flush ends in a sync; here it takes as long as the test says
            if stall["ns"]:
                rig.clock.advance(stall["ns"])

    def loop() -> dict:
        return json.loads((rig.root / "status.json").read_text())["loop"]

    rig = started(tmp_path, writer=SlowDisk)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=3_000, **WALK)
    rig.run(6 * SECOND)  # the rig moves the clock 20 ms between two turns of the loop, and nothing else takes time
    first = loop()
    assert first["longest_between_polls_ns"] == first["recent_between_polls_ns"] == 20 * MS
    assert first["polls"] > 200

    stall["ns"] = 35 * MS  # a second of slow syncs ...
    rig.run(SECOND)
    stall["ns"] = 0
    rig.run(4 * SECOND)  # ... seen in the status that covers it
    assert loop()["longest_between_polls_ns"] == loop()["recent_between_polls_ns"] == 55 * MS

    rig.run(5 * SECOND)  # the next status covers a quiet stretch: the longest stays, the recent one comes down
    assert loop()["longest_between_polls_ns"] == 55 * MS and loop()["recent_between_polls_ns"] == 20 * MS
    assert loop()["polls"] > first["polls"]
    rig.recorder.stop()


# ---- it cannot trade ------------------------------------------------------------------------------------------------


def test_the_view_it_is_handed_has_no_way_to_send_an_order(tmp_path):
    rig = started(tmp_path)
    view = rig.recorder._source
    public = {name for name in dir(view) if not name.startswith("_")}
    assert public == {"name", "state", "connect", "disconnect", "poll", "instrument", "subscribe_quotes",
                      "unsubscribe_quotes", "server_time_ns"}  # fmt: skip
    for name in ("place", "cancel", "mark_reconciled", "open_orders", "venue", "_venue", "__venue"):
        assert not hasattr(view, name), name
    with pytest.raises(AttributeError):
        view.extra = 1
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=200, **WALK)
    rig.run(3 * SECOND)
    assert rig.broker.state is ConnectionState.RECONCILING, "never READY: the venue itself would refuse an order"
    assert rig.market._orders == {} and rig.broker._sent == set()


ORDER_WORDS = {"place", "cancel", "mark_reconciled", "_send", "_cancel", "place_order", "cancel_order"}


def test_nothing_in_the_recorder_the_replay_or_the_view_names_an_order_method():
    for rel, tree in sources():
        if not rel.startswith(("src/hftlab/record/", "src/hftlab/replay/")) and rel != "src/hftlab/venues/view.py":
            continue
        calls = [called_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call) and called_name(n) in ORDER_WORDS]
        names = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ORDER_WORDS]
        assert calls == [] and names == [], rel


def test_only_the_view_itself_reaches_the_venue_behind_it():
    for rel, tree in sources():
        mangled = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "_MarketDataView__venue"]
        text = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "_MarketDataView__venue"]
        assert mangled == [] and text == [], rel
    feed = dict(sources())["src/hftlab/venues/ibkr/feed.py"]
    returns = [n for n in ast.walk(feed) if isinstance(n, ast.Return)]
    assert len(returns) == 1 and called_name(returns[0].value) == "MarketDataView", "the adapter never leaves whole"


# ---- the command ----------------------------------------------------------------------------------------------------


def test_the_shipped_config_loads_and_marks_its_placeholders():
    config = cli.load_config(ROOT / "config" / "recorder.yaml")
    assert config["pairs"] == (("EUR.USD", InstrumentKind.CASH), ("USD.JPY", InstrumentKind.CASH))
    assert cli.store_settings(config).keep_ns == 240 * HOUR and cli.offload_of(config) is None
    assert "PLACEHOLDER" in (ROOT / "config" / "recorder.yaml").read_text()


def test_a_dry_run_records_the_fake_broker_and_the_day_checks_out(tmp_path, capsys):
    assert cli.main(["--dry-run-seconds", "8", "--root", str(tmp_path / "rec")]) == 0
    assert "stopped after" in capsys.readouterr().err
    found = everything(tmp_path / "rec")
    assert found[0].kind == fmt.START and found[-1].kind == fmt.STOP and len(found) > 1_000
    assert {fmt.to_event(r, PAIRS).pair for r in found if r.kind == fmt.QUOTE} == {"EUR.USD", "USD.JPY"}


def test_sigterm_stops_the_process_cleanly_with_everything_flushed(tmp_path):
    command = [sys.executable, "-m", "hftlab.record", "--dry-run-seconds", "864000", "--root", str(tmp_path / "rec")]
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    child = subprocess.Popen(command, cwd=ROOT, env=environment, stderr=subprocess.PIPE)
    assert b"recording EUR.USD, USD.JPY from fake" in child.stderr.readline()
    time.sleep(0.5)
    child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=30) == 0
    child.stderr.close()
    found = everything(tmp_path / "rec")
    assert found[-1].kind == fmt.STOP and [r.index for r in found] == list(range(len(found)))
    assert json.loads((tmp_path / "rec" / "status.json").read_text())["status"] == "stopped"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [(["--venue", "sim"], "no market data of its own"), (["--venue", "ibkr_live"], "no venue named"),
     (["--config", "missing.yaml"], "cannot read"),
     (["--dry-run-seconds", "1", "--calendar", "config/calender"], "config/calender is not a directory"),
     (["--dry-run-seconds", "1", "--calendar", "config"], "config has no holidays.yaml and no macro_events.yaml")],
)  # fmt: skip
def test_the_command_refuses_what_it_cannot_record_or_read(arguments, message, tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), *arguments]) == 2
    assert message in capsys.readouterr().err and not list(tmp_path.iterdir())


def test_against_the_gateway_the_lock_comes_first(tmp_path, monkeypatch, capsys):
    """With MODE unset nothing is dialled, so this needs no network."""
    import asyncio

    monkeypatch.delenv("MODE", raising=False)
    try:
        assert cli.main(["--root", str(tmp_path / "rec")]) == 2
        assert "refused by the paper-only lock" in capsys.readouterr().err
    finally:
        asyncio.get_event_loop_policy().get_event_loop().close()
        asyncio.set_event_loop(None)


def test_housekeeping_is_its_own_command(tmp_path, capsys):
    cli.main(["--dry-run-seconds", "2", "--root", str(tmp_path / "rec")])
    capsys.readouterr()
    assert cli.main(["--housekeep", "--root", str(tmp_path / "rec")]) == 0
    assert "compressed 1" in capsys.readouterr().out
    assert [p.name for p in (tmp_path / "rec" / "2026-10-07").glob("*.seg*")] == ["03.seg.gz"]
    assert len(everything(tmp_path / "rec")) > 100


GOOD = {"pairs": [{"pair": "EUR.USD", "form": "cash"}], "quota_bytes": 10, "keep_hours": 1, "block_records": 8}
GOOD |= {"flush_ms": 1, "heartbeat_ms": 1, "gap_threshold_s": 1, "root": "var/record"}


@pytest.mark.parametrize(
    "change",
    [{"pairs": []}, {"quota_bytes": 0}, {"keep_hours": 1.5}, {"flush_ms": None}, {"pairs": ["EUR.USD"]},
     {"pairs": [{"pair": "EURUSD", "form": "cash"}]}, {"pairs": [{"pair": "EUR.USD", "form": "spot"}]}],
)  # fmt: skip
def test_a_config_that_makes_no_sense_is_refused(tmp_path, change):
    import yaml

    path = tmp_path / "recorder.yaml"
    path.write_text(yaml.safe_dump(GOOD))
    assert cli.load_config(path)["pairs"] == (("EUR.USD", InstrumentKind.CASH),)
    path.write_text(yaml.safe_dump({**GOOD, **change}))
    with pytest.raises(cli.ConfigError):
        cli.load_config(path)
    path.write_text("- a list\n")
    with pytest.raises(cli.ConfigError):
        cli.load_config(path)
