"""The shadow runner: what a sleeve decides becomes a shadow order, a fill, a booked result and a journal line."""
from __future__ import annotations

import itertools
import json

import pytest

from hftlab.core import InstrumentKind, Quote, Side, SimClock, TimeInForce, sleeve_of
from hftlab.record.format import Heartbeat, Marker
from hftlab.shadow import Journal, Shadow, ShadowError, live_events, open_live, run, verify
from hftlab.shadow import __main__ as cli
from hftlab.shadow.journal import read
from hftlab.strategy import Cancel, ControlSleeve, IdleSleeve, Intent, Placed
from hftlab.venues.fake import FakeConfig, build
from hftlab.venues.port import CommissionEvent, ConnectionEvent, ConnectionState, VenueUnavailable
from hftlab.venues.supervisor import Backoff
from hftlab.venues.view import MarketDataView
from tests.shadow.kit import BIG, TRADED, constant, kinds, session, walk
from tests.support import PLACEHOLDER, ROOT
from tests.venues.kit import EURUSD, INSTRUMENTS, MS, SECOND, T0, plain_costs


class Script:
    """A sleeve for tests: it does what a table says at given quote numbers, and keeps what it was shown as state."""

    def __init__(self, name: str, plan: dict) -> None:
        self.name, self.plan = name, plan

    def initial(self) -> tuple:
        return ()

    def decide(self, state, event, now_ns, view):
        seen = (*state, (type(event).__name__, now_ns, view.quantity("EUR.USD")))
        if isinstance(event, Quote) and event.seq in self.plan:
            wanted = self.plan[event.seq]
            return seen, wanted(state) if callable(wanted) else wanted
        return seen, ()


def buy(**changes) -> Intent:
    return Intent(**{"pair": "EUR.USD", "side": Side.BUY, "quantity": 20_000, **changes})


def sell(**changes) -> Intent:
    return buy(side=Side.SELL, leg="exit", **changes)


# ---- one trade, by hand ---------------------------------------------------------------------------------------------


def test_a_round_trip_is_decided_placed_filled_booked_and_journalled(tmp_path):
    quotes = constant(40)  # 108,500 / 108,510 every 100 ms
    quotes[10:] = [Quote("EUR.USD", 108_530, 108_545, BIG, BIG, q.recv_ts_ns, q.seq) for q in quotes[10:]]
    sleeve = Script("hand", {3: (buy(),), 21: (sell(),)})
    costs = plain_costs(commission_rate_bps_x100=20)
    result, entries, shadow = session(tmp_path / "j.jsonl", [sleeve], quotes, costs=costs, source="by-hand")

    assert kinds(entries) == [
        "start",
        "decision", "order", "order_event", "order_event", "fill", "order_event", "commission",
        "decision", "order", "order_event", "order_event", "fill", "order_event", "commission",
        "stop",
    ]  # fmt: skip
    decision, order, submitted, working, fill, filled, commission = entries[1:8]
    at = quotes[2].recv_ts_ns
    assert (decision["n"], decision["at_ns"], decision["sleeve"]) == (3, at, "hand"), "decided on the third event"
    assert [decision[key] for key in ("pair", "side", "quantity", "leg")] == ["EUR.USD", "BUY", 20_000, "entry"]
    assert order["client_order_id"].startswith("hft-hand-")
    assert (order["order_type"], order["tif"]) == ("MARKET", "FOK")
    assert [e["state"] for e in (submitted, working, filled)] == ["SUBMITTED", "WORKING", "FILLED"]
    assert fill["venue_ts_ns"] == at + 5 * MS, "it reached the shadow venue 5 ms after the decision"
    assert (fill["n"], fill["at_ns"]) == (4, quotes[3].recv_ts_ns), "and the report came with the next event"
    assert (fill["price"], fill["quantity"], fill["liquidity"]) == (108_510, 20_000, "TAKER"), "bought at the ask"
    assert (commission["commission"], commission["currency"]) == (2_000_000, "USD"), "0.43 USD by rate: the minimum"
    assert entries[12]["price"] == 108_530, "sold at the bid of the book as it was then"

    pair = result["sleeves"]["hand"]["pairs"]["EUR.USD"]
    assert pair == {
        "currency": "USD", "quantity": 0, "realised": 20 * 20_000, "unrealised": 0, "price_scale": 5,
        "fees_micro": 4_000_000, "net_micro": 0, "fees_not_netted_micro": {},
    }, "bought 20,000 at 1.08510, sold at 1.08530: 4.00 USD made, 4.00 USD of fees"  # fmt: skip
    counts = {"decisions": 2, "orders": 2, "refused": 0, "fills": 2}
    assert result["sleeves"]["hand"] == {**counts, "pairs": result["sleeves"]["hand"]["pairs"]}
    assert entries[-1]["summary"] == result and result["events"] == 40
    assert entries[0]["source"] == "by-hand" and entries[0]["cost_model"]["commission_rate_bps_x100"] == 20
    assert entries[0]["instruments"][0]["price_scale"] == 5 and entries[0]["sleeves"] == ["hand"]
    assert shadow.book.position("hand", "EUR.USD").is_flat and verify(tmp_path / "j.jsonl").entries == 16


def test_an_open_position_is_valued_at_the_side_it_would_be_closed_on(tmp_path):
    quotes = constant(10)
    quotes[6:] = [Quote("EUR.USD", 108_520, 108_530, BIG, BIG, q.recv_ts_ns, q.seq) for q in quotes[6:]]
    result, _, _ = session(tmp_path / "long.jsonl", [Script("hand", {2: (buy(),)})], quotes)
    pair = result["sleeves"]["hand"]["pairs"]["EUR.USD"]
    assert (pair["quantity"], pair["unrealised"]) == (20_000, 10 * 20_000), "long from 1.08510, valued at the bid"
    assert pair["net_micro"] == 10 * 20_000 * 10 - 2_000_000 == 0
    result, _, _ = session(tmp_path / "short.jsonl", [Script("hand", {2: (sell(),)})], quotes)
    pair = result["sleeves"]["hand"]["pairs"]["EUR.USD"]
    assert (pair["quantity"], pair["unrealised"]) == (-20_000, -30 * 20_000), "short from 1.08500, valued at the ask"


def test_a_fee_in_a_currency_the_book_cannot_net_is_printed_beside_the_result_not_left_out_in_silence(tmp_path):
    shadow = Shadow(TRADED, plain_costs(), [Script("hand", {2: (buy(),)})], Journal(tmp_path / "j.jsonl"))
    for quote in constant(5):
        shadow.handle(quote)
    (fill_id,) = shadow.book.snapshot()["fills"]
    # The shadow venue charges in the quote currency. A broker charges in the account's: say 3.00 AUD for this fill.
    shadow.book.charge(CommissionEvent(fill_id, 3_000_000, "AUD", 0, 0))
    result = shadow.close()
    pair = result["sleeves"]["hand"]["pairs"]["EUR.USD"]
    assert (pair["fees_micro"], pair["fees_not_netted_micro"]) == (0, {"AUD": 3_000_000})
    assert pair["net_micro"] == -(10 * 20_000 * 10), "the spread on the open position, and no fee: the AUD is not in it"
    assert result["sleeves"]["hand"]["pairs"]["USD.JPY"]["fees_not_netted_micro"] == {}
    assert read(tmp_path / "j.jsonl")[1][-1]["summary"] == result, "and the journal keeps it with the result"


def test_a_sleeve_is_told_of_its_own_orders_before_it_sees_the_next_quote(tmp_path):
    sleeve = Script("hand", {2: (buy(),)})
    _, _, shadow = session(tmp_path / "j.jsonl", [sleeve], constant(4))
    shown = shadow._states["hand"]
    assert [name for name, _, _ in shown] == [
        "Quote", "Quote", "Placed", "OrderEvent", "OrderEvent", "Fill", "OrderEvent", "Quote", "Quote",
    ]  # fmt: skip
    at = [T0 + n * 100 * MS for n in range(4)]
    assert [when for _, when, _ in shown] == [at[0], at[1], at[1], at[2], at[2], at[2], at[2], at[2], at[3]]
    assert [held for _, _, held in shown][5:] == [20_000] * 4, "the fill is in its book by the time it is told of it"


def test_the_time_of_a_decision_is_the_events_own_time_even_when_the_recorded_clock_stepped_back(tmp_path):
    quotes = constant(6)
    quotes[3] = Quote("EUR.USD", 108_500, 108_510, BIG, BIG, T0 - SECOND, 4)  # the wall clock was corrected
    _, entries, _ = session(tmp_path / "j.jsonl", [Script("hand", {4: (buy(),)})], quotes)
    decision = next(e for e in entries if e["type"] == "decision")
    assert decision["at_ns"] == quotes[2].recv_ts_ns, "time does not run backwards: the time already reached"
    assert next(e for e in entries if e["type"] == "fill")["venue_ts_ns"] == quotes[2].recv_ts_ns + 5 * MS


# ---- what can be refused --------------------------------------------------------------------------------------------


def test_what_the_venue_will_not_take_is_refused_journalled_and_told_to_the_sleeve(tmp_path):
    plan = {
        1: (buy(pair="GBP.USD"),),  # not an instrument of this session
        2: (buy(quantity=5_000),),  # below the instrument's minimum
        3: (buy(), buy()),  # the same decision twice: the same order id
        4: (buy(limit_price=108_503, tif=TimeInForce.DAY, leg="rest"),),  # off the price grid
        5: (Cancel("hft-other-0123456789abcdef0123"),),  # another sleeve's order
        6: (Cancel("hft-hand-0123456789abcdef0123"),),  # its own name, but no such order
    }
    result, entries, shadow = session(tmp_path / "j.jsonl", [Script("hand", plan)], constant(8))
    refused = [e["reason"] for e in entries if e["type"] == "refused"]
    assert len(refused) == 6 and result["sleeves"]["hand"]["refused"] == 6
    assert "GBP.USD is not one of this session's instruments" in refused[0]
    assert "below the minimum order size" in refused[1] and "already sent" in refused[2]
    assert "price increment" in refused[3] and refused[4] == "not this sleeve's order" and "no order" in refused[5]
    assert result["sleeves"]["hand"]["orders"] == 1 and result["sleeves"]["hand"]["decisions"] == 5
    told = [name for name, _, _ in shadow._states["hand"]]
    assert told.count("Refused") == 6 and told.count("Placed") == 1


def test_a_resting_order_can_be_taken_back_by_the_sleeve_that_placed_it(tmp_path):
    def take_back(state):
        placed = [n for n, (name, _, _) in enumerate(state) if name == "Placed"]
        assert placed, "it was told the id"
        return (Cancel(ids[0]),)

    ids: list[str] = []

    class Resting(Script):
        def decide(self, state, event, now_ns, view):
            if isinstance(event, Placed):
                ids.append(event.client_order_id)
            return super().decide(state, event, now_ns, view)

    plan = {1: (buy(limit_price=108_400, tif=TimeInForce.DAY),), 5: take_back}
    result, entries, _ = session(tmp_path / "j.jsonl", [Resting("hand", plan)], constant(10))
    states = [e["state"] for e in entries if e["type"] == "order_event"]
    assert states == ["SUBMITTED", "WORKING", "CANCELLED"] and "cancel" in kinds(entries)
    assert result["sleeves"]["hand"]["fills"] == 0 and sleeve_of(ids[0]) == "hand"


def test_a_sleeve_that_answers_its_own_acknowledgements_without_end_is_stopped(tmp_path):
    class Stubborn:
        name = "stubborn"

        def initial(self) -> int:
            return 0

        def decide(self, state, event, now_ns, view):
            return state + 1, (buy(quantity=1, attempt=state),)  # refused every time, and asked again

    with pytest.raises(ShadowError, match="keeps answering"):
        session(tmp_path / "j.jsonl", [Stubborn()], constant(2))
    assert verify(tmp_path / "j.jsonl").entries > 64, "what it asked for is in the journal, and the journal is whole"


def test_a_sleeve_must_return_intents_and_sleeves_must_have_different_names(tmp_path):
    with pytest.raises(ShadowError, match="neither an intent nor a cancel"):
        session(tmp_path / "a.jsonl", [Script("hand", {1: ("buy everything",)})], constant(2))
    with pytest.raises(ShadowError, match="share a name"):
        Shadow(TRADED, plain_costs(), [IdleSleeve("same"), IdleSleeve("same")], Journal(tmp_path / "b.jsonl"))


# ---- several sleeves, several sessions ------------------------------------------------------------------------------


def test_each_sleeve_has_its_own_book_and_its_own_orders(tmp_path):
    sleeves = [Script("first", {2: (buy(),)}), Script("second", {2: (sell(),), 9: (buy(leg="exit"),)}), IdleSleeve()]
    result, entries, shadow = session(tmp_path / "j.jsonl", sleeves, constant(15))
    held = [result["sleeves"][name]["pairs"]["EUR.USD"]["quantity"] for name in ("first", "second", "idle")]
    assert held == [20_000, 0, 0]
    assert result["sleeves"]["second"]["pairs"]["EUR.USD"]["net_micro"] == -(10 * 20_000 * 10) - 4_000_000
    assert {e["sleeve"] for e in entries if e["type"] == "fill"} == {"first", "second"}
    assert shadow.book.position("desk", "EUR.USD").quantity == 20_000
    assert [name for name, _, _ in shadow._states["first"]].count("Fill") == 1, "nobody is told of another's fills"


def test_a_second_session_continues_the_journal_and_starts_flat(tmp_path):
    path = tmp_path / "j.jsonl"
    first, _, _ = session(path, [Script("hand", {2: (buy(),)})], constant(5))
    second, entries, _ = session(path, [Script("hand", {2: (buy(),)})], constant(5))
    assert (first["session"], second["session"]) == (1, 2) and (first["last_event"], second["last_event"]) == (5, 10)
    assert kinds(entries).count("start") == 2 and verify(path).sessions == 2
    orders = [e["client_order_id"] for e in entries if e["type"] == "order"]
    assert len(set(orders)) == 2, "the event number goes on counting, so the same decision later is a new order"
    assert second["sleeves"]["hand"]["pairs"]["EUR.USD"]["quantity"] == 20_000, "flat at the start, one buy since"


def test_the_same_events_and_seeds_write_the_same_journal_byte_for_byte(tmp_path):
    def journal(name: str, sleeve_seed: int, venue_seed: int, stream_seed: int = 3) -> bytes:
        settings = {"sizes": {"EUR.USD": 20_000}, "entry_per_million": 30_000, "hold_ns": SECOND, "retry_ns": SECOND}
        sleeve = ControlSleeve("control", seed=sleeve_seed, **settings)
        costs = plain_costs(latency_min_ns=20 * MS, latency_max_ns=80 * MS)
        session(tmp_path / name, [sleeve, IdleSleeve()], walk(stream_seed, 1_500), seed=venue_seed, costs=costs)
        return (tmp_path / name).read_bytes()

    base = journal("a.jsonl", 1, 1)
    assert base == journal("b.jsonl", 1, 1) and base.count(b'"type":"fill"') > 20
    assert base != journal("c.jsonl", 2, 1), "another sleeve seed: other entries"
    assert base != journal("d.jsonl", 1, 2), "another venue seed: other latencies, so other fills"
    assert base != journal("e.jsonl", 1, 1, stream_seed=4), "other quotes"


def test_heartbeats_keep_a_quiet_book_tradable_and_markers_and_connection_events_pass_through(tmp_path):
    costs = plain_costs(stale_after_ns=2 * SECOND)
    first = constant(2)
    beats = [Heartbeat("EUR.USD", T0 + n * SECOND, T0 + n * SECOND, 0) for n in range(1, 30)]
    other = [Quote("USD.JPY", 151_230, 151_240, BIG, BIG, T0 + n * SECOND, 100 + n) for n in (30, 31)]

    class OnTheOtherPair(Script):  # buys EUR.USD when USD.JPY ticks, half a minute after EUR.USD last moved
        def decide(self, state, event, now_ns, view):
            if isinstance(event, Quote) and event.seq == 130:
                return state, (buy(),)
            return super().decide(state, event, now_ns, view)

    for name, stream, filled in (("vouched", [*first, *beats, *other], 1), ("silent", [*first, *other], 0)):
        extra = [Marker(True, T0, 1), ConnectionEvent(ConnectionState.RECONCILING, T0, 1)]
        sleeve = OnTheOtherPair("hand", {})
        result, entries, _ = session(tmp_path / f"{name}.jsonl", [sleeve], [*extra, *stream], costs=costs)
        assert result["sleeves"]["hand"]["fills"] == filled, name
        assert result["events"] == len(extra) + len(stream)
    assert [e["reason"] for e in entries if e["type"] == "order_event"][-1] == "the quote is stale"


# ---- a market that fills nothing ------------------------------------------------------------------------------------

MINUTE = 60 * SECOND
ROLLOVER = 1_760_043_600 * SECOND  # 2025-10-09 21:00 UTC: 17:00 in New York
WINDOW = {"no_fill_before_ns": 5 * MINUTE, "no_fill_after_ns": 15 * MINUTE}


def test_through_a_whole_no_fill_window_the_control_sleeve_tries_once_per_wait_not_once_per_quote(tmp_path):
    quotes = constant(15_000, start_ns=ROLLOVER - 6 * MINUTE)  # 25 minutes, 100 ms apart, right across the window
    opens, shuts = ROLLOVER - 5 * MINUTE, ROLLOVER + 15 * MINUTE
    wait = 30 * SECOND

    def journal(name: str, retry_ns: int) -> list[dict]:
        # It enters on the first quote, a minute before the window, and its minute is up just as the window opens.
        settings = {"sizes": {"EUR.USD": 20_000}, "entry_per_million": 1_000_000, "hold_ns": MINUTE}
        sleeve = ControlSleeve("control", seed=4, retry_ns=retry_ns, **settings)
        return session(tmp_path / name, [sleeve], quotes, costs=plain_costs(**WINDOW))[1]

    entries = journal("a.jsonl", wait)
    inside = [e for e in entries if e["type"] == "decision" and opens <= e["at_ns"] <= shuts]
    exits = [e["at_ns"] for e in inside]
    assert {e["leg"] for e in inside} == {"exit"}, "it went into the window holding, and could not get out"
    assert len(exits) == 40 <= (shuts - opens) // wait + 1, "twenty minutes of window, thirty seconds apart"
    assert {later - earlier for earlier, later in itertools.pairwise(exits)} == {wait}
    assert [e["attempt"] for e in inside] == list(range(40)), "each one a new attempt, so a new order id"
    ended = [e["reason"] for e in entries if e["type"] == "order_event" and e["state"] == "CANCELLED"]
    assert ended == ["inside the rollover window or the market is shut"] * 40
    out = next(e for e in entries if e["type"] == "fill" and e["side"] != entries[1]["side"])
    assert 0 < out["at_ns"] - shuts <= wait, "and it is out within one wait of the window's end"

    journal("b.jsonl", wait)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes(), "a replay waits just as long"
    hasty = [e for e in journal("c.jsonl", 0) if e["type"] == "decision" and opens <= e["at_ns"] <= shuts]
    assert len(hasty) > 5_000, "with no wait it is an order for nearly every quote: what the wait is here to prevent"


# ---- live quotes: the same stream, and still no broker order ---------------------------------------------------------


class Live:
    def __init__(self, config: FakeConfig | None = None) -> None:
        self.clock = SimClock(T0)
        self.broker = build(self.clock, INSTRUMENTS, config or FakeConfig(), seed=5, backoff=Backoff())
        self.market = self.broker.market
        self.view = MarketDataView(self.broker)
        self.sent: list = []
        submit = self.market.submit
        self.market.submit = lambda *a, **k: (self.sent.append(a), submit(*a, **k))[1]  # count what reaches the broker

    def stream(self, seconds: int):
        end = T0 + seconds * SECOND

        def should_stop() -> bool:
            self.clock.advance(20 * MS)
            return self.clock.now_ns() >= end

        return live_events(self.view, self.clock, ["EUR.USD", "USD.JPY"], should_stop)


PAIRS = (("EUR.USD", InstrumentKind.CASH), ("USD.JPY", InstrumentKind.CASH))
WALK = {"interval_ns": 100 * MS, "mid": 108_505, "spread": 10, "step": 5}


def test_live_quotes_drive_the_sleeves_and_no_order_reaches_the_broker(tmp_path):
    live = Live()
    live.market.generate_quotes("EUR.USD", start_ns=T0 + 50 * MS, count=550, **WALK)
    traded = open_live(live.view, PAIRS)
    assert [i.pair for i in traded] == ["EUR.USD", "USD.JPY"] and traded[0] == EURUSD
    sizes = {"EUR.USD": 20_000, "USD.JPY": 20_000}
    settings = {"sizes": sizes, "entry_per_million": 60_000, "hold_ns": SECOND, "retry_ns": SECOND}
    sleeve = ControlSleeve("control", seed=2, **settings)
    shadow = Shadow(traded, plain_costs(), [sleeve], Journal(tmp_path / "live.jsonl"), source="venue:fake")
    result = run(shadow, live.stream(60))
    line = result["sleeves"]["control"]
    assert line["fills"] >= 10 and line["orders"] >= line["fills"], "the sleeve traded, on the shadow venue"
    quiet = line["pairs"]["USD.JPY"]
    assert (quiet["quantity"], quiet["fees_micro"], quiet["net_micro"]) == (0, 0, 0), "no quotes, no trades"
    assert live.sent == [], "nothing was submitted to the broker behind the quotes"
    assert live.broker.state is ConnectionState.RECONCILING, "and could not have been: it was never made ready"
    assert live.broker.open_orders() == () and live.broker.executions_since(0) == () and live.broker.positions() == ()
    assert verify(tmp_path / "live.jsonl").entries > 40


def test_the_live_stream_is_quotes_connection_changes_and_a_heartbeat_for_each_quiet_pair():
    live = Live()
    live.market.generate_quotes("EUR.USD", start_ns=T0 + 50 * MS, count=20, **WALK)
    open_live(live.view, PAIRS)
    seen = list(live.stream(5))
    quotes = [e for e in seen if isinstance(e, Quote)]
    beats = [e for e in seen if isinstance(e, Heartbeat)]
    assert [type(e).__name__ for e in seen[:2]] == ["ConnectionEvent", "ConnectionEvent"]
    assert 10 <= len(quotes) <= 20 and {q.pair for q in quotes} == {"EUR.USD"}, "unchanged tops are not repeated"
    assert [q.seq for q in quotes] == sorted(q.seq for q in quotes)
    assert sorted({b.pair for b in beats}) == ["EUR.USD", "USD.JPY"]
    assert [b.recv_ts_ns for b in beats if b.pair == "USD.JPY"] == [T0 + n * SECOND for n in (1, 2, 3, 4)]
    still = [b.recv_ts_ns for b in beats if b.pair == "EUR.USD"]
    assert still == [T0 + 3 * SECOND, T0 + 4 * SECOND], "its last quote came at 1.95 s: quiet from the third second"
    assert all(b.server_ts_ns == b.recv_ts_ns and b.seq == 0 for b in beats)


def test_a_dead_connection_gives_no_heartbeat_so_quotes_go_stale(tmp_path):
    live = Live(FakeConfig(outages=((T0 + 1_500 * MS, T0 + 600 * SECOND),)))
    live.market.generate_quotes("EUR.USD", start_ns=T0 + 50 * MS, count=5, **WALK)
    open_live(live.view, PAIRS)
    seen = list(live.stream(6))
    beats = [e.recv_ts_ns for e in seen if isinstance(e, Heartbeat)]
    assert beats == [T0 + SECOND], "after the drop the venue does not answer, and nothing vouches for the book"
    assert ConnectionState.DISCONNECTED in [e.state for e in seen if isinstance(e, ConnectionEvent)]


def test_live_mode_needs_the_venue_to_be_there_at_the_start():
    live = Live(FakeConfig(outages=((T0 - SECOND, T0 + 60 * SECOND),)))
    with pytest.raises(VenueUnavailable):
        open_live(live.view, PAIRS)


# ---- the command ----------------------------------------------------------------------------------------------------


def recorded_day(tmp_path, seconds: int = 120):
    from hftlab.record import __main__ as record

    frozen = ["--config", str(PLACEHOLDER / "recorder.yaml")]
    assert record.main([*frozen, "--dry-run-seconds", str(seconds), "--root", str(tmp_path / "rec")]) == 0
    return tmp_path / "rec" / "2026-10-07"


def shadow_config(tmp_path, **changes) -> str:
    text = (ROOT / "config" / "shadow.yaml").read_text()
    text = text.replace("journal_dir: var/shadow", f"journal_dir: {tmp_path / 'journals'}")
    text = text.replace("entry_per_million: 200 ", "entry_per_million: 2000 ")
    for old, new in changes.items():
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "shadow.yaml"
    path.write_text(text)
    return str(path)


def test_the_command_runs_a_recorded_day_and_its_journal_verifies(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    day = recorded_day(tmp_path)
    capsys.readouterr()
    costs = ["--cost-model", str(PLACEHOLDER / "cost_model.yaml")]
    assert cli.main(["--day", str(day), "--config", shadow_config(tmp_path), *costs]) == 0
    out, err = capsys.readouterr()
    result = json.loads(out)
    journal = tmp_path / "journals" / "2026-10-07.jsonl"
    assert result["journal"] == str(journal) and result["cost_model_is_placeholder"] is True
    assert "PLACEHOLDER" in err and "session 1 over recording:2026-10-07" in err
    idle, control = result["sleeves"]["idle"], result["sleeves"]["control"]
    assert all(pair["net_micro"] == 0 for pair in idle["pairs"].values()) and idle["orders"] == 0
    assert control["fills"] >= 4 and control["pairs"]["EUR.USD"]["fees_micro"] % 2_000_000 == 0
    assert control["pairs"]["USD.JPY"]["currency"] == "JPY" and control["pairs"]["USD.JPY"]["price_scale"] == 3
    state, entries = read(journal)
    assert entries[-1]["summary"]["sleeves"] == result["sleeves"] and state.sessions == 1

    assert cli.main(["--verify", str(journal)]) == 0
    assert json.loads(capsys.readouterr().out) | {"journal": 0, "last_hash": 0} == {
        "journal": 0, "ok": True, "entries": state.entries, "sessions": 1, "torn_last_line": False, "last_hash": 0,
    }  # fmt: skip
    damaged = journal.read_bytes().replace(b'"quantity":20000', b'"quantity":20001', 1)
    journal.write_bytes(damaged)
    assert cli.main(["--verify", str(journal)]) == 1
    assert "does not verify" in capsys.readouterr().err

    assert cli.main(["--day", str(day), "--config", shadow_config(tmp_path), *costs]) == 2, "never onto an old journal"
    assert "already holds a journal" in capsys.readouterr().err
    other = tmp_path / "again.jsonl"
    assert cli.main(["--day", str(day), "--config", shadow_config(tmp_path), "--journal", str(other), *costs]) == 0
    assert json.loads(capsys.readouterr().out)["sleeves"] == result["sleeves"], "the same day, the same result"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"journal_dir:": "journals:"}, "journal_dir"),
        ({"seed: 1 ": "seed: one "}, "seed must be a whole"),
        ({"kind: control": "kind: clever"}, "unknown kind"),
        ({"hold_seconds: 60 ": "hold_seconds: 1.5 "}, "whole numbers"),
        ({"retry_seconds: 10 ": "retry_seconds: -1 "}, "between two attempts is not negative"),
        ({"    retry_seconds: 10 ": "    # retry_seconds: 10 "}, "unknown kind or settings"),
        ({"entry_per_million: 2000 ": "entry_per_million: 2000000 "}, "entry_per_million is 0 to 1,000,000"),
        ({"  - {name: idle, kind: idle}": "  - {name: Idle Sleeve, kind: idle}"}, "not a sleeve name"),
        ({"  - {name: idle, kind: idle}": "  - idle"}, "a sleeve needs a name"),
        ({"{pair: EUR.USD, form: cash}": "{pair: EUR.USD, form: spot}"}, "form cash or cfd"),
    ],
)
def test_a_config_that_is_wrong_is_refused_with_the_reason(tmp_path, capsys, monkeypatch, change, message):
    monkeypatch.chdir(ROOT)
    day = recorded_day(tmp_path, 2)
    assert cli.main(["--day", str(day), "--config", shadow_config(tmp_path, **change)]) == 2
    assert message in capsys.readouterr().err


def test_what_cannot_be_run_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    config = shadow_config(tmp_path)
    (tmp_path / "2026-10-07").mkdir()
    for arguments, message in (
        (["--day", str(tmp_path / "nowhere")], "is not a day folder"),
        (["--day", str(tmp_path / "2026-10-07")], "does not say what its instruments are"),
        (["--venue", "sim"], "has no market data of its own"),
        (["--venue", "elsewhere"], "elsewhere"),
        (["--day", str(tmp_path), "--cost-model", str(tmp_path / "none.yaml")], "cannot read"),
        (["--day", str(tmp_path), "--calendar", str(tmp_path / "none")], "is not a directory"),
        (["--day", str(tmp_path), "--calendar", str(tmp_path)], "has no holidays.yaml and no macro_events.yaml"),
        (["--config", str(tmp_path / "none.yaml")], "cannot read"),
    ):
        assert cli.main([*arguments, "--config", config][: len(arguments) + (0 if "--config" in arguments else 2)]) == 2
        assert message in capsys.readouterr().err, arguments
    assert cli.main(["--verify", str(tmp_path / "no-journal.jsonl")]) == 0, "an absent journal is an empty chain"
    assert json.loads(capsys.readouterr().out)["entries"] == 0


def test_sleeve_sizes_are_the_larger_minimum_on_the_size_grid():
    assert cli.minimum_sizes([EURUSD], plain_costs()) == {"EUR.USD": 20_000}
    assert cli.minimum_sizes([EURUSD], plain_costs(min_order_size=25_500)) == {"EUR.USD": 26_000}
    assert cli.minimum_sizes([EURUSD], plain_costs(min_order_size=5)) == {"EUR.USD": 20_000}
