"""The reserve under the tape's quota: sized from the risk limits, and refused when the quota cannot hold it.

`hftlab.trader.room` says how the reserve is worked out. Here: the arithmetic on limits a person can check by
hand; the refusals; for generated limits, every book they allow, closed in the worst way they allow, written
out with the tape's own encoders and found to fit; and one closing on the fake broker under limits that came
from the settings, measured.
"""
from __future__ import annotations

import dataclasses
from random import Random

import pytest
import yaml
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hftlab.core import Fill, Liquidity, OrderEvent, OrderState, OrderType, Quote, Side, TimeInForce
from hftlab.risk import HARD
from hftlab.trader import TraderRefused, room, tape
from hftlab.trader import __main__ as cli
from hftlab.trader.config import TraderConfigError, parse
from hftlab.trader.tape import read_tape, tape_bytes
from hftlab.venues.port import CommissionEvent, ConnectionState, OpenOrder, VenuePosition
from tests.risk.kit import limits, raw
from tests.support import ROOT
from tests.trader.kit import SECOND, Desk, put_stage, raw_settings

MINUTE = 60 * SECOND
CONFIG = ROOT / "config"
SMALL = {"part_bytes": 3_000, "quota_bytes": 50_000_000, "soft_percent": 10, "flatten_seconds": 10}
PAIRS = ("EUR.USD", "USD.JPY")
SLEEVES = ("control", "ml")


def the_settings(**tape_block):
    return parse(raw_settings(tape={**SMALL, **tape_block}))


# ---- the arithmetic --------------------------------------------------------------------------------------------------


def test_the_orders_a_span_can_hold_and_the_closes_the_largest_book_takes_are_the_limits_own_numbers():
    risk = limits()  # thirty orders a minute for the desk; 100,000 a pair, 60,000 a sleeve, 60,000 an order
    assert [room.orders(risk, span) for span in (10 * SECOND, MINUTE, 5 * MINUTE)] == [30, 60, 180]
    assert room.lines(risk) == [[1, 1], [1, 1]], "each sleeve's cap in each pair is one order"
    assert room.closes(risk) == 4, "two sleeves at their caps in the two pairs that may be open at once"
    deep = limits({"per_pair/EUR.USD/max_order_size": 7_000, "desk/max_open_pairs": 1})
    assert room.lines(deep) == [[9, 9], [1, 1]] and room.closes(deep) == 18, "60,000 in orders of 7,000, twice"
    capped = limits({"per_sleeve/ml/max_position": 5_000, "per_pair/USD.JPY/max_position": 30_000})
    assert room.lines(capped) == [[1, 1], [1, 1]] and room.closes(capped) == 4


def test_the_reserve_is_the_time_limits_turns_and_orders_and_grows_with_each_of_them():
    risk, plain = limits(), the_settings()
    reserve = room.reserve_bytes(plain, risk)
    assert room.turns(plain) == 12, "a beat a second for ten seconds, and one at each end"
    found = room.limits(plain, risk)
    assert (found.soft, found.hard) == (5_000_000, 50_000_000 - reserve)
    assert (found.quota, found.reserve) == (50_000_000, reserve)
    assert found.as_dict() == {"soft_bytes": 5_000_000, "hard_bytes": found.hard, "quota_bytes": 50_000_000}
    assert room.reserve_bytes(the_settings(flatten_seconds=20), risk) > reserve, "a longer time limit"
    assert room.reserve_bytes(plain, limits({"desk/max_orders_per_minute": 31})) > reserve, "more orders a minute"
    assert room.reserve_bytes(dataclasses.replace(plain, beat_ns=SECOND // 2), risk) > reserve, "more turns"
    assert room.reserve_bytes(dataclasses.replace(plain, pairs=plain.pairs[:1]), risk) < reserve, "fewer pairs"
    nothing_sent = room.reserve_bytes(plain, None)
    assert 0 < nothing_sent < reserve // 100, "a run that can send nothing keeps room for its turns and no order"
    assert room.limits(parse(raw_settings()), risk) is None and room.edge_options(parse(raw_settings()), risk) == {
        "part_bytes": 64_000_000
    }, "no quota, no limits"
    assert room.edge_options(plain, risk) == {
        "part_bytes": 3_000, "quota_bytes": 50_000_000, "soft_bytes": 5_000_000, "reserve_bytes": reserve,
    }  # fmt: skip


# ---- what is refused -------------------------------------------------------------------------------------------------


def test_a_quota_that_cannot_hold_the_reserve_above_the_soft_limit_is_refused_and_told_what_it_needs():
    risk = limits()
    reserve = room.reserve_bytes(the_settings(), risk)
    between = 30 * room._SENT  # the sends of the orders the desk's limit lets through at once: thirty
    least = -(-(reserve + between) * 100 // 90)
    with pytest.raises(TraderConfigError, match="cannot hold the reserve") as refused:
        room.limits(the_settings(quota_bytes=least - 1_000), risk)
    assert f"takes up to {reserve} bytes" in str(refused.value) and f"must be at least {least}" in str(refused.value)
    enough = room.limits(the_settings(quota_bytes=least), risk)
    assert enough.hard >= enough.soft
    with pytest.raises(TraderConfigError, match="cannot hold the reserve"):
        room.limits(the_settings(quota_bytes=reserve), risk)
    with pytest.raises(TraderConfigError, match="cannot hold the reserve"):
        room.limits(the_settings(soft_percent=99), risk)
    assert room.limits(the_settings(soft_percent=99), None) is not None, "the same quota holds a run that sends nothing"


def test_a_time_limit_too_short_to_close_what_the_limits_allow_is_refused():
    slow = limits({"desk/max_orders_per_minute": 3})
    with pytest.raises(TraderConfigError, match="too short for the limits: the largest book they allow takes 4 clos"):
        room.limits(the_settings(), slow)
    assert room.limits(the_settings(flatten_seconds=60), slow) is not None, "two minutes begun: six orders"
    deep = limits({"per_pair/EUR.USD/max_order_size": 5_000, "desk/max_orders_per_minute": 60})
    with pytest.raises(TraderConfigError, match=r"12 of them one after another.* and 10 beat"):
        room.limits(the_settings(quota_bytes=10**9), deep)
    assert room.limits(the_settings(quota_bytes=10**9, flatten_seconds=12), deep) is not None


def test_the_trader_refuses_to_begin_a_session_under_such_a_quota_and_writes_nothing(tmp_path):
    put_stage(tmp_path, "control", "min_size")
    desk = Desk(tmp_path, changes={"desk/max_orders_per_minute": 30})
    desk.run(2)
    desk.stop()
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    desk.settings = dataclasses.replace(desk.settings, tape_quota_bytes=100_000)
    with pytest.raises(TraderRefused, match=r"tape\.quota_bytes of 100000 cannot hold the reserve"):
        desk.session()
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    with pytest.raises(TraderConfigError, match="cannot hold the reserve"):
        desk.connect()


def test_the_command_refuses_such_a_quota_before_it_connects_to_anything(tmp_path, capsys):
    config, risk = tmp_path / "trader.yaml", tmp_path / "risk.yaml"
    loaded = raw_settings(state_dir=str(tmp_path / "trader"), tape={"part_bytes": 3_000, "quota_bytes": 100_000})
    config.write_text(yaml.safe_dump(loaded))
    risk.write_text(yaml.safe_dump(raw()))
    files = ["--config", str(config), "--risk", str(risk), "--cost-model", str(CONFIG / "cost_model.yaml"),
             "--calendar", str(CONFIG / "calendar"), "--state-dir", str(tmp_path)]  # fmt: skip
    assert cli.main([*files, "--dry-run-seconds", "5"]) == cli.EXIT_REFUSED
    said = capsys.readouterr().err
    assert "trader: refused: tape.quota_bytes of 100000 cannot hold the reserve" in said and "PAPER" not in said
    assert not (tmp_path / "trader").exists(), "refused before a lock, a journal or a connection"


# ---- every book the limits allow, closed in the worst way they allow, fits -------------------------------------------

def a_fill(random, order_id: str, pair: str) -> Fill:
    """A fill as a venue might report one: any size, any price, any time, an id of its own of any length it may have."""

    def number() -> int:
        return random.choice([1, 10**5, 10**12, room.BIG, random.randint(1, room.BIG)])

    return Fill(
        "x" * random.randint(1, room.TEXT), order_id, pair, random.choice(list(Side)), number(), number(), number(),
        Liquidity.TAKER, number(), "USD", number(), number(),
    )  # fmt: skip


@st.composite
def a_case(draw):
    """Limits, the settings of the tape, a book inside the limits, and how badly its closing goes."""
    pair_cap = draw(st.integers(min_value=1, max_value=200)) * 10_000
    order_size = draw(st.integers(min_value=1, max_value=200)) * 5_000
    changes: dict[str, object] = {
        "desk/max_orders_per_minute": draw(st.integers(min_value=1, max_value=40)),
        "desk/max_open_pairs": draw(st.integers(min_value=1, max_value=2)),
    }
    for pair in PAIRS:
        changes[f"per_pair/{pair}/max_position"] = pair_cap
        changes[f"per_pair/{pair}/max_order_size"] = order_size
    for sleeve in SLEEVES:
        changes[f"per_sleeve/{sleeve}/max_position"] = draw(st.integers(min_value=1, max_value=200)) * 10_000
    risk = limits(changes)
    seconds = draw(st.integers(min_value=1, max_value=90))
    made = dataclasses.replace(
        parse(raw_settings(tape={"part_bytes": 1, "quota_bytes": 10**15, "flatten_seconds": seconds})),
        beat_ns=draw(st.sampled_from([250, 500, 1_000, 2_000])) * 1_000_000,
    )  # fmt: skip
    try:
        room.limits(made, risk)
    except TraderConfigError:
        assume(False)  # refused at the start: the time limit cannot hold the closing of these limits
    book: dict[tuple[str, str], int] = {}
    for pair in PAIRS[: risk.desk.max_open_pairs]:
        held = 0
        for sleeve in SLEEVES:
            cap = min(risk.pairs[pair].max_position, risk.sleeves[sleeve].max_position)
            quantity = draw(st.integers(min_value=-cap, max_value=cap))
            if abs(held + quantity) > risk.pairs[pair].max_position:
                quantity = -quantity  # sleeves may hold against each other; the desk's line may not pass the pair's cap
            held += quantity
            if quantity:
                book[sleeve, pair] = quantity
    return risk, made, book, draw(st.integers(min_value=0, max_value=10**6)), Random(draw(st.integers(0, 2**32)))


def events_of_an_order(a_filled, order_id: str, pair: str) -> list:
    """All a venue says about one order, at the most the arithmetic allows for: its reports, fills and commissions."""
    big, text = room.BIG, room.TEXT
    said: list = [
        OrderEvent(order_id, OrderState.PARTIALLY_FILLED, big, big, big, "b" * text, "r" * text)
        for _ in range(room.REPORTS_PER_ORDER)
    ]
    for _ in range(room.FILLS_PER_ORDER):
        fill = a_filled(order_id, pair)
        said += [fill, CommissionEvent(fill.fill_id, room.BIG, "USD", room.BIG, room.BIG)]
    return said


@settings(max_examples=150)
@given(a_case())
def test_the_closing_of_any_book_the_limits_allow_with_every_retry_they_allow_fits_the_reserve(case):
    risk, made, book, retries, random = case
    limit = made.tape_flatten_ns
    most, listed = room.orders(risk, limit), room.orders(risk, limit + room.LISTED_NS)
    needed = [
        (sleeve, pair) for (sleeve, pair), quantity in sorted(book.items())
        for _ in range(-(-abs(quantity) // risk.pairs[pair].max_order_size))
    ]  # fmt: skip
    assert len(needed) <= room.closes(risk) <= most, "the largest book's closes are what the reserve is sized for"
    # Every order the gate lets through in the time limit: the closes first, and retries of them for the rest.
    again = min(retries, most - len(needed))
    sent = [*needed, *(random.choice(needed or [("control", "EUR.USD")]) for _ in range(again))]

    def a_filled(order_id: str, pair: str) -> Fill:
        return a_fill(random, order_id, pair)

    def order_id(number: int, sleeve: str) -> str:
        return f"hft-{sleeve}-{number:020x}"

    written: list[list] = []
    arrived: list = []
    for number, (sleeve, pair) in enumerate(sent):
        one = order_id(number, sleeve)
        written += [["sent", "place", one, "uncertain", room._STATE], ["sent", "cancel", one, "failed", room._STATE]]
        arrived += events_of_an_order(a_filled, one, pair)
    fills = [event for event in arrived if isinstance(event, Fill)]
    earlier = [order_id(10**6 + number, "ml") for number in range(listed - len(sent))]  # orders from the minutes before
    for old in earlier:
        fills += [a_filled(old, "EUR.USD") for _ in range(room.FILLS_PER_ORDER)]
    working = [
        OpenOrder(
            one, "b" * room.TEXT, "EUR.USD", Side.SELL, room.BIG, room.BIG, OrderType.MARKET, room.BIG, TimeInForce.IOC,
            OrderState.PARTIALLY_FILLED,
        )
        for one in [*(order_id(number, sleeve) for number, (sleeve, _) in enumerate(sent)), *earlier]
    ]  # fmt: skip
    switches = sorted([risk.kill_switch, risk.flatten_switch])
    # A turn of the time limit: a quote for every pair, a beat, and the venue's whole answer to all three questions.
    quotes = [tape.encode(Quote(pair, *(random.randint(1, room.BIG) for _ in range(6)))) for pair, _ in made.pairs]
    positions = [VenuePosition(pair, kind, -room.BIG) for pair, kind in made.pairs]
    turn = [
        ["step", room.BIG, room._STATE, quotes, [switches, room.BIG, []], HARD],
        ["ask", "open_orders", room._STATE, [tape._ANSWERS["open_orders"][0](one) for one in working]],
        ["ask", "executions_since", room._STATE, [tape._ANSWERS["executions_since"][0](fill) for fill in fills]],
        ["ask", "positions", room._STATE, [tape._ANSWERS["positions"][0](one) for one in positions]],
    ]
    written.append(["stop", room.BIG])
    said = [tape.encode(event) for event in arrived]  # each event is on one turn's line, once: the bytes of its row
    total = (
        sum(len(tape._line(row)) for row in written)
        + room.turns(made) * sum(len(tape._line(row)) for row in turn)
        + sum(len(tape._line(row)) for row in said)
        + len(tape.DARK)
    )
    heads = room.STARTS * (room._HEAD + len(made.pairs) * room._INSTRUMENT)
    reserve = room.reserve_bytes(made, risk)
    assert total + heads <= reserve, (total, heads, reserve)
    assert (total + heads) * 2 > reserve, "and the reserve is this worst case, not a multiple of it"


def test_the_longest_state_and_the_longest_order_id_are_the_ones_the_arithmetic_uses():
    assert room._STATE == max((state.value for state in ConnectionState), key=len) == "DISCONNECTED"
    assert len(room._ORDER_ID) == len("hft-") + 24 + 1 + 20, "the longest sleeve name and the digest (hftlab.core.ids)"
    assert max(OrderState, key=lambda state: len(state.value)) is room._STATED


# ---- measured, on the fake broker, under limits that came from the settings ------------------------------------------

LIMITS = {
    "desk/max_orders_per_minute": 30, "per_pair/EUR.USD/max_position": 20_000, "per_pair/USD.JPY/max_position": 20_000,
    "per_pair/EUR.USD/max_order_size": 20_000, "per_pair/USD.JPY/max_order_size": 20_000,
    "per_sleeve/control/max_position": 20_000, "per_sleeve/ml/max_position": 20_000, "desk/max_open_pairs": 1,
}  # fmt: skip


def test_a_real_closing_of_the_largest_book_one_sleeve_may_hold_takes_a_small_part_of_the_reserve(tmp_path):
    put_stage(tmp_path / "live", "control", "min_size")
    loaded = raw_settings(tape={"part_bytes": 30_000, "quota_bytes": 10**9, "soft_percent": 1, "flatten_seconds": 10})
    loaded["sleeves"][1]["hold_seconds"] = 3_600
    desk = Desk(tmp_path / "live", settings=loaded, changes=LIMITS, start=False)
    reserve = room.reserve_bytes(desk.settings, desk.risk)
    least = -(-(reserve + 30 * room._SENT) * 100 // 99)  # what holds the reserve above a soft limit of 1%
    quota = least + 400
    desk.settings = dataclasses.replace(desk.settings, tape_quota_bytes=quota)
    found = room.limits(desk.settings, desk.risk)
    assert found.reserve == reserve and 30 * room._SENT <= found.hard - found.soft < 5_000
    with pytest.raises(TraderConfigError, match="cannot hold the reserve"):
        room.limits(dataclasses.replace(desk.settings, tape_quota_bytes=least - 200), desk.risk)
    desk.start()
    start = desk.of("start")[0]
    assert start["tape"] == {"soft_bytes": found.soft, "hard_bytes": found.hard, "quota_bytes": quota}
    head = desk.day() / "session-001.tape.jsonl"
    assert head.stat().st_size <= room._HEAD + 2 * room._INSTRUMENT, "a real head is inside what the reserve allows for"
    for _ in range(300):
        if desk.trader.flat_at_hard_limit:
            break
        desk.run(1)
    assert desk.trader.flat_at_hard_limit and desk.positions() == {}
    closes = [e for e in desk.of("decision") if e["source"] == "flatten"]
    assert [(e["sleeve"], e["pair"], e["quantity"]) for e in closes] == [("control", "EUR.USD", 20_000)], (
        "the sleeve was at its cap, the largest book these limits allow it, and one order closed it"
    )
    assert [e["room"] for e in desk.of("tape")] == ["soft", "hard"]
    desk.stop()
    rows = read_tape(head)
    at_hard = next(n for n, row in enumerate(rows) if row[0] == "step" and row[5:] == [HARD])
    taken = sum(len(tape._line(row)) for row in rows[at_hard:])
    assert 0 < taken < reserve // 100, "what the closing took, against what was kept for it"
    assert tape_bytes(tmp_path / "live" / "days") < quota
    assert all(one.identical and one.stopped for one in desk.replay(tmp_path / "scratch"))
