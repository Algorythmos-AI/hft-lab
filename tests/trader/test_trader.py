"""The trader's loop against the fake broker: what reaches the broker, what the shadow venue, and when neither."""
from __future__ import annotations

import json

import pytest

from hftlab.calendar import rollover_near
from hftlab.core import Side, TimeInForce
from hftlab.journal import read, verify
from hftlab.strategy import Intent, Placed, Refused
from hftlab.trader import TraderRefused
from hftlab.venues.view import MarketDataView
from tests.trader.kit import SECOND, T0, Desk, put_stage, raw_settings
from tests.venues.kit import EURUSD, plain_costs

BUY, SELL = Side.BUY, Side.SELL
HOUR = 3_600 * SECOND


def on_paper(tmp_path, stage: str = "min_size", **more) -> Desk:
    put_stage(tmp_path, "control", stage)
    return Desk(tmp_path, **more)


def decide(desk: Desk, side: Side = BUY, quantity: int = 20_000, **more):
    """Hand the trader one intent of the control sleeve, as the sleeve would on this turn."""
    desk.trader._n += 1
    return desk.trader._decide("control", Intent("EUR.USD", side, quantity, **more), "sleeve", desk.clock.now_ns())


def quiet(**changes) -> dict:
    """Settings whose control sleeve never enters by itself: the test says what it wants."""
    settings = raw_settings(**changes)
    settings["sleeves"][1]["entry_per_million"] = 0
    return settings


# ---- shadow-only: what the shipped configuration gives --------------------------------------------------------------


def test_with_limits_not_in_force_it_runs_in_shadow_only_mode_and_holds_nothing_that_could_send_an_order(tmp_path):
    desk = Desk(tmp_path, paper=False)
    desk.run(60)
    result = desk.stop()
    assert result["mode"] == "shadow_only" and desk.of("start")[0]["mode"] == "shadow_only"
    assert desk.trader.manager is None and desk.trader.gate is None and desk.edge.venue is None
    assert isinstance(desk.edge._source, MarketDataView), "a view with no order methods"
    assert desk.market._orders == {} and not (tmp_path / "outbox.jsonl").exists()
    assert len(desk.of("shadow_fill")) > 20 and desk.of("verdict") == [] and desk.of("broker_fill") == []
    assert result["books"]["broker"] is None and result["books"]["shadow"]["net_total_micro"]["USD"] < 0
    assert verify(desk.day() / "decisions.jsonl").entries == len(desk.decisions())


def test_it_refuses_to_start_a_sleeve_above_shadow_while_the_limits_are_not_in_force(tmp_path):
    put_stage(tmp_path, "control", "min_size")
    with pytest.raises(TraderRefused, match=r"control may place paper orders .* limits are not in force"):
        Desk(tmp_path, paper=False)
    assert not list(tmp_path.glob("days/*/*.tape.jsonl")), "nothing was begun"


def test_with_limits_in_force_a_sleeve_still_in_shadow_sends_nothing_to_the_broker(tmp_path):
    desk = Desk(tmp_path)  # paper mode, and every sleeve in `shadow`
    desk.run(60)
    desk.stop()
    assert desk.trader.manager is not None and len(desk.of("shadow_fill")) > 20
    assert desk.market._orders == {} and desk.of("verdict") == [] and desk.of("broker_fill") == []
    assert [e["type"] for e in desk.outbox() if e["type"] in ("intent", "refused")] == []


# ---- on paper: the gate, the manager, and two books -----------------------------------------------------------------


def test_every_order_at_the_broker_was_allowed_by_the_gate_and_journalled_before_it_was_sent(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(120)
    desk.stop()
    intents = {str(e["client_order_id"]): e for e in desk.outbox() if e["type"] == "intent"}
    verdicts = {str(e["client_order_id"]): e for e in desk.of("verdict")}
    assert len(desk.market._orders) > 40 and set(desk.market._orders) == set(intents)
    assert {order_id for order_id, v in verdicts.items() if v["sent"]} == set(intents)
    assert all(intents[order_id]["code"] == verdicts[order_id]["code"] for order_id in intents)
    refused = [v for v in verdicts.values() if not v["sent"]]
    assert refused and all(v["client_order_id"] not in desk.market._orders for v in refused)
    assert desk.trader.manager.faults == () and desk.trader.gate.latches() == ()


def test_the_same_order_goes_to_both_venues_at_the_same_moment_and_each_book_takes_only_its_own_fills(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(120)
    desk.stop()
    sent = {str(e["client_order_id"]): e["at_ns"] for e in desk.of("verdict") if e["sent"]}
    mirrored = {str(e["client_order_id"]): e["at_ns"] for e in desk.of("shadow_order")}
    assert mirrored == sent, "the same ids, on the same turns"
    broker, shadow = desk.trader.broker_book.snapshot(), desk.trader.shadow_book.snapshot()
    assert broker["fills"] and all(str(fill).startswith("X") for fill in broker["fills"])
    assert shadow["fills"] and all(str(fill).startswith("S") for fill in shadow["fills"])
    assert sorted(broker["fills"]) == sorted(e.exec_id for e in desk.market.executions())
    assert desk.held()[0] == desk.held()[2], "the broker book is what the broker holds"


def test_a_sleeve_on_paper_reads_its_position_from_the_broker_book_and_its_results_from_the_shadow_book(tmp_path):
    desk = on_paper(tmp_path, costs=plain_costs(slippage_increments=2))
    desk.run(60)
    view = desk.trader._view("control")
    broker, shadow = desk.trader.broker_book.view("control"), desk.trader.shadow_book.view("control")
    assert view.positions == broker.positions and view.realised == shadow.realised and view.fees == shadow.fees
    assert shadow.realised != broker.realised, "the two books disagree about the result, and the shadow's is shown"
    assert desk.trader._view("idle").positions == {}
    desk.stop()


def test_min_size_sends_what_adds_at_the_minimum_and_normal_sends_what_was_asked(tmp_path):
    desk = on_paper(tmp_path / "a", settings=quiet())
    desk.run(5)
    assert isinstance(decide(desk, BUY, 60_000), Placed)
    desk.run(2)
    assert desk.of("order")[-1]["quantity"] == 20_000 and desk.of("order")[-1]["stage"] == "min_size"
    assert desk.held() == (20_000, 20_000, 20_000)
    other = on_paper(tmp_path / "b", "normal", settings=quiet())
    other.run(5)
    assert isinstance(decide(other, BUY, 60_000), Placed)
    other.run(2)
    assert other.of("order")[-1]["quantity"] == 60_000
    assert other.held() == (60_000, 60_000, 60_000)


def test_a_stepped_down_sleeve_may_reduce_and_may_not_add(tmp_path):
    desk = on_paper(tmp_path, "normal", settings=quiet())
    desk.run(5)
    assert isinstance(decide(desk, BUY, 40_000), Placed)
    desk.run(2)
    desk.trader.gate.halt()  # the order manager saw something that cannot happen
    desk.run(2)
    assert desk.trader.stages.stage("control") == "stepped_down"
    added = decide(desk, BUY)
    assert isinstance(added, Refused) and added.reason == "STAGE_EXITS_ONLY"
    through = decide(desk, SELL, 60_000)
    assert isinstance(through, Refused) and through.reason == "STAGE_EXITS_ONLY", "through zero is not a reduction"
    sent = len(desk.market._orders)
    assert isinstance(decide(desk, SELL, 40_000), Placed)
    desk.run(2)
    assert len(desk.market._orders) == sent + 1 and desk.held() == (0, 0, 0)


# ---- the stage steps down at once ------------------------------------------------------------------------------------


def test_a_tripped_latch_steps_the_sleeve_down_at_once_is_journalled_and_pages(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(10)
    desk.trader.gate.halt()
    desk.run(10)
    (change,) = desk.of("stage")
    assert (change["sleeve"], change["before"], change["to"], change["cause"]) == (
        "control", "min_size", "stepped_down", "LATCH",
    )  # fmt: skip
    assert change["detail"] == "ORDER_FAULT" and desk.of("latch")[0]["latch"] == "ORDER_FAULT"
    assert "latch.order-fault" in desk.pages.fired and any(key.startswith("stage.control.") for key in desk.pages.fired)
    staged = [e for e in read(tmp_path / "stages.jsonl")[1] if e["type"] == "stage"]
    assert [e["to"] for e in staged] == ["min_size", "stepped_down"], "in the stage journal, which a restart reads"
    entries = [e for e in desk.of("verdict") if e["at_ns"] > change["at_ns"] and e["sent"]]
    orders = {str(e["client_order_id"]): e for e in desk.of("order")}
    assert all(orders[str(e["client_order_id"])]["reducing"] == 1 for e in entries), "nothing that adds went out after"
    desk.restart()
    assert desk.trader.stages.stage("control") == "stepped_down", "it survives a restart"


def test_a_reset_is_asked_for_by_an_operator_done_by_the_running_trader_and_does_not_give_the_stage_back(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(5)
    desk.trader.gate.halt()
    desk.run(5)
    desk.ask("reset", "ORDER_FAULT", operator="owner")
    desk.ask("reset", "DAILY_LOSS_DESK/USD", operator="owner")  # not tripped: refused, and written down as refused
    desk.run(5)
    asked = desk.of("request")
    assert [(e["subject"], e["done"], e["operator"]) for e in asked] == [
        ("ORDER_FAULT", 1, "owner"), ("DAILY_LOSS_DESK/USD", 0, "owner"),
    ]  # fmt: skip
    assert desk.trader.gate.latches() == () and "latch.order-fault" in desk.pages.resolved
    assert next(e for e in read(tmp_path / "risk.jsonl")[1] if e["type"] == "reset")["operator"] == "owner"
    assert desk.trader.stages.stage("control") == "stepped_down", "the way back up is the rule, not the reset"
    assert list((tmp_path / "requests").iterdir()) == []


def test_a_reconciliation_break_steps_every_paper_sleeve_down_and_pages_and_nothing_new_is_sent(tmp_path):
    settings = quiet(reconcile_seconds=2)
    desk = on_paper(tmp_path, settings=settings)  # the grace time of the test limits is five seconds
    desk.run(5)
    desk.market.carry(EURUSD, 20_000)  # a position at the broker that no fill of ours explains
    desk.run(12)
    assert desk.of("break")[0]["broken"] == 1 and "reconciliation_break" in desk.pages.fired
    assert desk.trader.stages.stage("control") == "stepped_down" and desk.of("stage")[0]["cause"] == "BREAK"
    refused = decide(desk, BUY)
    assert isinstance(refused, Refused) and refused.reason == "STAGE_EXITS_ONLY"
    assert desk.market._orders == {}
    assert desk.trader.summary()["books"]["reconciliation_breaks_today"] == 1
    desk.market.carry(EURUSD, -20_000)
    desk.run(6)
    assert [e["broken"] for e in desk.of("break")] == [1, 0] and desk.pages.resolved == ["reconciliation_break"]
    assert desk.trader.stages.stage("control") == "stepped_down", "the books agreeing again is not a promotion"


# ---- the switches ----------------------------------------------------------------------------------------------------


def test_kill_stops_entries_on_paper_and_exits_still_go(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(30)
    desk.switched.add("KILL")
    desk.run(2)
    seen = desk.of("switches")[-1]
    assert seen["on"] == ["KILL"]
    desk.run(60)
    after = [e for e in desk.of("verdict") if e["at_ns"] > seen["at_ns"]]
    orders = {str(e["client_order_id"]): e for e in desk.of("order")}
    assert {e["code"] for e in after if not e["sent"]} == {"KILL_SWITCH"}
    assert all(orders[str(e["client_order_id"])]["reducing"] == 1 for e in after if e["sent"])
    assert desk.held() == (0, 0, 0), "what was held when KILL was set was closed by the sleeve's own exit"


def test_flatten_closes_every_position_in_both_books(tmp_path):
    desk = on_paper(tmp_path, "normal", settings=quiet())
    desk.run(5)
    assert isinstance(decide(desk, BUY, 40_000), Placed)
    desk.run(3)
    assert desk.held() == (40_000, 40_000, 40_000)
    desk.switched.add("FLATTEN")
    desk.run(5)
    assert desk.held() == (0, 0, 0)
    closes = [e for e in desk.of("decision") if e["source"] == "flatten"]
    assert len(closes) == 1 and (closes[0]["side"], closes[0]["quantity"]) == ("SELL", 40_000)
    assert desk.of("verdict")[-1]["code"] == "FLATTEN_SWITCH" and desk.of("verdict")[-1]["sent"] == 1


def test_in_shadow_the_switches_do_what_they_would_do_on_paper(tmp_path):
    desk = Desk(tmp_path, paper=False)
    desk.run(21)
    held = desk.held()[1]
    assert held != 0, "the test wants a position to close"
    desk.switched.add("FLATTEN")
    desk.run(10)
    assert desk.held()[1] == 0 and [e["source"] for e in desk.of("decision")].count("flatten") == 1
    assert "FLATTEN_SWITCH" in {e["code"] for e in desk.of("refused")}
    desk.switched = {"KILL"}
    before = len(desk.of("shadow_order"))
    desk.run(30)
    assert len(desk.of("shadow_order")) == before and "KILL_SWITCH" in {e["code"] for e in desk.of("refused")}


# ---- stop, and start again -------------------------------------------------------------------------------------------


def test_a_stop_cancels_nothing_and_the_next_start_takes_working_orders_back_before_anything_is_sent(tmp_path):
    desk = on_paper(tmp_path, settings=quiet())
    desk.run(5)
    resting = decide(desk, BUY, 20_000, tif=TimeInForce.DAY, limit_price=108_300)
    assert isinstance(resting, Placed)
    desk.run(2)
    result = desk.stop()
    assert desk.kinds()[-1] == "stop" and result["mode"] == "paper"
    assert [b.order.client_order_id for b in desk.market.working()] == [resting.client_order_id], "left working"
    assert "cancel" not in [e["type"] for e in desk.outbox()]
    assert (desk.day() / "session-001.tape.jsonl").read_bytes().rstrip().endswith(b"]") and verify(
        tmp_path / "outbox.jsonl"
    ).torn is False
    desk.start()
    desk.run(5)
    kinds = [e["type"] for e in desk.outbox()]
    assert desk.market.working() == [] and kinds.count("cancel") == 1
    assert kinds.index("cancel") < kinds.index("reconciled", kinds.index("cancel")), "taken back, then reconciled"
    assert desk.trader.manager.tracked(resting.client_order_id).stage.value == "CANCELLED"
    assert kinds.count("intent") == 1, "and nothing was sent in between"


def test_a_position_held_over_a_restart_is_closed_by_the_trader_once_and_the_sleeve_goes_on_after(tmp_path):
    desk = on_paper(tmp_path, settings=quiet())
    desk.run(5)
    assert isinstance(decide(desk, BUY), Placed)
    desk.run(2)
    desk.restart()  # the sleeve's memory of its position is gone; the books are not
    assert desk.held() == (20_000, 20_000, 20_000)
    desk.run(10)
    assert [(e["sleeve"], e["pair"], e["quantity"]) for e in desk.of("inherited")] == [("control", "EUR.USD", 20_000)]
    closes = [e for e in desk.of("decision") if e["source"] == "inherit"]
    assert [(e["side"], e["quantity"]) for e in closes] == [("SELL", 20_000)], "one close, not one a beat"
    assert desk.held() == (0, 0, 0)
    assert isinstance(decide(desk, BUY), Placed), "flat again, the pair is the sleeve's again"


def test_while_a_pair_holds_an_inherited_position_the_sleeve_may_not_trade_it(tmp_path):
    desk = on_paper(tmp_path, settings=quiet(close_retry_seconds=30))
    desk.run(5)
    assert isinstance(decide(desk, BUY), Placed)
    desk.run(2)
    desk.restart()
    desk.switched.add("KILL")  # nothing here stops an exit, so stop the venue instead: no quotes, a stale book
    desk.run(1)
    refused = desk.trader._act("control", Intent("EUR.USD", BUY, 20_000), desk.clock.now_ns())
    assert isinstance(refused, Refused) and refused.reason == "INHERITED_POSITION"


# ---- when the two venues answer differently --------------------------------------------------------------------------


def test_an_exit_the_broker_filled_never_opens_the_other_way_in_the_shadow_book(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(60, size=1_000)  # too little on display for the shadow venue's fill-or-kill; the fake broker fills
    desk.stop()
    assert len(desk.of("broker_fill")) > 10 and desk.of("shadow_fill") == []
    skipped = desk.of("shadow_skipped")
    assert skipped and {e["code"] for e in skipped} == {"NOTHING_TO_REDUCE"}
    exits = {str(e["client_order_id"]) for e in desk.of("order") if e["reducing"]}
    assert {str(e["client_order_id"]) for e in skipped} == exits, "every exit, and only the exits"
    assert desk.trader.shadow_book.open_lines() == {} and desk.trader.shadow_book.snapshot()["fills"] == []
    (line,) = desk.of("slippage")[0]["lines"]
    assert line["count"] == 0 and line["broker_only"] == len(desk.of("verdict")) - sum(
        1 for e in desk.of("verdict") if not e["sent"]
    )


def test_a_shadow_position_the_broker_never_had_is_closed_on_the_shadow_venue_and_the_difference_journalled(tmp_path):
    desk = on_paper(tmp_path, reject=lambda order: "margin" if order.side is BUY else None)
    desk.run(120)
    aligned = desk.of("align")
    assert aligned, "the broker rejected buys the shadow venue filled"
    assert all(e["broker"] == 0 and e["shadow"] == 20_000 and e["target"] == 0 for e in aligned)
    closing = [e for e in desk.of("decision") if e["source"] == "align"]
    assert len(closing) == len(aligned) and {(e["side"], e["quantity"]) for e in closing} == {("SELL", 20_000)}
    ids = {str(e["client_order_id"]) for e in desk.of("order") if e["sleeve"] == "control"}
    at_broker = set(desk.market._orders)
    assert all(order_id in ids for order_id in at_broker)
    align_ids = {str(o["client_order_id"]) for o in desk.of("order") for d in closing if o["n"] == d["n"]}
    assert align_ids and not align_ids & at_broker, "an aligning order is the shadow venue's alone"
    desk.run(10, quotes=True)
    broker, shadow, _ = desk.held()
    assert abs(shadow) <= abs(broker) or shadow == 0 or desk.trader._busy("control", "EUR.USD")
    desk.stop()


def test_the_slippage_report_says_how_much_worse_the_shadow_venue_filled_in_exact_increments(tmp_path):
    desk = on_paper(tmp_path, costs=plain_costs(slippage_increments=2))
    desk.run(120)
    desk.stop()
    (line,) = desk.of("slippage")[0]["lines"]
    assert (line["sleeve"], line["pair"]) == ("control", "EUR.USD") and line["count"] > 30
    assert (line["lowest_increments"], line["highest_increments"], line["mean_increments"]) == (2, 2, 2)
    assert line["sum_increments"] == 2 * line["count"] and line["inexact"] == 0
    assert all(type(value) is int for key, value in line.items() if key not in ("sleeve", "pair"))


# ---- the day ---------------------------------------------------------------------------------------------------------


def test_a_session_ends_at_the_rollover_and_the_next_one_starts_as_a_restart_would(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    desk = on_paper(tmp_path, at_ns=rollover - 20 * SECOND)
    desk.quoted = desk.clock.now_ns()
    first = desk.trader.label
    assert desk.run(40) is True, "the day closed under it"
    assert desk.clock.now_ns() < rollover + 2 * SECOND, "and it took no turn after the one that saw the close"
    result = desk.stop()
    assert desk.of("stop")[0]["day_closed"] == 1 and result["day"] == first
    days = [e for e in read(tmp_path / "stages.jsonl")[1] if e["type"] == "day"]
    assert [(e["sleeve"], e["clean"], e["causes"]) for e in days] == [
        ("control", 0, ["TOO_SHORT"]), ("idle", 0, ["TOO_SHORT"]),
    ]  # fmt: skip
    desk.session()  # the same connection; a new session
    assert desk.broker.state.value == "READY"
    assert desk.trader.label != first and desk.trader.session == 1 and desk.trader.manager.blind
    desk.run(30)
    assert not desk.trader.manager.blind and desk.of("verdict"), "reconciled, and trading again in the new day"
    assert sorted(path.name for path in (tmp_path / "days").iterdir()) == sorted([first, desk.trader.label])


def test_the_status_file_says_where_the_trader_stands_in_whole_numbers(tmp_path):
    desk = on_paper(tmp_path)
    desk.run(60)
    said = json.loads((tmp_path / "status.json").read_text())
    assert said["mode"] == "paper" and said["connection"] == "READY" and said["day"] == desk.trader.label
    control = next(sleeve for sleeve in said["sleeves"] if sleeve["name"] == "control")
    assert control["stage"] == "min_size" and control["trades_today"] == control["trades_total"] > 10
    assert set(control["net_total_micro"]) == {"USD", "JPY"} and type(control["net_total_micro"]["USD"]) is int
    assert said["books"]["broker"]["net_total_micro"]["USD"] < 0 and said["books"]["reconciliation_breaks_today"] == 0
    assert "." not in json.dumps(said).replace("EUR.USD", "").replace("USD.JPY", ""), "no float anywhere in it"
    desk.stop()


def test_a_sleeve_is_taken_down_by_an_operator_only_when_it_is_flat_and_never_up(tmp_path):
    desk = on_paper(tmp_path, "normal", settings=quiet())
    desk.run(5)
    assert isinstance(decide(desk, BUY), Placed)
    desk.run(2)
    desk.ask("stage", "control", "shadow")
    desk.run(2)
    assert desk.of("request")[-1]["refused"] == "NOT_FLAT" and desk.trader.stages.stage("control") == "normal"
    assert isinstance(decide(desk, SELL), Placed)
    desk.run(2)
    desk.ask("stage", "control", "shadow", operator="sam")
    desk.run(2)
    (change,) = desk.of("stage")
    assert (change["to"], change["cause"], change["detail"]) == ("shadow", "OPERATOR", "sam")
    desk.ask("stage", "control", "normal")
    desk.ask("stage", "control", "min_size")
    desk.run(2)
    assert desk.trader.stages.stage("control") == "shadow" and [e["done"] for e in desk.of("request")[-2:]] == [0, 0]
    sent = len(desk.market._orders)
    assert isinstance(decide(desk, BUY), Placed)
    desk.run(2)
    assert len(desk.market._orders) == sent and desk.held() == (0, 20_000, 0), "its orders are the shadow venue's now"


def test_a_second_exit_behind_one_still_on_its_way_is_not_a_reduction(tmp_path):
    desk = on_paper(tmp_path, "normal", settings=quiet())
    desk.run(5)
    assert isinstance(decide(desk, BUY, 40_000), Placed)
    desk.run(2)
    desk.trader.gate.halt()
    desk.run(2)  # stepped down: only what reduces may go
    assert isinstance(decide(desk, SELL, 40_000, tif=TimeInForce.DAY, limit_price=108_900), Placed), "rests, unfilled"
    behind = decide(desk, SELL, 40_000, attempt=1)
    assert isinstance(behind, Refused) and behind.reason == "STAGE_EXITS_ONLY", "both filling would end short"
    assert desk.held()[0] == 40_000


def test_at_the_days_close_a_sleeve_that_met_its_rule_goes_up_and_the_owner_is_told(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    settings = raw_settings(clean_day_minutes=1)
    settings["rollout"]["control"]["promote"]["min_size"] = {"clean_days": 1, "decision": "HFT-DEC-0042"}
    desk = Desk(tmp_path, settings=settings, at_ns=rollover - 90 * SECOND)
    desk.quoted = desk.clock.now_ns()
    assert desk.run(120) is True
    desk.stop()
    (change,) = desk.of("stage")
    assert (change["sleeve"], change["before"], change["to"], change["cause"], change["detail"]) == (
        "control", "shadow", "min_size", "PROMOTED", "HFT-DEC-0042",
    )  # fmt: skip
    assert desk.market._orders == {}, "the day it earned the step, it was still in shadow"
    assert [key for key in desk.pages.fired if key.startswith("stage.control.")], "the owner is told"
    desk.session()
    desk.run(60)
    assert desk.trader.stages.stage("control") == "min_size"
    assert {e["code"] for e in desk.of("verdict")} == {"ROLLOVER_WINDOW"}, "its intents go to the gate now, which rules"


def test_in_shadow_only_mode_the_same_rule_is_met_and_refused_and_the_refusal_is_in_the_journal(tmp_path):
    rollover = rollover_near(T0 + 12 * HOUR)
    settings = raw_settings(clean_day_minutes=1)
    settings["rollout"]["control"]["promote"]["min_size"] = {"clean_days": 1, "decision": "HFT-DEC-0042"}
    desk = Desk(tmp_path, paper=False, settings=settings, at_ns=rollover - 90 * SECOND)
    desk.quoted = desk.clock.now_ns()
    assert desk.run(120) is True
    desk.stop()
    assert desk.of("stage") == [] and desk.trader.stages.stage("control") == "shadow"
    held = [e for e in read(tmp_path / "stages.jsonl")[1] if e["type"] == "held"]
    assert [(e["sleeve"], e["to"], e["cause"]) for e in held] == [("control", "min_size", "LIMITS_NOT_IN_FORCE")]
    desk.session()  # and the next start is not refused: nothing is above shadow
    assert desk.trader.paper is False
