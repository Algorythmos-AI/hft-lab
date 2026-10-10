"""What is particular to the broker adapter: the gateway's messages, what leaves, and what stays out."""
from __future__ import annotations

import ast
import asyncio
import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from ib_async import IB, Contract, Execution, OrderStatus, Trade
from ib_async import Fill as IbFill
from ib_async import Order as IbOrder
from ib_async.objects import CommissionReport, TradeLogEntry

from hftlab.core import InstrumentKind, OrderEvent, OrderState, Quote, Side, SimClock, WallClock
from hftlab.safety import VENUES, PaperGate, PaperOnlyError
from hftlab.venues.fake import FakeConfig
from hftlab.venues.ibkr.adapter import IbkrSettings, IbkrVenue
from hftlab.venues.ibkr.client import IbAsyncClient
from hftlab.venues.port import ConnectionEvent, ConnectionState, NotReady, OrderRefused, VenueNotice, VenueUnavailable
from hftlab.venues.supervisor import Backoff
from tests.support import called_name, sources
from tests.venues.kit import ASK, BID, MS, SECOND, T0, Harness, an_order, brief, of
from tests.venues.stub_ib import StubIb

S = ConnectionState
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
ADAPTER = "src/hftlab/venues/ibkr/adapter.py"
CLIENT = "src/hftlab/venues/ibkr/client.py"


def states(events) -> list[str]:
    return [e.state.name for e in of(events, ConnectionEvent)]


def ready(config=None, **options) -> tuple[Harness, object]:
    h = Harness("ibkr", config, **options)
    return h, h.ready()


# ---- identity -------------------------------------------------------------------------------------------------------


def test_the_adapter_is_the_registered_paper_venue_and_the_fake_is_not_a_venue_at_all():
    h, _ = ready()
    assert h.venue.name == "ibkr_paper" and VENUES[h.venue.name].needs_paper_lock is True
    assert Harness("fake").venue.name not in VENUES


def test_the_defaults_are_a_paper_gateway_on_this_machine():
    settings = IbkrSettings()
    assert (settings.host, settings.port, settings.client_id, settings.account) == ("127.0.0.1", 4002, 77, None)


def test_without_a_gate_the_adapter_builds_one_from_the_environment_and_its_own_port(monkeypatch):
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setenv("HFT_IBKR_USERNAME", "trader1")
    monkeypatch.setenv("HFT_IBKR_LIVE_USERNAME", "trader1")
    clock = SimClock(T0)
    stub = StubIb(clock, Harness("fake").market)
    venue = IbkrVenue(clock=clock, client=stub, settings=IbkrSettings(port=7497))
    with pytest.raises(PaperOnlyError, match="is the declared live username"):
        venue.connect()
    assert stub.calls == []
    monkeypatch.setenv("HFT_IBKR_USERNAME", "paperlogin")
    again = IbkrVenue(clock=clock, client=StubIb(clock, Harness("fake").market), settings=IbkrSettings(port=7497))
    again.connect()
    assert again.state is S.RECONCILING


# ---- what leaves ----------------------------------------------------------------------------------------------------


def test_an_order_leaves_with_its_id_as_the_reference_and_no_float_in_it():
    h, instrument = ready()
    order = an_order(side=Side.SELL, quantity=25_000, limit=108_515)
    h.venue.place(order, instrument)
    (sent,) = h.stub.sent
    assert sent.orderRef == order.client_order_id and sent.account == "DU1234567"
    assert (sent.action, sent.orderType, sent.tif, sent.transmit) == ("SELL", "LMT", "DAY", True)
    assert type(sent.totalQuantity) is int and sent.totalQuantity == 25_000
    assert isinstance(sent.lmtPrice, Decimal) and str(sent.lmtPrice) == "1.08515"


def test_a_login_with_several_accounts_needs_one_named_for_orders():
    config = FakeConfig(accounts=("DU1111111", "DU2222222"))
    h, instrument = ready(config)
    with pytest.raises(OrderRefused, match="several accounts"):
        h.venue.place(an_order(1), instrument)
    assert h.stub.sent == []

    named = Harness("ibkr", config)
    named.venue._settings = dataclasses.replace(named.venue._settings, account="DU2222222")
    instrument = named.ready()
    named.venue.place(an_order(1), instrument)
    assert named.stub.sent[0].account == "DU2222222"

    wrong = Harness("ibkr", config)
    wrong.venue._settings = dataclasses.replace(wrong.venue._settings, account="DU9999999")
    instrument = wrong.ready()
    with pytest.raises(OrderRefused, match="not one the gateway manages"):
        wrong.venue.place(an_order(1), instrument)


def test_an_order_from_an_earlier_process_can_be_found_and_cancelled():
    first, instrument = ready()
    resting = an_order(limit=108_400)
    first.venue.place(resting, instrument)
    first.after(30 * MS)

    clock, market = first.clock, first.market  # the broker and its book outlive the process
    stub = StubIb(clock, market)
    second = IbkrVenue(clock=clock, client=stub, gate=PaperGate(port=4002, env={"MODE": "paper"}))
    second.connect()
    second.instrument("EUR.USD", InstrumentKind.CASH)
    (seen,) = second.open_orders()
    assert (seen.client_order_id, seen.limit_price) == (resting.client_order_id, 108_400)
    assert seen.state is OrderState.WORKING
    second.cancel(resting.client_order_id)
    second.poll()
    clock.advance(10 * MS)
    assert brief(second.poll()) == [("order", "CANCELLED", 0)] and second.open_orders() == ()


# ---- the gateway's connection messages ------------------------------------------------------------------------------


def test_losing_the_brokers_side_of_the_gateway_is_a_lost_connection_though_the_socket_is_up():
    h, instrument = ready(backoff=Backoff())
    h.stub.say(1100, "Connectivity between IBKR and Trader Workstation has been lost.")
    assert h.venue.state is S.DISCONNECTED and h.stub.up is True
    assert "1100" in of(h.venue.poll(), ConnectionEvent)[-1].reason
    with pytest.raises(NotReady):
        h.venue.place(an_order(), instrument)
    assert states(h.after(2 * SECOND)) == ["CONNECTING", "DISCONNECTED"], "still blind: the retry fails"
    assert len([c for c in h.stub.calls if c.startswith("handshake")]) == 1, "the socket was never dropped"


@pytest.mark.parametrize("code", [1101, 1102])
def test_when_the_gateway_is_back_the_lock_is_passed_again_and_the_caller_reconciles(code):
    h, instrument = ready(backoff=Backoff())
    h.stub.say(1100, "lost")
    h.venue.poll()
    h.stub.say(code, "restored")
    events = h.venue.poll()
    assert states(events) == ["CONNECTING", "RECONCILING"] and of(events, VenueNotice)[0].code == f"ib:{code}"
    with pytest.raises(NotReady, match="RECONCILING"):
        h.venue.place(an_order(), instrument)
    h.venue.mark_reconciled()
    h.venue.place(an_order(), instrument)
    assert len([c for c in h.stub.calls if c.startswith("handshake")]) == 1


def test_a_gateway_that_comes_back_with_a_live_account_is_refused():
    h, instrument = ready(backoff=Backoff())
    h.stub.say(1100, "lost")
    h.market.config = dataclasses.replace(h.market.config, accounts=("U7654321",))
    h.stub.say(1102, "restored")
    assert states(h.venue.poll())[-1] == "REFUSED" and h.stub.up is False
    with pytest.raises(PaperOnlyError):
        h.venue.place(an_order(), instrument)


def test_quotes_resume_after_the_gateway_is_back_even_if_the_book_did_not_move():
    h, _ = ready(backoff=Backoff())
    h.market.script_quote(T0 + MS, "EUR.USD", BID, ASK)
    assert len(of(h.after(2 * MS), Quote)) == 1
    h.stub.say(1100, "lost")
    h.stub.say(1101, "restored, data lost")
    h.venue.poll()
    h.market.script_quote(T0 + 5 * MS, "EUR.USD", BID, ASK)
    assert len(of(h.after(5 * MS), Quote)) == 1
    assert h.stub.calls.count("subscribe_ticker") == 2, "the subscription was made again"


def test_other_gateway_messages_are_notices_with_their_code():
    h, instrument = ready()
    h.stub.say(2104, "Market data farm connection is OK:usfarm")
    h.venue.place(an_order(), instrument)
    h.stub.say(399, "Order message: warning", request=1)
    notices = of(h.venue.poll(), VenueNotice)
    assert [(n.code, n.client_order_id) for n in notices] == [("ib:2104", None), ("ib:399", an_order().client_order_id)]


def test_a_closed_socket_is_noticed_and_reconnected_on_schedule():
    h, _ = ready(backoff=Backoff())
    h.stub.close()  # as when the gateway restarts
    assert h.venue.state is S.DISCONNECTED
    assert "closed the connection" in of(h.venue.poll(), ConnectionEvent)[-1].reason
    assert states(h.after(1 * SECOND)) == ["CONNECTING", "RECONCILING"]
    assert len([c for c in h.stub.calls if c.startswith("handshake")]) == 2


@pytest.mark.parametrize(
    "failure", [ConnectionError("Not connected"), TimeoutError(), ConnectionResetError(), OSError(5)]
)
def test_a_request_the_gateway_does_not_answer_is_unavailable_not_a_crash(failure):
    h, _ = ready()

    def broken():
        raise failure

    h.stub.positions = broken
    with pytest.raises(VenueUnavailable, match="did not answer"):
        h.venue.positions()


def test_a_gateway_that_is_not_listening_is_unavailable():
    h = Harness("ibkr", FakeConfig(outages=((T0, T0 + SECOND),)))
    with pytest.raises(VenueUnavailable, match="ConnectionRefusedError"):
        h.venue.connect()
    assert h.venue.state is S.DISCONNECTED


# ---- order status and executions, handed over by the library --------------------------------------------------------


def a_trade(reference: str, status: str = "Submitted", **fields) -> Trade:
    contract = Contract(secType="CASH", conId=1000, symbol="EUR", currency="USD", exchange="IDEALPRO")
    order = IbOrder(orderId=5, permId=991, action="BUY", totalQuantity=20000.0, orderType="MKT", orderRef=reference)
    return Trade(contract, order, OrderStatus(orderId=5, status=status, **fields), [], [TradeLogEntry(NOW, status)])


def test_a_new_order_refused_by_the_broker_is_rejected_with_its_message():
    h, _ = ready()
    ours = an_order().client_order_id
    trade = a_trade(ours, "Cancelled")
    trade.log.append(TradeLogEntry(NOW, "Cancelled", "Error 201, reqId 5: Order rejected - reason:margin", 201))
    h.venue._on_order_status(trade)
    (event,) = of(h.venue.poll(), OrderEvent)
    assert (event.state, event.broker_order_id) == (OrderState.REJECTED, "991") and "reason:margin" in event.reason


def test_an_acknowledged_order_that_the_broker_cancels_is_cancelled_not_rejected():
    h, _ = ready()
    ours = an_order().client_order_id
    h.venue._on_order_status(a_trade(ours, "Submitted"))
    cancelled = a_trade(ours, "Cancelled")
    cancelled.log.append(TradeLogEntry(NOW, "Cancelled", "Error 201: margin call", 201))
    h.venue._on_order_status(cancelled)
    assert [e.state for e in of(h.venue.poll(), OrderEvent)] == [OrderState.WORKING, OrderState.CANCELLED]


def test_statuses_that_change_nothing_and_repeats_are_not_reported():
    h, _ = ready()
    ours = an_order().client_order_id
    for status in ("PendingSubmit", "PreSubmitted", "Submitted", "Submitted", "PendingCancel", "SomethingNew"):
        h.venue._on_order_status(a_trade(ours, status))
    h.venue._on_order_status(a_trade(ours, "Submitted", filled=5000.0, remaining=15000.0))
    h.venue._on_order_status(a_trade(ours, "Inactive"))
    assert [(e.state.name, e.filled_quantity) for e in of(h.venue.poll(), OrderEvent)] == [
        ("WORKING", 0), ("PARTIALLY_FILLED", 5_000), ("REJECTED", 0),
    ]  # fmt: skip


@pytest.mark.parametrize("reference", ["", "manual", "wt-b-20261009-0123456789ab"])
def test_orders_and_executions_this_platform_did_not_send_are_not_its_events(reference):
    h, _ = ready()
    trade = a_trade(reference, "Filled", filled=20000.0)
    execution = Execution(execId="x.1", time=NOW, side="BOT", shares=20000.0, price=1.0851, orderRef=reference)
    fill = IbFill(trade.contract, execution, CommissionReport(execId="x.1", commission=2.0, currency="USD"), NOW)
    h.venue._on_order_status(trade)
    h.venue._on_execution(trade, fill)
    h.venue._on_commission(trade, fill, fill.commissionReport)
    assert h.venue.poll() == []


def test_what_cannot_be_translated_exactly_becomes_a_notice_never_a_rounded_event():
    h, _ = ready()
    ours = an_order().client_order_id
    trade = a_trade(ours, "Filled", filled=20000.0)
    off_grid = Execution(execId="x.1", time=NOW, side="BOT", shares=20000.0, price=1.085101, orderRef=ours)
    h.venue._on_execution(trade, IbFill(trade.contract, off_grid, CommissionReport(), NOW))
    h.venue._on_order_status(a_trade(ours, "Submitted", filled=0.5))
    elsewhere = Contract(secType="CASH", conId=4242, symbol="GBP", currency="USD", exchange="IDEALPRO")
    stray = Execution(execId="x.2", time=NOW, side="BOT", shares=1000.0, price=1.3, orderRef=ours)
    h.venue._on_execution(trade, IbFill(elsewhere, stray, CommissionReport(), NOW))
    events = h.venue.poll()
    assert [type(e) for e in events] == [VenueNotice] * 3
    assert [e.code for e in events] == ["translation", "translation", "unknown-instrument"]
    assert all(e.client_order_id == ours for e in events)


def test_a_quote_off_the_price_grid_is_a_notice_and_the_next_good_one_still_arrives():
    h, _ = ready()
    ticker = next(iter(h.stub._tickers.values()))
    ticker.bid, ticker.ask, ticker.bidSize, ticker.askSize = 1.085001, 1.0851, 1e6, 1e6
    h.venue._on_tickers({ticker})
    ticker.bid = 1.08505
    h.venue._on_tickers({ticker})
    events = h.venue.poll()
    assert [type(e) for e in events] == [VenueNotice, Quote] and events[1].bid == 108_505


def test_an_execution_is_matched_to_its_instrument_even_if_the_contract_id_differs():
    h, _ = ready()
    ours = an_order().client_order_id
    trade = a_trade(ours, "Filled", filled=20000.0)
    other_id = Contract(secType="CASH", conId=999_999, symbol="EUR", currency="USD", exchange="IDEALPRO")
    execution = Execution(execId="x.9", time=NOW, side="SLD", shares=20000.0, price=1.085, orderRef=ours)
    execution.lastLiquidity = 2
    h.venue._on_execution(trade, IbFill(other_id, execution, CommissionReport(), NOW))
    assert brief(h.venue.poll()) == [("fill", "SELL", 20_000, 108_500, "TAKER")]


def test_working_orders_on_instruments_never_looked_up_are_flagged_not_hidden():
    h, instrument = ready()
    h.venue.place(an_order(limit=108_400), instrument)
    h.after(30 * MS)
    h.venue._contracts.clear()
    h.venue._by_contract.clear()
    assert h.venue.open_orders() == ()
    assert [n.code for n in of(h.venue.poll(), VenueNotice)] == ["foreign-order"]


# ---- the structure --------------------------------------------------------------------------------------------------


def calls_in(tree: ast.AST, names: set[str]) -> list[tuple[str, str]]:
    """(enclosing function, called name) for each call to one of `names`."""
    found = []
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and called_name(node) in names:
                    found.append((function.name, called_name(node)))
    return found


def test_the_library_is_asked_to_place_or_cancel_from_exactly_one_place_each():
    entry = {"place_order", "cancel_order", "placeOrder", "cancelOrder", "reqGlobalCancel"}
    by_file = {rel: calls_in(tree, entry) for rel, tree in sources()}
    assert by_file.pop(ADAPTER) == [("_send", "place_order"), ("_cancel", "cancel_order")]
    assert by_file.pop(CLIENT) == [("place_order", "placeOrder"), ("cancel_order", "cancelOrder")]
    assert {rel: found for rel, found in by_file.items() if found} == {}


def test_the_library_is_connected_from_exactly_one_place():
    for rel, tree in sources():
        made = calls_in(tree, {"IB", "connect", "connectAsync", "handshake"})
        if rel == CLIENT:
            assert made == [("__init__", "IB"), ("handshake", "connectAsync")]
        elif rel == ADAPTER:
            assert made == [("_open", "handshake")]
        elif rel != "src/hftlab/venues/ibkr/probe.py":
            assert [m for m in made if m[1] != "connect"] == [], rel


# ---- the real library, without a gateway ----------------------------------------------------------------------------


@pytest.fixture
def ib():
    """A real `IB` from the library, never connected. The event loop its client makes is closed afterwards."""
    yield IB()
    loop = asyncio.get_event_loop_policy().get_event_loop()
    loop.close()
    asyncio.set_event_loop(None)


def test_the_real_client_wires_the_librarys_events_to_the_adapter(ib):
    client = IbAsyncClient(ib, request_timeout_s=3.0)
    venue = IbkrVenue(clock=WallClock(), client=client, gate=PaperGate(port=4002, env={"MODE": "live"}))
    assert ib.RequestTimeout == 3.0 and client.connected() is False
    for event in (ib.pendingTickersEvent, ib.orderStatusEvent, ib.execDetailsEvent, ib.commissionReportEvent,
                  ib.errorEvent, ib.disconnectedEvent):  # fmt: skip
        assert len(event) >= 1
    ib.errorEvent.emit(-1, 2104, "Market data farm connection is OK:usfarm", None)
    ours = an_order().client_order_id
    ib.orderStatusEvent.emit(a_trade(ours, "Submitted"))
    events = venue.poll()
    assert [type(e) for e in events] == [VenueNotice, OrderEvent] and events[0].code == "ib:2104"
    assert events[1].state is OrderState.WORKING and events[1].ts_ns > 1_700_000_000 * SECOND


def test_the_real_client_never_dials_when_the_lock_refuses_and_fails_cleanly_when_not_connected(ib):
    client = IbAsyncClient(ib)
    gate = PaperGate(port=4001, env={"MODE": "paper"})
    venue = IbkrVenue(clock=WallClock(), client=client, settings=IbkrSettings(port=4001), gate=gate)
    with pytest.raises(PaperOnlyError, match="live trading port"):
        venue.connect()
    assert venue.state is S.REFUSED and ib.client.isConnected() is False
    for request in (client.accounts, client.positions, client.open_trades, client.executions, client.server_time):
        with pytest.raises(ConnectionError):
            request()
    client.close()
