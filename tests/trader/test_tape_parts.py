"""The tape on disk: a head and parts cut by UTC day and by size, compressed when closed, counted against a quota.

What must hold: the parts read in order are the tape as one file would have held it, so a replay is the same
byte for byte wherever the cuts fell and whether or not a part is compressed; and the tapes never grow past
their quota.
"""
from __future__ import annotations

import gzip
import json
import shutil

import pytest
import yaml

from hftlab.trader import TapeError, tape
from hftlab.trader.__main__ import main, watch_tapes
from hftlab.trader.config import PART_BYTES, TraderConfigError, load, parse
from hftlab.trader.tape import TapeFull, being_written, heads, housekeep, parts, read_tape, tape_bytes
from tests.support import ROOT
from tests.trader.kit import SECOND, T0, Desk, Pages, put_stage, raw_settings

DAY = 86_400 * SECOND
MIDNIGHT = (T0 // DAY + 1) * DAY  # 2025-10-10 00:00 UTC: Thursday evening in New York, the market open
HEAD = "session-001.tape.jsonl"


def a_desk(folder, **options) -> Desk:
    put_stage(folder, "control", "min_size")
    return Desk(folder, **options)


def lines(path) -> list[list]:
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return [json.loads(raw) for raw in data.splitlines()]


def identical(desk: Desk, scratch) -> bool:
    found = desk.replay(scratch)
    return bool(found) and all(one.identical and one.stopped for one in found)


# ---- cut by UTC day ------------------------------------------------------------------------------------------------


def test_a_session_across_utc_midnight_is_cut_there_between_two_turns_and_replays_byte_for_byte(tmp_path):
    desk = a_desk(tmp_path / "live", at_ns=MIDNIGHT - 10 * SECOND)
    desk.run(25)
    desk.stop()
    head = desk.day() / HEAD
    assert [row[0] for row in lines(head)] == ["open", "marks"], "the head holds how the session began, and no turn"
    first, second = parts(head)
    assert [path.name for path in (first, second)] == [
        "session-001.part-0001.2025-10-09.jsonl", "session-001.part-0002.2025-10-10.jsonl",
    ]  # fmt: skip
    before, after = lines(first), lines(second)
    assert before[0][0] == "step" and after[0][0] == "step", "a part begins with a turn"
    assert max(row[1] for row in before if row[0] == "step") < MIDNIGHT <= after[0][1]
    assert after[-1][0] == "stop" and "sent" in {row[0] for row in before} | {row[0] for row in after}
    assert read_tape(head) == lines(head) + before + after, "the parts in order are the tape"
    assert len(heads(desk.day())) == 1 and desk.of("order"), "one session, and it traded on both sides of the cut"
    assert identical(desk, tmp_path / "scratch")


# ---- cut by size, and compressed -----------------------------------------------------------------------------------


def test_a_part_is_closed_at_the_first_turn_after_it_is_full_and_a_turn_is_never_split(tmp_path):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000})
    desk.run(20)
    desk.stop()
    found = parts(desk.day() / HEAD)
    numbers = [path.name[12:21] for path in found]
    assert len(found) > 8 and numbers == [f"part-{n:04d}" for n in range(1, len(found) + 1)]
    for path in found[:-1]:
        rows, size = lines(path), path.stat().st_size
        last_turn = max(n for n, row in enumerate(rows) if row[0] == "step")
        before_it = sum(len(json.dumps(row, separators=(",", ":"))) + 1 for row in rows[:last_turn])
        assert rows[0][0] == "step" and size >= 3_000 > before_it, "full, and not before its last turn began"
    assert identical(desk, tmp_path / "scratch")


def test_closed_parts_are_compressed_and_the_day_still_replays_byte_for_byte(tmp_path):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000}, at_ns=MIDNIGHT - 10 * SECOND)
    desk.run(25)
    desk.stop()
    days, head = tmp_path / "live" / "days", desk.day() / HEAD
    rows, used, count = read_tape(head), tape_bytes(days), len(parts(head))
    assert housekeep(days) == count, "the session said it stopped: every part is closed"
    assert all(path.suffix == ".gz" for path in parts(head)) and head.is_file(), "the head keeps its name"
    assert read_tape(head) == rows and tape_bytes(days) < used
    assert housekeep(days) == 0, "and a second pass finds nothing to do"
    assert identical(desk, tmp_path / "scratch")
    assert len(heads(desk.day())) == 1, "the session counts heads, and compression took none away"


def test_the_part_a_running_trader_is_writing_is_left_alone_and_is_closed_once_a_later_session_exists(tmp_path):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000})
    desk.run(10)
    days, head = tmp_path / "live" / "days", desk.day() / HEAD
    open_now = parts(head)[-1]
    assert being_written(days) == open_now
    assert housekeep(days) == len(parts(head)) - 1
    assert parts(head)[-1] == open_now and open_now.suffix == ".jsonl"
    desk.run(5)  # the trader goes on writing into it
    desk.restart()  # a kill, and a new session on the same files
    assert being_written(days) is None, "the new session has written its head and no turn yet"
    desk.run(1)
    assert being_written(days) == parts(desk.day() / "session-002.tape.jsonl")[-1]
    housekeep(days)
    assert all(path.suffix == ".gz" for path in parts(head)), "the dead session's last part is closed now"
    desk.run(5)
    desk.stop()
    assert being_written(days) is None and [one.identical for one in desk.replay(tmp_path / "scratch")] == [True, True]


def test_housekeeping_stopped_between_its_two_last_steps_leaves_a_part_twice_and_it_is_read_once(tmp_path):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000})
    desk.run(10)
    desk.stop()
    head = desk.day() / HEAD
    rows, second = read_tape(head), parts(head)[1]
    with second.open("rb") as plain, gzip.open(f"{second}.gz", "wb") as packed:
        shutil.copyfileobj(plain, packed)
    assert read_tape(head) == rows, "no line twice"
    housekeep(tmp_path / "live" / "days")
    assert read_tape(head) == rows and not second.exists()


def test_a_missing_part_or_one_cut_short_before_a_later_part_is_a_hole_and_is_refused(tmp_path):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000})
    desk.run(10)
    desk.stop()
    head = desk.day() / HEAD
    second = parts(head)[1]
    whole = second.read_bytes()
    second.write_bytes(whole[:-5])
    with pytest.raises(TapeError, match="ends in the middle of a line, and a later part follows"):
        read_tape(head)
    second.unlink()
    with pytest.raises(TapeError, match="one is missing, and the tape has a hole"):
        read_tape(head)
    with pytest.raises(TapeError, match="a hole"):
        desk.replay(tmp_path / "scratch")


# ---- the quota -----------------------------------------------------------------------------------------------------


def test_a_session_that_reaches_the_quota_ends_as_one_told_to_stop_and_the_next_is_refused(tmp_path):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000, "quota_bytes": 20_000})
    days = tmp_path / "live" / "days"
    assert desk.run(60) is False and desk.edge.full
    desk.stop()
    used = tape_bytes(days)
    assert 20_000 <= used < 20_000 + 3_000, "it took no turn after the one that reached the quota"
    assert read_tape(desk.day() / HEAD)[-1][0] == "stop" and identical(desk, tmp_path / "scratch")
    before = sorted(path.name for path in desk.day().iterdir())
    with pytest.raises(TapeFull, match="no session is begun until room is made"):
        desk.session()
    assert sorted(path.name for path in desk.day().iterdir()) == before, "refused with nothing written"
    housekeep(days)  # compressing the closed parts is one way to make room
    assert tape_bytes(days) < 20_000
    desk.start()
    desk.run(2)
    assert len(heads(desk.day())) == 2


def test_without_a_quota_nothing_is_counted_and_nothing_stops(tmp_path):
    desk = a_desk(tmp_path / "live")
    assert desk.run(5) is False and not desk.edge.full


def test_the_owner_is_paged_once_when_the_tapes_are_near_their_quota_and_once_when_they_are_not(tmp_path):
    settings = parse(raw_settings(state_dir=str(tmp_path), tape={"part_bytes": 100, "quota_bytes": 1_000}))
    pages = Pages()
    (tmp_path / "days" / "2025-10-09").mkdir(parents=True)
    part = tmp_path / "days" / "2025-10-09" / "session-001.part-0001.2025-10-09.jsonl"
    part.write_bytes(b"x" * 899)
    watch_tapes(settings, pages)
    assert pages.fired == [] and pages.resolved == ["tape_near_quota"]
    part.write_bytes(b"x" * 900)
    watch_tapes(settings, pages)
    assert pages.fired == ["tape_near_quota"]
    (tmp_path / "days" / "2025-10-09" / "decisions.jsonl").write_bytes(b"x" * 5_000)
    assert tape_bytes(tmp_path / "days") == 900, "the decision journal is the record and is not counted"


# ---- what reaches the disk -------------------------------------------------------------------------------------------


def test_with_fsync_on_the_part_is_synced_on_every_beat_and_every_file_when_it_is_closed(tmp_path, monkeypatch):
    synced = []
    real = tape.os.fsync
    monkeypatch.setattr(tape.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    desk = a_desk(tmp_path / "live", edge={"fsync": True})
    at_start = len(synced)
    assert at_start >= 2, "the head, and the name of the first file in its folder"
    desk.run(10)
    beats = sum(1 for row in read_tape(desk.day() / HEAD) if row[0] == "step" and row[4] is not None)
    assert beats >= 9 and len(synced) - at_start >= beats, "a power cut loses what was written since the last beat"
    quiet = a_desk(tmp_path / "quiet")
    synced.clear()
    quiet.run(10)
    assert synced == [], "with fsync off nothing is synced until a file is closed"
    quiet.stop()
    assert synced, "and then it is"


# ---- the settings and the command ------------------------------------------------------------------------------------


def test_the_shipped_settings_cut_and_bound_the_tape_and_a_block_that_is_wrong_is_refused():
    shipped = load(ROOT / "config" / "trader.yaml")
    assert shipped.tape_part_bytes == PART_BYTES == tape.PART_BYTES and shipped.tape_quota_bytes == 1_000_000_000
    assert parse(raw_settings()).tape_quota_bytes is None, "a file without the block sets no quota"
    for block in ({"part_bytes": 10}, {"part_bytes": 10, "quota_bytes": 5}, {"part_bytes": 0, "quota_bytes": 5},
                  {"part_bytes": 10, "quota_bytes": 50, "keep": 1}, "64MB"):  # fmt: skip
        with pytest.raises(TraderConfigError):
            parse(raw_settings(tape=block))


def test_the_housekeeping_command_compresses_closed_parts_and_says_what_the_tapes_use(tmp_path, capsys):
    desk = a_desk(tmp_path / "live", edge={"part_bytes": 3_000})
    desk.run(10)
    desk.stop()
    config = tmp_path / "trader.yaml"
    config.write_text(yaml.safe_dump(raw_settings(state_dir=str(tmp_path / "live"))))
    assert main(["--config", str(config), "--housekeep"]) == 0
    said = capsys.readouterr().err
    count = len(parts(desk.day() / HEAD))
    assert f"tapes: compressed {count} closed part(s); {tape_bytes(tmp_path / 'live' / 'days')} bytes on disk" in said
    assert main(["--config", str(config), "--housekeep"]) == 0 and "compressed 0 closed" in capsys.readouterr().err
