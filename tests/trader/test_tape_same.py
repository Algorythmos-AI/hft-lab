"""An answer the venue repeats is counted on the tape, not written again, and the asking backs off (format 2).

What flooded a tape for eight hours (HFT-EXP-0009): a connection that could not be made safe was asked on every
turn, four times a second, and every identical answer was written out. Here: the asking waits longer each time,
the tape stays small, and a replay is still held to exactly what the session asked. Fake broker, made-up balances.
"""
from __future__ import annotations

import json

import pytest

from hftlab.core import Fill, Instrument, InstrumentKind, Liquidity, Side
from hftlab.trader import TapeError
from hftlab.trader.session import SAFE_LONGEST_NS
from hftlab.trader.tape import FORMAT, ReplayEdge, _Recorded, parts, read_tape
from hftlab.venues.port import ConnectionState, VenueCash, VenueUnavailable
from tests.inventory.kit import Bureau, account
from tests.trader.kit import Desk
from tests.venues.kit import SECOND, T0

HEAD = "session-001.tape.jsonl"
EURUSD_CFD = Instrument.from_pair("EUR.USD", price_scale=5, min_order_size=1_000, kind=InstrumentKind.CFD)
OPEN = ["open", T0, "fake", [], "READY"]


def rows_of(desk: Desk) -> list[list]:
    return read_tape(desk.day() / HEAD)


def tape_size(desk: Desk) -> int:
    return sum(path.stat().st_size for path in parts(desk.day() / HEAD))


def never_safe(folder) -> Bureau:
    """A cash account with a float in its limits, and a position in another form that no book explains: the
    connection cannot be made safe, exactly as before, for as long as it is there."""
    desk = Bureau(folder, start=False)
    desk.market.carry(EURUSD_CFD, 5)
    desk.start()
    return desk


def test_a_connection_that_cannot_be_made_safe_is_asked_less_and_less_often_and_the_tape_stays_small(tmp_path):
    desk = never_safe(tmp_path / "day")
    hour = 3_600
    desk.run(hour, quotes=False)
    assert desk.trader.manager.blind and desk.broker.state is ConnectionState.RECONCILING
    assert [e["broken"] for e in desk.of("break")] == [1], "a position in another form is compared as it ever was"
    rows = rows_of(desk)
    asked = [row for row in rows if row[0] == "ask"]
    assert {row[1] for row in asked} == {"open_orders", "executions_since", "positions"} and len(asked) == 3
    assert not [row for row in rows if row[0] == "same"], "nothing is written while the answer stays the same"
    turns = [row for row in rows if row[0] == "step"]
    assert len(turns) < hour + 20, "and no turn is taken between beats once the wait is longer than a beat"
    assert tape_size(desk) < 80 * len(turns), "an hour of it is an hour of beats, and nothing else"

    desk.stop()
    counted = {row[1]: row[2] for row in read_tape(desk.day() / HEAD) if row[0] == "same"}
    longest = SAFE_LONGEST_NS // SECOND
    assert set(counted) == {"open_orders", "executions_since", "positions"}
    assert all(hour // longest <= times <= hour // longest + 10 for times in counted.values()), counted
    replayed = desk.replay(tmp_path / "scratch")
    assert [one.identical for one in replayed] == [True], "and the session replays byte for byte from that tape"


def test_a_new_connection_is_asked_at_once_whatever_was_waited_before(tmp_path):
    down = T0 + 600 * SECOND
    desk = Bureau(tmp_path, account(outages=((down, down + 5 * SECOND),)), start=False)
    desk.market.carry(EURUSD_CFD, 5)
    desk.start()
    desk.run(590, quotes=False)
    assert desk.trader._safe_wait == SAFE_LONGEST_NS
    desk.market._carried.clear()  # whoever held it closed it while the link was down
    desk.run(20, quotes=False)
    back = next(e["at_ns"] for e in desk.of("connection") if e["state"] == "RECONCILING" and e["at_ns"] > down)
    safe = next(e["at_ns"] for e in desk.of("connection") if e["state"] == "READY" and e["at_ns"] > down)
    assert safe - back < SECOND and not desk.trader.manager.blind


def test_hours_of_reconciling_a_float_that_stands_write_each_answer_once(tmp_path):
    desk = Bureau(tmp_path / "day")
    desk.fund()
    before = len([row for row in rows_of(desk) if row[0] == "ask"])
    size = tape_size(desk)
    desk.run(1_800, quotes=False)
    rows = rows_of(desk)
    assert len([row for row in rows if row[0] == "ask"]) <= before + 4, "thirty comparisons, all answered the same"
    assert tape_size(desk) - size < 80 * 1_820
    assert not desk.of("break") and desk.trader.float_status()["cash"] == desk.market.cash()
    asked_at = desk.trader.float_status()["cash_at_ns"]
    assert asked_at > desk.clock.now_ns() - 61 * SECOND, "the cash was asked for throughout"
    desk.stop()
    counted = {row[1]: row[2] for row in rows_of(desk) if row[0] == "same"}
    assert counted["cash"] >= 29 and counted["executions_since"] >= 29 and counted["positions"] >= 29
    assert [one.identical for one in desk.replay(tmp_path / "scratch")] == [True]


def test_a_replay_that_asks_more_or_less_often_than_the_tape_counts_is_refused(tmp_path):
    desk = Bureau(tmp_path / "day")
    desk.fund()
    desk.run(300, quotes=False)
    desk.stop()
    (part,) = parts(desk.day() / HEAD)
    lines = part.read_bytes().split(b"\n")
    at = max(n for n, line in enumerate(lines) if line.startswith(b'["same","cash",'))  # written as it stopped
    times = json.loads(lines[at])[2]
    lines[at] = json.dumps(["same", "cash", times + 1], separators=(",", ":")).encode()
    part.write_bytes(b"\n".join(lines))
    with pytest.raises(TapeError, match="answered the same"):
        desk.replay(tmp_path / "scratch")


def test_the_head_says_the_format_and_a_tape_without_it_needs_a_line_for_every_question():
    step = ["step", T0 + 1, "READY", [], None]
    cash = ["ask", "cash", "READY", [["USD", 5]]]
    counted = [OPEN, ["marks", {"format": FORMAT}], cash, step, ["same", "cash", 2], ["stop", T0 + 2]]
    new = ReplayEdge(counted, paper=True)
    assert new.asks.cash() == (VenueCash("USD", 5),)
    assert new.next(urgent=False) is not None
    assert new.asks.cash() == new.asks.cash() == (VenueCash("USD", 5),), "given again, and counted"
    assert new.next(urgent=False) is None, "the count agrees: the session's last word is reached"
    short = ReplayEdge(counted, paper=True)
    short.asks.cash(), short.next(urgent=False), short.asks.cash()
    with pytest.raises(TapeError, match=r"2 time\(s\) and the replay asked 1 time"):
        short.next(urgent=False)
    old = ReplayEdge([OPEN, ["marks", {}], cash, step, ["stop", T0 + 2]], paper=True)
    assert old.asks.cash() == (VenueCash("USD", 5),) and old.next(urgent=False) is not None
    with pytest.raises(TapeError, match="wants 'ask cash'"):
        old.asks.cash()
    with pytest.raises(TapeError, match="wants 'ask cash'"):
        ReplayEdge([OPEN, ["marks", {"format": FORMAT}], step], paper=True).asks.cash()


def test_a_count_in_the_middle_of_a_tape_is_followed_by_the_answer_that_differed():
    first, second = ["ask", "cash", "READY", [["USD", 5]]], ["ask", "cash", "READY", [["USD", 6]]]
    rows = [OPEN, ["marks", {"format": FORMAT}], first, ["same", "cash", 1], second, ["stop", T0 + 2]]
    edge = ReplayEdge(rows, paper=True)
    assert [edge.asks.cash()[0].amount for _ in range(3)] == [5, 5, 6]
    hasty = ReplayEdge(rows, paper=True)
    hasty.asks.cash()
    with pytest.raises(TapeError, match=r"1 time\(s\) and the replay asked 0 time"):
        hasty.next(urgent=False)


class _Told:
    """Stands where the edge is: keeps the lines it is given."""

    def __init__(self) -> None:
        self.rows: list[list] = []
        self.state, self.dark = ConnectionState.READY, False

    def write(self, row: list) -> None:
        self.rows.append(row)


class _Listing:
    """A venue whose list of executions is the same each time but for when it was asked."""

    def __init__(self) -> None:
        self.asked, self.down, self.fills = 0, False, 1

    def executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        self.asked += 1
        if self.down:
            raise VenueUnavailable("no answer")
        heard = T0 + self.asked
        return tuple(
            Fill(f"x{n}", "hft-control-0", "EUR.USD", Side.BUY, 1_000, 108_500, heard, Liquidity.TAKER, 2, "USD", T0, n)
            for n in range(self.fills)
        )


def test_the_trader_is_shown_the_listing_that_is_on_the_tape_and_a_changed_one_is_written():
    told, venue = _Told(), _Listing()
    recorded = _Recorded(venue, told)
    first = recorded.executions_since(0)
    assert recorded.executions_since(0) is first and recorded.executions_since(0) is first
    assert venue.asked == 3 and [row[0] for row in told.rows] == ["ask"], "asked each time; written once"
    assert first[0].ts_ns == T0 + 1, "when it was first listed: what the tape holds, and what a replay gives back"
    venue.fills = 2
    assert len(recorded.executions_since(0)) == 2
    question = "executions_since"
    assert [row[:3] for row in told.rows[1:]] == [["same", question, 2], ["ask", question, "READY"]]
    venue.down = True
    for _ in range(3):
        with pytest.raises(VenueUnavailable):
            recorded.executions_since(0)
    assert told.rows[3:] == [["ask", "executions_since", "READY", None, "VenueUnavailable"]], "a failure repeats too"
    recorded.flush()
    assert told.rows[4:] == [["same", "executions_since", 2]]
    recorded.begin()
    venue.down = False
    recorded.executions_since(0)
    assert told.rows[-1][0] == "ask", "a new session's tape starts with nothing said"
