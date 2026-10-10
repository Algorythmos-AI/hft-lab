"""Replay: the recording gives back the stream the live process saw, and a damaged day is found out."""
from __future__ import annotations

import hashlib
import json
import time
import tracemalloc

import pytest

from hftlab.calendar import Calendar
from hftlab.core import Instrument, InstrumentKind, Quote, SimClock
from hftlab.record import format as fmt
from hftlab.record.recorder import Recorder, RecorderSettings
from hftlab.record.store import Housekeeper, StoreSettings, read_journal
from hftlab.replay import __main__ as cli
from hftlab.replay import at_ns, check, events, instruments, replay
from hftlab.venues.fake import FakeConfig
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.record.kit import HOUR, MS, ROOMY, SECOND, T0, a_store
from tests.record.test_recorder import WALK, Rig

THRESHOLD = 5 * SECOND


class Consumer:
    """Stands for strategy code: integer arithmetic on the events and the clock, written out line by line."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.ema_x1000: dict[str, int] = {}
        self.chain = hashlib.sha256()

    def take(self, event, now_ns: int) -> None:
        if isinstance(event, Quote):
            before = self.ema_x1000.get(event.pair, event.mid_x2 * 1000)
            self.ema_x1000[event.pair] = (before * 9 + event.mid_x2 * 1000) // 10
            line = f"{now_ns} quote {event.pair} {event.bid} {event.ask} {event.seq} {self.ema_x1000[event.pair]}"
        elif isinstance(event, fmt.Heartbeat):
            line = f"{now_ns} quiet {event.pair} {event.server_ts_ns}"
        elif isinstance(event, ConnectionEvent):
            line = f"{now_ns} {event.state.name} {event.seq}"
        else:
            line = f"{now_ns} {'start' if event.started else 'stop'} {event.run}"
        self.chain.update(line.encode())
        self.lines.append(line)

    def output(self) -> bytes:
        return ("\n".join(self.lines) + "\n" + self.chain.hexdigest() + "\n").encode()


def a_recorded_session(tmp_path, config: FakeConfig | None = None) -> tuple[Rig, Consumer]:
    """A session with two pairs, a quiet spell, an outage, and a live consumer fed as the events are recorded."""
    rig = Rig(tmp_path, config or FakeConfig(outages=((T0 + 20 * SECOND, T0 + 26 * SECOND),)))
    live = Consumer()
    # Live, the consumer's "now" is the receive time of the event in hand, never the wall clock at that moment.
    rig.recorder._on_event = lambda event: live.take(event, at_ns(event))
    rig.recorder.start()
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + MS, count=4_000, **WALK)
    rig.market.generate_quotes("USD.JPY", start_ns=T0 + 3 * MS, count=900, **{**WALK, "mid": 151_230})
    rig.run(45 * SECOND)
    rig.recorder.stop()
    return rig, live


# ---- parity ---------------------------------------------------------------------------------------------------------


def test_a_consumer_fed_from_the_recording_writes_byte_for_byte_what_it_wrote_live(tmp_path):
    rig, live = a_recorded_session(tmp_path)
    clock, replayed = SimClock(), Consumer()
    for event in replay(rig.root / "2026-10-14", clock):
        replayed.take(event, clock.now_ns())
    assert replayed.output() == live.output()
    kinds = {line.split()[1] for line in live.lines}
    assert len(live.lines) > 3_000 and kinds >= {"quote", "quiet", "DISCONNECTED", "start", "stop"}


def test_parity_survives_compression_of_the_day(tmp_path):
    rig, live = a_recorded_session(tmp_path)
    assert Housekeeper(rig.root, ROOMY).run(T0 + 2 * HOUR).compressed == 1
    clock, replayed = SimClock(), Consumer()
    for event in replay(rig.root / "2026-10-14", clock):
        replayed.take(event, clock.now_ns())
    assert replayed.output() == live.output()


def test_replay_moves_the_clock_to_each_event_before_handing_it_over(tmp_path):
    rig, _ = a_recorded_session(tmp_path)
    clock = SimClock()
    seen = [(clock.now_ns(), at_ns(event)) for event in replay(rig.root / "2026-10-14", clock)]
    assert all(now == at for now, at in seen) and [at for _, at in seen] == sorted(at for _, at in seen)
    assert seen[0][0] == T0 and clock.now_ns() == T0 + 45 * SECOND


def test_a_clock_corrected_backwards_while_recording_does_not_take_the_replay_back(tmp_path):
    store = a_store(tmp_path / "rec")
    for at in (T0 + 10 * SECOND, T0 + 8 * SECOND, T0 + 12 * SECOND):
        store.write(fmt.QUOTE, at, store.next_index + 1, "EUR.USD", 108_500, 108_510, 1, 1)
    store.close()
    clock = SimClock()
    times = [clock.now_ns() - T0 for _ in replay(tmp_path / "rec" / "2026-10-14", clock)]
    assert times == [10 * SECOND, 10 * SECOND, 12 * SECOND]
    assert check(tmp_path / "rec" / "2026-10-14", Calendar(), THRESHOLD).clock_steps_back == 1


# ---- the check ------------------------------------------------------------------------------------------------------


def test_a_clean_day_checks_out_and_its_gaps_are_all_explained(tmp_path):
    rig, _ = a_recorded_session(tmp_path)
    report = check(rig.root / "2026-10-14", Calendar(), THRESHOLD)
    assert report.ok and report.problems == [] and not report.torn_tail
    assert (report.segments, report.runs, report.first_index) == (1, 1, 0) and report.last_index == report.records - 1
    assert report.quotes > 3_000 and report.heartbeats > 10
    assert report.gaps == {"connection_lost": 2} and report.unexplained == 0


def connected_store(root):
    """A store whose stream begins as a recorder's does: a start, then a connection that is up."""
    store = a_store(root)
    store.write(fmt.START, T0, 0, None, store.run)
    store.write(fmt.CONNECTION, T0, 1, None, fmt.STATE_CODES[ConnectionState.RECONCILING])
    return store


def test_a_silence_nothing_explains_is_counted_as_unexplained(tmp_path):
    store = connected_store(tmp_path / "rec")
    for at in (T0, T0 + SECOND, T0 + 90 * SECOND):
        store.write(fmt.QUOTE, at, store.next_index + 1, "EUR.USD", 108_500, 108_510, 1, 1)
    store.close()
    report = check(tmp_path / "rec" / "2026-10-14", Calendar(), THRESHOLD)
    assert report.ok and report.gaps == {"unexplained": 1} and report.unexplained == 1


def damage(path, offset_from_end: int) -> None:
    whole = bytearray(path.read_bytes())
    whole[-offset_from_end] ^= 0xFF
    path.write_bytes(bytes(whole))


def test_a_damaged_block_in_the_middle_of_a_day_is_a_problem(tmp_path):
    rig, _ = a_recorded_session(tmp_path)
    segment = rig.root / "2026-10-14" / "03.seg"
    damage(segment, segment.stat().st_size // 2)
    report = check(rig.root / "2026-10-14", Calendar(), THRESHOLD)
    assert not report.ok and any("checksum" in p or "no block" in p for p in report.problems)


def test_a_torn_tail_on_the_last_segment_is_noted_not_a_problem(tmp_path):
    rig, _ = a_recorded_session(tmp_path)
    segment = rig.root / "2026-10-14" / "03.seg"
    segment.write_bytes(segment.read_bytes()[:-30])
    report = check(rig.root / "2026-10-14", Calendar(), THRESHOLD)
    assert report.torn_tail and report.ok


def two_hours(tmp_path):
    store = connected_store(tmp_path / "rec")
    for hour in range(2):
        for i in range(40):
            at = T0 + hour * HOUR + i * SECOND
            store.write(fmt.QUOTE, at, store.next_index + 1, "EUR.USD", 108_500, 108_510, 1, 1)
    store.close()
    return tmp_path / "rec" / "2026-10-14"


def test_a_torn_tail_on_an_earlier_segment_and_a_missing_segment_are_problems(tmp_path):
    day = two_hours(tmp_path)
    (day / "03.seg").write_bytes((day / "03.seg").read_bytes()[:-30])
    report = check(day, Calendar(), THRESHOLD)
    assert not report.ok and any("not the last" in p for p in report.problems)
    assert any("index" in p for p in report.problems)


def test_a_journal_that_does_not_say_what_the_records_imply_is_a_problem(tmp_path):
    day = two_hours(tmp_path)
    assert check(day, Calendar(), THRESHOLD).ok
    journal = day / "journal.jsonl"
    gaps = [line for line in read_journal(day) if line["type"] == "gap"]
    assert [g["reason"] for g in gaps] == ["unexplained"], "forty minutes of the first hour had no data"
    journal.write_text("".join(line + "\n" for line in journal.read_text().splitlines() if '"gap"' not in line))
    report = check(day, Calendar(), THRESHOLD)
    assert not report.ok and "do not match" in report.problems[0]


def test_a_venue_sequence_that_goes_back_within_a_run_is_a_problem(tmp_path):
    store = a_store(tmp_path / "rec")
    for seq in (5, 6, 6, 4):
        store.write(fmt.QUOTE, T0 + seq, seq, "EUR.USD", 108_500, 108_510, 1, 1)
    store.close()
    report = check(tmp_path / "rec" / "2026-10-14", Calendar(), THRESHOLD)
    assert len([p for p in report.problems if "venue sequence" in p]) == 2


# ---- the command ----------------------------------------------------------------------------------------------------


def test_the_check_command_prints_a_report_and_its_exit_code_says_whether_the_day_is_intact(tmp_path, capsys):
    rig, _ = a_recorded_session(tmp_path)
    day = rig.root / "2026-10-14"
    assert cli.main(["--day", str(day), "--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["unexplained_gaps"] == 0 and report["gaps"] == {"connection_lost": 2}
    assert cli.main(["--day", str(day)]) == 0
    assert json.loads(capsys.readouterr().out)["events"]["Quote"] == report["quotes"]
    damage(day / "03.seg", (day / "03.seg").stat().st_size // 2)
    assert cli.main(["--day", str(day), "--check"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert cli.main(["--day", str(day)]) == 1 and cli.main(["--day", str(tmp_path / "nowhere"), "--check"]) == 2


def test_strict_makes_an_unexplained_gap_a_failure(tmp_path, capsys):
    day = two_hours(tmp_path)
    assert cli.main(["--day", str(day), "--check"]) == 0
    assert cli.main(["--day", str(day), "--check", "--strict"]) == 1
    assert json.loads(capsys.readouterr().out.split("}\n{")[0] + "}")["unexplained_gaps"] == 1


# ---- memory and throughput ------------------------------------------------------------------------------------------

PAIRS = ("EUR.USD", "USD.JPY", "GBP.USD", "AUD.USD")


class Synthetic:
    """A quote source that makes its quotes as they are asked for: four pairs, a hundred updates a second each."""

    name, state = "synthetic", ConnectionState.RECONCILING

    def __init__(self, clock: SimClock) -> None:
        self.clock, self.seq = clock, 0

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def instrument(self, pair, kind):
        return Instrument.from_pair(pair, price_scale=5, min_order_size=20_000, kind=kind, price_increment=5)

    def subscribe_quotes(self, instrument) -> None: ...

    def unsubscribe_quotes(self, instrument) -> None: ...

    def server_time_ns(self) -> int:
        return self.clock.now_ns()

    def poll(self, timeout_ns: int = 0) -> list:
        self.clock.advance(10 * MS)
        now, out = self.clock.now_ns(), []
        for number, pair in enumerate(PAIRS):
            self.seq += 1
            bid = 100_000 + number * 1_000 + self.seq % 97
            out.append(Quote(pair, bid, bid + 10, 1_000_000, 2_000_000, now, self.seq))
        return out


def record_synthetic(root, seconds: int) -> tuple[int, float]:
    clock = SimClock(T0)
    settings = RecorderSettings(pairs=tuple((pair, InstrumentKind.CASH) for pair in PAIRS))
    store = a_store(root, StoreSettings(1 << 30, 24 * HOUR, fsync=False), pairs=PAIRS, opened=False)
    recorder = Recorder(Synthetic(clock), store, clock, settings)
    began = time.perf_counter()
    recorder.run(lambda: clock.now_ns() >= T0 + seconds * SECOND)
    return recorder.records, time.perf_counter() - began


def test_a_long_session_at_four_hundred_updates_a_second_is_recorded_fast_and_in_little_memory(tmp_path):
    records, elapsed = record_synthetic(tmp_path / "speed", 300)
    rate = records / elapsed
    assert records >= 300 * 400 and rate > 5_000, f"{rate:.0f} events a second"
    tracemalloc.start()
    record_synthetic(tmp_path / "memory", 120)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, f"peak {peak} bytes"
    report = check(tmp_path / "speed" / "2026-10-14", Calendar(), THRESHOLD)
    assert report.ok and report.records == records and report.gaps == {}
    print(f"\nrecorder: {records} events in {elapsed:.2f} s = {rate:,.0f} events/s; peak memory {peak / 1024:.0f} kB")


def test_reading_back_is_fast_too(tmp_path):
    records, _ = record_synthetic(tmp_path / "rec", 120)
    began = time.perf_counter()
    count = sum(1 for _ in events(tmp_path / "rec" / "2026-10-14"))
    assert count == records and count / (time.perf_counter() - began) > 5_000


@pytest.mark.parametrize("seconds", [1])
def test_the_synthetic_source_is_what_the_test_says_it_is(tmp_path, seconds):
    records, _ = record_synthetic(tmp_path / "rec", seconds)
    assert records == 2 + seconds * 400  # a start, the quotes, a stop


# ---- a day says what its instruments are ---------------------------------------------------------------------------


def test_a_recorded_day_carries_its_instruments_in_every_hour_of_its_journal(tmp_path):
    rig = Rig(tmp_path)
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + 30 * MS, count=50, **WALK)
    rig.recorder.start()
    rig.run(2 * SECOND)
    rig.clock.advance_to(T0 + HOUR + SECOND)  # into the next hour: the description is written again
    rig.market.generate_quotes("EUR.USD", start_ns=T0 + HOUR + 2 * SECOND, count=50, **WALK)
    rig.run(3 * SECOND)
    expected = rig.broker.instrument("EUR.USD", InstrumentKind.CASH)
    rig.recorder.stop()
    day = tmp_path / "rec" / "2026-10-14"
    found = instruments(day)
    assert sorted(found) == ["EUR.USD", "USD.JPY"]
    assert found["EUR.USD"] == expected
    assert (found["USD.JPY"].price_scale, found["USD.JPY"].quote) == (3, "JPY")
    described = [line for line in read_journal(day) if line.get("type") == "instruments"]
    assert len(described) == 2, "once when subscribed, once more at the start of the next hour"
    assert check(day, Calendar(), 5 * SECOND).ok, "the description does not disturb the gap journal"


def test_a_day_that_describes_a_pair_two_ways_or_not_at_all_is_not_priced(tmp_path):
    day = tmp_path / "2026-10-14"
    day.mkdir()
    assert instruments(day) == {}
    one = {"pair": "EUR.USD", "kind": "CASH", "price_scale": 5, "min_order_size": 20_000, "size_increment": 1}
    lines = [
        {"type": "instruments", "instruments": [{**one, "price_increment": 5}]},
        {"type": "instruments", "instruments": [{**one, "price_increment": 5}]},
    ]
    (day / "journal.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert instruments(day)["EUR.USD"].price_increment == 5
    for bad in ({**one, "price_increment": 1}, {**one, "price_increment": "5"}, {**one, "kind": "SWAP"}, one):
        text = json.dumps({"type": "instruments", "instruments": [bad]}) + "\n"
        with (day / "journal.jsonl").open("a") as stream:
            stream.write(text)
        with pytest.raises(fmt.CorruptSegment):
            instruments(day)
        kept = (day / "journal.jsonl").read_text().removesuffix(text)
        (day / "journal.jsonl").write_text(kept)
