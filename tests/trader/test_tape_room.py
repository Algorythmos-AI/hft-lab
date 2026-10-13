"""The tapes at their limits: nothing is opened at the soft limit, everything is closed at the hard limit, and the
trader never stops watching a position because a byte budget ran out (ADR 0027, addendum).

Against the fake broker, with quotas of a few thousand bytes. The limits are handed to the edge directly, so a
test says where they lie; `tests/trader/test_tape_reserve.py` holds the arithmetic that sizes the reserve from
the risk limits, and the refusal of a quota that cannot hold it.
"""
from __future__ import annotations

import dataclasses
import gzip
import itertools
import json

from hftlab.risk import HARD, ROOM, SOFT
from hftlab.trader import __main__ as cli
from hftlab.trader.session import TAPE_DARK, TAPE_HARD, TAPE_OVERDUE, TAPE_SOFT
from hftlab.trader.tape import DARK, heads, housekeep, last_head, parts, read_tape, tape_bytes
from tests.trader.kit import DIGEST, MS, SECOND, Desk, put_stage, raw_settings

HEAD = "session-001.tape.jsonl"
# Where the limits lie in these tests: nothing is opened from 8,000 bytes of tape, everything is closed from 12,000.
NEAR = {"part_bytes": 3_000, "quota_bytes": 40_000, "soft_bytes": 8_000, "reserve_bytes": 28_000}
FAR = {"part_bytes": 3_000, "quota_bytes": 400_000, "soft_bytes": 8_000, "reserve_bytes": 28_000}  # hard at 372,000
LATER = {**FAR, "soft_bytes": 14_000}
TIGHT = {"part_bytes": 3_000, "quota_bytes": 13_000, "soft_bytes": 8_000, "reserve_bytes": 1_000}  # a reserve too small
COLLARS = {
    "per_pair/EUR.USD/price_collar_ticks": 10, "per_pair/EUR.USD/exit_collar_ticks": 40,
    "per_pair/EUR.USD/max_spread_ticks": 4,
}  # fmt: skip
LIMIT = 5  # seconds the closing may take in these tests before the owner is paged


def a_desk(
    folder, *, hold: int = 600, edge: dict | None = None, paper: bool = True, session: bool = True, **options
) -> Desk:
    """A control sleeve on paper that holds what it buys for `hold` seconds, and a closing that may take five."""
    if paper:
        put_stage(folder, "control", "min_size")
    raw = raw_settings()
    raw["sleeves"][1]["hold_seconds"] = hold
    desk = Desk(folder, settings=raw, edge=edge or NEAR, paper=paper, start=False, **options)
    desk.settings = dataclasses.replace(desk.settings, tape_flatten_ns=LIMIT * SECOND, state_dir=folder)
    desk.connect()
    if session:
        desk.session()
    return desk


def until(desk: Desk, level: int, *, feed=None, most: int = 600) -> None:
    """Let the market run, a second at a time, until the tapes are at `level`."""
    for _ in range(most):
        if desk.edge.level == level:
            return
        (feed or desk.run)(1)
    raise AssertionError(f"the tapes never reached level {level}: {desk.edge.room()}")


def blown_out(desk: Desk):
    """A feed of EUR.USD quotes sixty times as wide as usual: beyond even the exit collar of `COLLARS`."""

    def feed(seconds: int) -> None:
        for number in range(seconds * 10):
            desk.market.script_quote(desk.quoted + MS + number * 100 * MS, "EUR.USD", 108_200, 108_800)
        desk.quoted += seconds * SECOND
        desk.run(seconds, quotes=False)

    return feed


def lines(path) -> list[list]:
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return [json.loads(raw) for raw in data.splitlines()]


def after(desk: Desk, kind: str, **where) -> list[dict]:
    """The decision journal's lines after the first one of this kind (and with these values)."""
    found = desk.decisions()
    first = next(n for n, e in enumerate(found) if e["type"] == kind and all(e[k] == v for k, v in where.items()))
    return found[first + 1 :]


def identical(desk: Desk, scratch, ended: list[str]) -> bool:
    found = desk.replay(scratch)
    assert [one.as_dict()["ended"] for one in found] == ended
    return all(one.identical for one in found)


def broker_orders(desk: Desk) -> dict[str, int]:
    """What the broker executed of each order it holds."""
    return {order_id: booked.executed for order_id, booked in desk.market._orders.items()}


# ---- the soft limit --------------------------------------------------------------------------------------------------


def test_at_the_soft_limit_entries_stop_and_exits_go_on_with_one_line_and_one_page(tmp_path):
    desk = a_desk(tmp_path / "live", hold=8, edge=FAR)
    until(desk, SOFT)
    assert desk.held()[0] == 20_000, "it holds a position at the moment the soft limit is reached"
    desk.run(40)
    told = desk.of("tape")
    assert [(e["room"], e["before"], e["code"]) for e in told] == [("soft", "room", "TAPE_SOFT_LIMIT")]
    assert desk.pages.fired.count(TAPE_SOFT) == 1 and TAPE_HARD not in desk.pages.fired
    later = after(desk, "tape")
    orders = [e for e in later if e["type"] == "order"]
    verdicts = {str(e["client_order_id"]): e for e in later if e["type"] == "verdict"}
    sent = [e for e in orders if verdicts[str(e["client_order_id"])]["sent"]]
    assert len(orders) > 5 and [e["reducing"] for e in sent] == [1], "the one order that went out closed the position"
    assert verdicts[str(sent[0]["client_order_id"])]["code"] == "TAPE_SOFT_LIMIT", "allowed, as an exit, under it"
    kept = [e for e in desk.outbox() if e["type"] == "intent"][-1]
    assert (kept["client_order_id"], kept["outcome"]) == (sent[0]["client_order_id"], "EXITS_ONLY")
    refused = {str(e["code"]) for e in later if e["type"] == "refused"}
    assert refused == {"TAPE_SOFT_LIMIT"}, "every entry since was refused by the gate, under one code"
    assert desk.held() == (0, 0, 0) and desk.trader.stages.stage("control") == "min_size", "no stage was touched"
    assert not desk.trader.flat_at_hard_limit
    desk.stop()
    assert identical(desk, tmp_path / "scratch", ["stopped"])


def test_a_sleeve_that_trades_on_the_shadow_venue_alone_is_stopped_at_the_soft_limit_the_same_way(tmp_path):
    desk = a_desk(tmp_path / "live", hold=8, edge=FAR, paper=False)
    until(desk, SOFT)
    assert desk.held()[1] == 20_000
    desk.run(40)
    later = after(desk, "tape", room="soft")
    assert {str(e["code"]) for e in later if e["type"] == "refused"} == {"TAPE_SOFT_LIMIT"}
    sent = [e["reducing"] for e in later if e["type"] == "order"]
    assert sent and set(sent) == {1}, "the only orders that reached the shadow venue closed what it held"
    assert desk.trader.shadow_book.open_lines() == {}
    desk.stop()
    assert identical(desk, tmp_path / "scratch", ["stopped"])


# ---- the hard limit --------------------------------------------------------------------------------------------------


def test_at_the_hard_limit_a_held_position_is_closed_and_its_fills_and_the_stop_are_on_the_tape(tmp_path):
    desk = a_desk(tmp_path / "live")
    until(desk, SOFT)
    assert desk.held() == (20_000, 20_000, 20_000)
    for _ in range(60):
        if desk.trader.flat_at_hard_limit:
            break
        assert desk.run(1) is False
    assert desk.trader.flat_at_hard_limit and desk.held() == (0, 0, 0) and desk.positions() == {}
    told = [(e["room"], e["code"]) for e in desk.of("tape")]
    assert told == [("soft", "TAPE_SOFT_LIMIT"), ("hard", "TAPE_HARD_LIMIT")]
    closing = after(desk, "tape", room="hard")
    assert next(e["source"] for e in closing if e["type"] == "decision") == "flatten"
    assert closing[-1]["type"] == "tape_flat"
    sent = [e for e in closing if e["type"] == "verdict" and e["sent"]]
    assert [e["code"] for e in sent] == ["TAPE_HARD_LIMIT"], "one close, proposed by the gate as under FLATTEN"
    assert {str(e["code"]) for e in closing if e["type"] == "refused"} <= {"TAPE_HARD_LIMIT"}, "and nothing was opened"
    close = str(sent[0]["client_order_id"])
    assert broker_orders(desk)[close] == 20_000 and desk.pages.fired == [TAPE_SOFT, TAPE_HARD]
    summary = desk.stop()
    assert summary["books"]["open_positions"] == 0

    rows = read_tape(desk.day() / HEAD)
    at_hard = next(n for n, row in enumerate(rows) if row[0] == "step" and len(row) > 5 and row[5] == HARD)
    reserve = rows[at_hard:]
    assert ["sent", "place", close, "sent", "READY"] in reserve, "the close, as the order manager saw it go"
    events = [event for row in reserve if row[0] == "step" for event in row[3]]
    fills = [event for event in events if event[0] == "f"]
    assert [(fill[2], fill[5]) for fill in fills] == [(close, 20_000)], "its fill is on the tape"
    assert reserve[-1][0] == "stop" and reserve[-2][0] == "step"
    for row in reserve[1:-1]:
        if row[0] == "step":
            quoted = [event[1] for event in row[3] if event[0] == "q"]
            assert row[4] is not None and len(quoted) == len(set(quoted)), "thinned: a turn a beat, a quote a pair"
    used = tape_bytes(tmp_path / "live" / "days")
    assert 12_000 <= used < 12_000 + 2_000, "the closing took a small part of the reserve"
    assert identical(desk, tmp_path / "scratch", ["stopped"])


def test_a_run_that_can_send_nothing_closes_its_shadow_lines_at_the_hard_limit_as_flatten_does_and_ends(tmp_path):
    desk = a_desk(tmp_path / "live", paper=False)
    until(desk, SOFT)
    assert desk.trader.shadow_book.open_lines() == {("control", "EUR.USD"): 20_000, ("control", "USD.JPY"): -20_000}
    for _ in range(60):
        if desk.trader.flat_at_hard_limit:
            break
        desk.run(1)
    assert desk.trader.flat_at_hard_limit and [e["room"] for e in desk.of("tape")] == ["soft", "hard"]
    closing = after(desk, "tape", room="hard")
    closes = [(e["source"], e["pair"], e["side"], e["quantity"]) for e in closing if e["type"] == "decision"]
    assert closes[:2] == [("flatten", "EUR.USD", "SELL", 20_000), ("flatten", "USD.JPY", "BUY", 20_000)], (
        "closed by the trader itself, on the shadow venue, as under FLATTEN"
    )
    assert desk.trader.shadow_book.open_lines() == {} and closing[-1]["type"] == "tape_flat"
    assert (closing[-1]["overdue"], closing[-1]["shadow"]) == (0, []) and TAPE_OVERDUE not in desk.pages.fired
    desk.stop()
    assert read_tape(desk.day() / HEAD)[-1][0] == "stop" and identical(desk, tmp_path / "scratch", ["stopped"])


def test_a_shadow_line_that_cannot_be_closed_in_the_time_limit_is_written_down_and_pages_nobody(tmp_path):
    desk = a_desk(tmp_path / "live", paper=False)
    until(desk, SOFT)
    thin = lambda seconds: desk.run(seconds, size=1)  # noqa: E731  a book too thin for the shadow venue to fill on
    until(desk, HARD, feed=thin)
    assert not desk.trader.flat_at_hard_limit
    thin(LIMIT + 2)
    assert desk.trader.flat_at_hard_limit, "the shadow book holds nobody's money: the session does not wait on it"
    (flat,) = desk.of("tape_flat")
    (name, pair, left), *_ = flat["shadow"]
    assert (name, pair) == ("control", "EUR.USD") and 19_000 < left <= 20_000, "what it still holds, to the unit"
    assert desk.of("tape_overdue") == []
    assert TAPE_OVERDUE not in desk.pages.fired
    desk.stop()
    assert identical(desk, tmp_path / "scratch", ["stopped"])


def test_a_blown_out_spread_at_the_hard_limit_pages_and_the_trader_stays_up_and_closes_when_it_returns(tmp_path):
    desk = a_desk(tmp_path / "live", changes=COLLARS)
    until(desk, SOFT)
    wide = blown_out(desk)
    until(desk, HARD, feed=wide)
    assert desk.held() == (20_000, 20_000, 20_000)
    wide(LIMIT + 70)  # past the time limit, and past the reconciler's next look
    assert not desk.trader.flat_at_hard_limit and desk.held()[2] == 20_000, "nothing was forced through the bad book"
    (late,) = desk.of("tape_overdue")
    assert (late["open"], late["in_flight"], late["blind"]) == ([["control", "EUR.USD", 20_000]], [], 0)
    (reached,) = [e for e in desk.of("tape") if e["room"] == "hard"]
    assert LIMIT * SECOND <= late["at_ns"] - reached["at_ns"] < (LIMIT + 2) * SECOND
    assert desk.pages.fired == [TAPE_SOFT, TAPE_HARD, TAPE_OVERDUE], "paged once, and only once it was late"
    blocked = [e for e in after(desk, "tape", room="hard") if e["type"] == "verdict"]
    assert len(blocked) > 10 and {(e["code"], e["sent"]) for e in blocked} == {("PRICE_COLLAR", 0)}
    waits = [later["at_ns"] - earlier["at_ns"] for earlier, later in itertools.pairwise(blocked)]
    assert min(waits) >= 5 * SECOND, "refused closes come again at the gate's own interval, and no faster"
    turns = desk.trader.summary()["turns"]
    wide(3)
    assert desk.trader.summary()["turns"] == turns + 3, "and the session goes on taking its turns"

    desk.run(8)  # the spread comes back
    assert desk.trader.flat_at_hard_limit and desk.held() == (0, 0, 0) and desk.positions() == {}
    assert desk.of("tape_flat")[0]["overdue"] == 1 and TAPE_OVERDUE in desk.pages.resolved
    desk.stop()
    rows = read_tape(desk.day() / HEAD)
    late = rows[next(n for n, row in enumerate(rows) if row[0] == "step" and row[5:] == [HARD]) :]
    # An answer that repeats the last one is counted in a `same` line and not written again.
    looks = sum(1 if row[0] == "ask" else row[2] for row in late if row[0] in ("ask", "same") and row[1] == "positions")
    assert looks >= 1, "the reconciler went on looking at the broker's positions"
    assert tape_bytes(tmp_path / "live" / "days") <= NEAR["quota_bytes"]
    assert identical(desk, tmp_path / "scratch", ["stopped"])


# ---- the reserve used up ---------------------------------------------------------------------------------------------


def test_when_the_reserve_itself_is_used_up_the_tape_stops_and_the_session_does_not(tmp_path):
    desk = a_desk(tmp_path / "live", edge=TIGHT, changes=COLLARS)
    days, quota = tmp_path / "live" / "days", TIGHT["quota_bytes"]
    until(desk, SOFT)
    wide = blown_out(desk)
    until(desk, HARD, feed=wide)
    for _ in range(40):
        wide(1)
        assert tape_bytes(days) <= quota, "no line is written past the quota"
        if desk.edge.dark:
            break
    assert desk.edge.dark and desk.held()[2] == 20_000
    assert desk.pages.fired[-1] == TAPE_DARK and TAPE_OVERDUE in desk.pages.fired
    stood = tape_bytes(days)
    last = parts(desk.day() / HEAD)[-1]
    assert last.read_bytes().endswith(b"\n" + DARK) and quota - 100 < stood <= quota
    written = len(desk.decisions())
    wide(10)
    assert tape_bytes(days) == stood and len(desk.decisions()) > written, "unrecorded, and still deciding"
    assert desk.of("verdict")[-1]["code"] == "PRICE_COLLAR", "the closes are still proposed, and still refused"

    desk.run(8)  # the spread comes back: the position is closed, though nothing records it
    assert desk.trader.flat_at_hard_limit and desk.held() == (0, 0, 0) and desk.positions() == {}
    desk.stop()
    assert tape_bytes(days) == stood and read_tape(desk.day() / HEAD)[-1] == ["dark"]
    found = desk.replay(tmp_path / "scratch")
    assert [(one.as_dict()["ended"], one.identical) for one in found] == [("dark", True)]
    assert 0 < found[0].replayed_bytes < found[0].live_bytes, "replayed as far as the tape goes, and the same so far"
    desk.start()  # it ended on purpose and saved its sleeves' states, tied to the tape as it stands
    assert desk.of("sleeve_state")[-1] | {"restored": ["control"], "cold": {}} == desk.of("sleeve_state")[-1]


def test_a_start_with_no_room_at_all_is_not_refused_it_writes_its_head_and_runs_unrecorded(tmp_path):
    desk = a_desk(tmp_path / "live", edge=TIGHT, changes=COLLARS)
    days, quota = tmp_path / "live" / "days", TIGHT["quota_bytes"]
    until(desk, SOFT)
    wide = blown_out(desk)
    until(desk, HARD, feed=wide)
    for _ in range(40):
        if desk.edge.dark:
            break
        wide(1)
    assert desk.edge.dark and desk.held()[2] == 20_000
    stood = tape_bytes(days)
    desk.restart()  # the process is killed with a position open and no room left at all
    second = desk.day() / "session-002.tape.jsonl"
    head = second.stat().st_size
    assert [row[0] for row in lines(second)] == ["open", "marks"] and head < 2_000
    wide(2)
    assert desk.edge.dark and read_tape(second)[-1] == ["dark"]
    assert tape_bytes(days) == stood + head + len(DARK), "its head and its last word: the one write past the quota"
    assert tape_bytes(days) < quota + 2_000
    desk.run(12)  # the spread comes back
    assert desk.trader.flat_at_hard_limit and desk.held() == (0, 0, 0) and desk.positions() == {}
    assert not desk.trader.manager.blind, "it reconciled before it closed anything"
    desk.stop()
    found = desk.replay(tmp_path / "scratch")
    assert [(one.as_dict()["ended"], one.identical) for one in found] == [("dark", True), ("dark", True)]


def test_a_session_that_went_unrecorded_ends_once_room_is_made_and_the_next_one_is_recorded_again(tmp_path):
    desk = a_desk(tmp_path / "live", edge=TIGHT, changes=COLLARS)
    days = tmp_path / "live" / "days"
    until(desk, SOFT)
    wide = blown_out(desk)
    until(desk, HARD, feed=wide)
    for _ in range(40):
        if desk.edge.dark:
            break
        wide(1)
    assert desk.edge.dark and desk.held()[2] == 20_000
    turns = desk.trader.summary()["turns"]
    assert housekeep(days) > 0, "the part that ends with `dark` is closed, and is compressed with the others"
    assert tape_bytes(days) < TIGHT["soft_bytes"]
    wide(3)
    assert desk.trader.summary()["turns"] == turns, "it took no further turn: the edge ended the session"
    desk.stop()
    desk.session()  # what the command's loop does next, on the same connection
    wide(3)
    assert not desk.edge.dark and desk.held()[2] == 20_000, "the position is still there, and is watched and recorded"
    second = read_tape(desk.day() / "session-002.tape.jsonl")
    assert [row[0] for row in second[:3]] == ["open", "marks", "step"] and second[2][5] in (ROOM, SOFT)
    assert TAPE_DARK in desk.pages.resolved
    desk.stop()
    found = desk.replay(tmp_path / "scratch")
    assert [(one.as_dict()["ended"], one.identical) for one in found] == [("dark", True), ("stopped", True)]


# ---- the next start, and the way back --------------------------------------------------------------------------------


def test_a_start_at_the_soft_limit_begins_on_exits_only_and_trades_again_once_housekeeping_made_room(tmp_path):
    desk = a_desk(tmp_path / "live", hold=3, edge=LATER)
    days = tmp_path / "live" / "days"
    until(desk, SOFT)
    desk.run(10)
    desk.stop()
    assert tape_bytes(days) > LATER["soft_bytes"]
    desk.start()  # a new process: it is not refused, and it is told the room on its first turn
    assert desk.edge.level == SOFT
    desk.run(5)
    second = after(desk, "start", session=2)
    assert [(e["room"], e["before"], e["n"]) for e in second if e["type"] == "tape"] == [("soft", "room", 1)]
    assert {str(e["code"]) for e in second if e["type"] == "refused"} <= {"TAPE_SOFT_LIMIT", "VENUE_NOT_READY"}
    assert all(e["code"] != "ALLOW" for e in second if e["type"] == "verdict"), "nothing was opened"
    assert desk.pages.fired.count(TAPE_SOFT) == 2, "said again by the session that started under it"

    assert housekeep(days) > 0  # the hourly job: the closed parts are compressed
    assert tape_bytes(days) < LATER["soft_bytes"] - 4_000
    desk.run(3)
    told = [(e["room"], e["before"]) for e in after(desk, "start", session=2) if e["type"] == "tape"]
    assert told[:2] == [("soft", "room"), ("room", "soft")], "back to normal by itself, with a line that says so"
    assert TAPE_SOFT in desk.pages.resolved
    back = after(desk, "tape", room="room")
    assert any(e["type"] == "verdict" and e["code"] == "ALLOW" and e["sent"] for e in back), "it opens positions again"
    desk.stop()
    assert identical(desk, tmp_path / "scratch", ["stopped", "stopped"])


def test_flat_at_the_hard_limit_the_process_waits_with_no_session_and_starts_one_when_there_is_room(tmp_path, capsys):
    desk = a_desk(tmp_path / "live", session=False)  # the command's own loop begins the sessions
    days = tmp_path / "live" / "days"
    desk.quotes(400)
    tidied: list[int] = []
    waited_from: list[int] = []

    def should_stop() -> bool:
        now = desk.clock.now_ns()
        first = last_head(days)
        if first is not None and not waited_from and first.name == HEAD and read_tape(first)[-1][0] == "stop":
            assert desk.edge.level == HARD and desk.positions() == {}, "it stopped flat, at the hard limit"
            waited_from.append(now)
        if waited_from and not tidied and now >= waited_from[0] + 30 * SECOND:
            tidied.append(housekeep(days))  # half a minute later the hourly job passes
        return now >= desk.until

    desk.until = desk.clock.now_ns() + 300 * SECOND
    desk.edge._should_stop = should_stop
    sessions = cli.serve(
        desk.edge, desk.settings, model=desk.costs, calendar=cli.Calendar(), risk=desk.risk, notify=desk.pages,
        config_digest=DIGEST, should_stop=should_stop,
    )  # fmt: skip
    said = capsys.readouterr().err
    assert sessions == 2 and tidied and tidied[0] > 0
    assert "the tapes are at their hard limit and the broker book is flat: waiting for room, with no session" in said
    assert "the tapes have room again" in said
    first, second = (read_tape(head) for head in heads(next(days.iterdir())))
    assert first[-1][0] == "stop" and second[0][1] >= waited_from[0] + 30 * SECOND, "nothing began while it waited"
    started = [e for e in desk.decisions() if e["type"] == "start"]
    assert [e["session"] for e in started] == [1, 2]
    assert desk.pages.fired[:2] == [TAPE_SOFT, TAPE_HARD] and {TAPE_SOFT, TAPE_HARD} <= set(desk.pages.resolved), (
        "the session that began with room took back the pages of the one that ended without"
    )
    assert any(e["type"] == "verdict" and e["code"] == "ALLOW" and e["sent"] for e in after(desk, "start", session=2))
    found = desk.replay(tmp_path / "scratch")
    assert [one.identical for one in found] == [True, True]


def test_the_command_says_that_a_session_started_on_exits_only(tmp_path, capsys):
    desk = a_desk(tmp_path / "live", hold=3, edge=FAR)
    until(desk, SOFT)
    desk.stop()
    desk.until = desk.clock.now_ns() + 3 * SECOND
    desk.quotes(3)
    stop = desk.edge._should_stop
    cli.serve(
        desk.edge, desk.settings, model=desk.costs, calendar=cli.Calendar(), risk=desk.risk, notify=desk.pages,
        config_digest=DIGEST, should_stop=stop,
    )  # fmt: skip
    said = capsys.readouterr().err
    assert "EXITS ONLY: the tapes hold " in said
    assert "at their soft limit (soft 8000, hard 372000, quota 400000)" in said
    assert ROOM == 0 and identical(desk, tmp_path / "scratch", ["stopped", "stopped"])
