"""The trader on a cash account: the float is funded on request, sleeves trade around it, a restart loses nothing,
and a recorded day replays byte for byte (ADR 0031, ADR 0005). Fake broker, made-up balances."""
from __future__ import annotations

import dataclasses
import io
import json

import pytest
import yaml

from hftlab.book import FLOAT
from hftlab.trader.__main__ import EXIT_OK, fund_plan
from hftlab.trader.session import TraderRefused
from tests.inventory.kit import FLOATS, RESERVES, UNIT, Bureau, account, loaded_limits, put_stage
from tests.venues.kit import MS, SECOND, T0


def requests(desk: Bureau) -> list[tuple[str, int, str]]:
    return [(str(e["what"]), int(e["done"]), str(e["refused"])) for e in desk.of("request")]


def sent(desk: Bureau, sleeve: str = FLOAT) -> list[dict[str, object]]:
    return [e for e in desk.outbox() if e["type"] == "order" and str(e["client_order_id"]).startswith(sleeve + "-")]


def funded(folder, config=None, *, stage: str | None = "min_size") -> Bureau:
    if stage:
        put_stage(folder, "control", stage)
    desk = Bureau(folder, config)
    desk.fund()
    return desk


def test_a_funding_is_asked_for_and_the_running_trader_converts_until_the_float_stands(tmp_path):
    desk = Bureau(tmp_path)
    desk.run(3)
    assert desk.trader.float_status()["phase"] == "none" and desk.float_lines() == {}
    assert desk.cash("AUD") == 1_000_000 and desk.cash("USD") == 0
    desk.fund()
    assert requests(desk) == [("fund", 1, "")]
    assert desk.float_lines()["EUR.USD"] == FLOATS["EUR"] and desk.float_lines()["AUD.USD"] < 0
    for code, units in FLOATS.items():
        assert desk.cash(code) >= units, code
    kinds = [(e["type"], e.get("what")) for e in desk.ledger()]
    assert kinds == [("anchor", None), ("open", "fund"), ("closed", "fund")]
    said = desk.trader.float_status()
    assert said["baseline"] == {c: a for c, a in desk.market.cash().items()} and said["waiting"] == []
    assert desk.positions() == desk.float_lines(), "the venue's positions are the book's: the float's lines"


def test_no_sleeve_and_no_shadow_book_ever_sees_a_conversion(tmp_path):
    desk = funded(tmp_path, stage=None)
    assert desk.trader.shadow_book.open_lines().keys().isdisjoint({(FLOAT, "EUR.USD"), (FLOAT, "AUD.USD")})
    assert FLOAT not in desk.trader.shadow_book.sleeves()
    assert all(e["sleeve"] == FLOAT for e in desk.of("broker_fill"))
    summary = desk.trader.summary()
    assert [s["name"] for s in summary["sleeves"]] == ["idle", "control"]


def test_asking_twice_converts_once(tmp_path):
    desk = funded(tmp_path, stage=None)
    orders, cash = len(sent(desk)), desk.market.cash()
    desk.ask("fund", "float")
    desk.run(10)
    assert requests(desk)[-1] == ("fund", 0, "ALREADY_FUNDED")
    assert len(sent(desk)) == orders and desk.market.cash() == cash
    assert [e["type"] for e in desk.ledger()].count("open") == 1


def test_a_trader_killed_in_the_middle_of_a_funding_goes_on_with_what_is_left_and_no_more(tmp_path):
    desk = Bureau(tmp_path)
    desk.run(3)
    desk.ask("fund", "float")
    desk.run(2)
    assert desk.trader.float_status()["phase"] == "funding"
    assert "EUR.USD" not in desk.float_lines(), "the dollars are raised; the euros are not bought yet"
    target = dict(desk.ledger()[-1]["targets"])
    desk.restart()
    assert desk.trader.float_status()["phase"] == "funding", "the ledger is read again"
    desk.run(30)
    assert desk.trader.float_status()["phase"] == "funded"
    assert desk.float_lines() == target, "what the first process converted was not converted again"
    assert [e["type"] for e in desk.ledger()] == ["anchor", "open", "closed"]
    assert desk.cash("EUR") == FLOATS["EUR"]


def test_a_conversion_filled_in_parts_across_a_dropped_link_still_ends_on_its_target(tmp_path):
    down = T0 + 4 * SECOND + 300 * MS
    config = account(fill_chunks=2, chunk_gap_ns=400 * MS, outages=((down, down + 5 * SECOND),))
    desk = Bureau(tmp_path, config)
    desk.fund(60)
    assert desk.float_lines()["EUR.USD"] == FLOATS["EUR"]
    assert desk.positions() == desk.float_lines()


def test_a_sleeve_trades_both_ways_around_the_float_and_the_pair_it_cannot_hold_stays_on_the_shadow_venue(tmp_path):
    desk = funded(tmp_path)
    desk.run(120)
    orders = desk.of("order")
    on_paper = {(o["pair"], o["side"]) for o in orders if o["sleeve"] == "control"}
    assert {("EUR.USD", "BUY"), ("EUR.USD", "SELL"), ("AUD.USD", "BUY"), ("AUD.USD", "SELL")} <= on_paper
    assert not [e for e in desk.outbox() if e["type"] == "order" and e["pair"] == "USD.JPY"], "never sent"
    assert "USD.JPY" not in desk.positions()
    fills = desk.of("broker_fill")
    assert {f["sleeve"] for f in fills} == {FLOAT, "control"} and len(fills) > 10
    codes = {v["code"] for v in desk.of("verdict")}
    assert "PAIR_NOT_TRADABLE" not in codes, "the trader does not even propose it"
    for code, units in RESERVES.items():
        assert desk.cash(code) >= units
    assert "float-stuck" not in desk.pages.fired and not [n for n in desk.pages.fired if n.startswith("break")]


def test_the_float_is_taken_back_only_while_every_sleeve_is_flat(tmp_path):
    desk = funded(tmp_path)
    for _ in range(40):
        desk.run(1)
        if desk.held()[0] or desk.held("AUD.USD")[0]:
            break
    assert desk.held()[0] or desk.held("AUD.USD")[0]
    desk.ask("unfund", "float")
    desk.run(1)
    assert requests(desk)[-1][2] in ("NOT_FLAT", "NOT_SETTLED") and desk.trader.float_status()["phase"] == "funded"


def test_an_unfunding_brings_the_account_back_to_one_currency_but_for_the_hubs_reserve(tmp_path):
    desk = funded(tmp_path, stage=None)
    desk.run(5)
    desk.ask("unfund", "float")
    desk.run(40)
    assert requests(desk)[-1] == ("unfund", 1, "")
    said = desk.trader.float_status()
    assert said["phase"] == "none" and said["baseline"] is None
    assert desk.cash("EUR") == 0, "all of a currency goes back: its reserve is no longer kept"
    left = desk.float_lines()
    assert set(left) <= {"AUD.USD"} and desk.positions() == left
    closed = [e for e in desk.ledger() if e["type"] == "closed"]
    assert [e["what"] for e in closed] == ["fund", "unfund"] and closed[-1]["left"] == left
    # What stays in dollars is the hub's reserve and what was too little to send as one order.
    assert RESERVES["USD"] <= desk.cash("USD") < RESERVES["USD"] + 25_000 and desk.cash("AUD") > 970_000
    assert "float-stuck" in desk.pages.fired and "float-stuck" in desk.pages.resolved
    desk.ask("unfund", "float")
    desk.run(3)
    assert requests(desk)[-1] == ("unfund", 0, "NOT_FUNDED")
    desk.ask("fund", "float")
    desk.run(30)
    assert desk.trader.float_status()["phase"] == "funded", "and it can be funded again, from what is there"
    assert desk.cash("EUR") >= FLOATS["EUR"] and desk.positions() == desk.float_lines()


def test_an_account_that_cannot_cover_the_float_is_refused_and_nothing_is_sent(tmp_path):
    desk = Bureau(tmp_path, account(cash=(("AUD", 100_000 * UNIT),)))
    desk.run(3)
    desk.ask("fund", "float")
    desk.run(10)
    assert requests(desk) == [("fund", 0, "CANNOT_COVER")]
    assert not sent(desk) and desk.ledger() == [] and desk.cash("AUD") == 100_000


def test_a_funded_day_of_paper_trading_replays_to_the_same_decisions_byte_for_byte(tmp_path):
    desk = Bureau(tmp_path / "day")
    put_stage(tmp_path / "day", "control", "min_size")
    desk.restart()
    desk.run(3)
    desk.ask("fund", "float")
    desk.run(2)
    desk.restart()  # killed in the middle of the funding
    desk.run(90)
    desk.stop()
    assert desk.trader.float_status()["phase"] == "funded" and len(desk.of("broker_fill")) > 5
    replayed = desk.replay(tmp_path / "scratch")
    assert len(replayed) >= 2 and all(one.identical and not one.dark for one in replayed), replayed


# ---- the plan, as the command prints it -------------------------------------------------------------------------


def test_the_plan_is_printed_from_what_the_running_trader_last_heard_and_sends_nothing(tmp_path):
    desk = Bureau(tmp_path / "state")
    desk.run(70)  # one reconciliation: the venue has said what the account holds
    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["float"]["cash"] == {"AUD": 1_000_000 * UNIT} and status["float"]["phase"] == "none"
    risk_file = tmp_path / "risk.yaml"
    risk_file.write_text(yaml.safe_dump(loaded_limits()))
    where = dataclasses.replace(desk.settings, state_dir=tmp_path / "state")
    out = io.StringIO()
    assert fund_plan(where, risk_file, out, now_ns=desk.clock.now_ns()) == EXIT_OK
    lines = out.getvalue().splitlines()
    steps = [line.split()[:3] for line in lines if line.startswith(("  SELL", "  BUY"))]
    assert [(verb, pair) for verb, _, pair in steps] == [("SELL", "AUD.USD"), ("BUY", "EUR.USD")]
    assert int(steps[1][1]) == FLOATS["EUR"] and "Nothing was sent" in lines[-1]
    assert not sent(desk) and desk.ledger() == []
    with pytest.raises(TraderRefused, match="five minutes"):
        fund_plan(where, risk_file, out, now_ns=desk.clock.now_ns() + 600 * SECOND)
    (tmp_path / "state" / "status.json").unlink()
    with pytest.raises(TraderRefused, match="does not say what the account holds"):
        fund_plan(where, risk_file, out, now_ns=desk.clock.now_ns())


def test_a_plan_for_a_float_that_stands_says_there_is_nothing_to_plan(tmp_path):
    desk = funded(tmp_path / "state", stage=None)
    desk.run(70)
    risk_file = tmp_path / "risk.yaml"
    risk_file.write_text(yaml.safe_dump(loaded_limits()))
    out = io.StringIO()
    where = dataclasses.replace(desk.settings, state_dir=tmp_path / "state")
    assert fund_plan(where, risk_file, out, now_ns=desk.clock.now_ns()) == EXIT_OK
    assert "nothing to plan" in out.getvalue() and "SELL" not in out.getvalue()


# ---- the status screen ------------------------------------------------------------------------------------------


def test_the_status_screen_shows_the_float_the_paper_positions_and_fills_and_nothing_of_it_is_published(tmp_path):
    from hftlab.ops.status import read, render
    from hftlab.publish.snapshot import build
    from tests.ops.kit import sources

    folder = tmp_path / "var" / "trader"
    desk = Bureau(folder)
    desk.run(70)
    places = sources(tmp_path, trader_status=folder / "status.json")
    before = render(read(places, desk.clock.now_ns() + SECOND))
    assert "float: NOT FUNDED: no entry is sent" in before and "the account holds AUD 1,000,000" in before
    put_stage(folder, "control", "min_size")
    desk.restart()
    desk.fund()
    for _ in range(60):
        desk.run(2)
        if desk.held()[0] or desk.held("AUD.USD")[0]:
            break
    reading = read(places, desk.clock.now_ns() + SECOND)
    found = reading.trader.inventory
    assert found.phase == "funded" and found.baseline["EUR"] == FLOATS["EUR"] and found.shadow_only == ("USD.JPY",)
    assert found.paper_fills["float"] >= 1 and found.paper_fills.get("control", 0) >= 1
    assert len(found.positions) >= 1 and all(line.startswith("control/") for line in found.positions)
    screen = render(reading)
    assert "float: funded; the account holds" in screen and "float baseline AUD" in screen and "in no result" in screen
    assert "paper positions: control/" in screen and "on the shadow venue only: USD.JPY" in screen
    assert "sleeve control: min_size" in screen and "shadow net" in screen
    published = json.dumps(build(reading, phases={f"F{n}": "open" for n in range(8)}, version="0123456789ab"))
    for word in ("baseline", "float", "cash", str(FLOATS["EUR"]), "739"):
        assert word not in published, f"a balance is on the host's screen and never in the snapshot: {word}"
