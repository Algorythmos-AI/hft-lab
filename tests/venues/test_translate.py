"""Translation between the broker library's objects and core types: exact both ways, or an error."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ib_async import CommissionReport, Contract, ContractDetails, Execution, Forex, OrderStatus, Ticker, Trade
from ib_async import Order as IbOrder
from ib_async import Position as IbPosition
from ib_async.objects import DOMLevel, HistoricalTickBidAsk, TickAttribBidAsk

from hftlab.core import (
    InstrumentKind,
    Liquidity,
    Order,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    client_order_id,
    from_scaled,
    is_ours,
)
from hftlab.venues.ibkr import translate as tr
from hftlab.venues.ibkr.translate import TranslationError
from tests.venues.kit import EURUSD, EURUSD_CFD, USDJPY

NAN, INF = float("nan"), float("inf")
UNSET = 1.7976931348623157e308
OURS = client_order_id(sleeve="control", pair="EUR.USD", side=Side.BUY, decision_seq=1)


def decoded(value: int, scale: int) -> float:
    """What the library gives for a value the gateway sent as exact decimal text."""
    return float(from_scaled(value, scale))


def forex(con_id: int = 12087792) -> Contract:
    return Contract(secType="CASH", conId=con_id, symbol="EUR", currency="USD", exchange="IDEALPRO")


def a_ticker(**fields) -> Ticker:
    """A ticker with these fields. The library blanks a new ticker's prices, so they are set afterwards."""
    ticker = Ticker()
    for name, value in fields.items():
        setattr(ticker, name, value)
    return ticker


# ---- numbers --------------------------------------------------------------------------------------------------------


@given(st.integers(-(10**15) + 1, 10**15 - 1), st.integers(0, 9))
def test_a_value_sent_as_decimal_text_and_decoded_to_a_float_comes_back_exactly(value, scale):
    assert tr.exact(decoded(value, scale), scale, "value") == value


@pytest.mark.parametrize(
    ("value", "scale", "expected"),
    [(1.08512, 5, 108_512), (151.234, 3, 151_234), (0.1 + 0.2, 17, None), (1.085125, 5, None), (5e-05, 5, 5),
     (1e16, 0, 10**16), (25000.0, 0, 25_000), (25000, 0, 25_000), (0.30000000000000004, 5, None), (2.5, 0, None)],
)  # fmt: skip
def test_exact_examples(value, scale, expected):
    if expected is None:
        with pytest.raises((TranslationError, ValueError)):
            tr.exact(value, min(scale, 9), "value")
    else:
        assert tr.exact(value, scale, "value") == expected


@pytest.mark.parametrize("value", [NAN, INF, -INF, None, "1.0", True, Decimal("1.0"), [1.0]])
def test_what_is_not_a_finite_number_is_an_error_not_a_guess(value):
    with pytest.raises(TranslationError):
        tr.exact(value, 5, "value")


@pytest.mark.parametrize(
    ("tick", "expected"),
    [(5e-05, (5, 5)), (0.00005, (5, 5)), (0.0001, (4, 1)), (0.005, (3, 5)), (0.001, (3, 1)), (0.01, (2, 1)),
     (1.0, (0, 1)), (0.25, (2, 25)), (5, (0, 5)), (1e-09, (9, 1))],
)  # fmt: skip
def test_a_price_step_gives_the_scale_and_the_increment(tick, expected):
    assert tr.increment_of(tick, "minTick") == expected


@pytest.mark.parametrize("tick", [0.0, -0.0001, NAN, INF, None, "0.0001", True, 1e-10])
def test_a_price_step_that_is_missing_or_too_fine_is_an_error(tick):
    with pytest.raises(TranslationError):
        tr.increment_of(tick, "minTick")


@given(st.integers(1, 10**6), st.integers(0, 9))
def test_any_increment_survives_the_trip_through_a_float(increment, scale):
    found_scale, found = tr.increment_of(decoded(increment, scale), "minTick")
    assert found_scale <= scale and found * 10 ** (scale - found_scale) == increment


@pytest.mark.parametrize("value", [NAN, INF, -1.0, 0.0, -1, UNSET, None, True, "1.1"])
def test_the_gateways_ways_of_saying_no_value_are_all_absent(value):
    assert tr.present(value) is False
    assert tr.present(1.08512) and tr.present(1)


# ---- time -----------------------------------------------------------------------------------------------------------


@given(st.integers(0, 4_000_000_000), st.integers(0, 999_999))
def test_a_zoned_datetime_becomes_exact_utc_nanoseconds(seconds, micros):
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds, microseconds=micros)
    assert tr.ns_of(moment) == seconds * 10**9 + micros * 1_000
    assert tr.ns_of(moment.astimezone(timezone(timedelta(hours=11)))) == seconds * 10**9 + micros * 1_000


def test_a_datetime_without_a_zone_cannot_be_placed():
    assert tr.ns_of(datetime(2026, 10, 10, 12, 0, 0)) is None
    assert tr.ns_of(None) is None and tr.ns_of("20261010 12:00:00") is None
    assert tr.ns_of(datetime(1969, 12, 31, tzinfo=UTC)) is None


@given(st.integers(0, 4_000_000_000))
def test_whole_seconds_round_trip_through_the_request_form(seconds):
    assert tr.ns_of(tr.utc(seconds * 10**9 + 123_456_789)) == seconds * 10**9
    assert tr.utc(seconds * 10**9).tzinfo is UTC


# ---- instruments ----------------------------------------------------------------------------------------------------


def test_the_contract_asked_for_is_spot_on_idealpro_or_a_cfd_on_smart():
    spot = tr.contract_for("EUR.USD", InstrumentKind.CASH)
    assert (spot.secType, spot.symbol, spot.currency, spot.exchange) == ("CASH", "EUR", "USD", "IDEALPRO")
    assert isinstance(spot, Forex)
    cfd = tr.contract_for("USD.JPY", InstrumentKind.CFD)
    assert (cfd.secType, cfd.symbol, cfd.currency, cfd.exchange) == ("CFD", "USD", "JPY", "SMART")


def details(contract=None, **changes) -> ContractDetails:
    values = {"contract": contract or forex(), "minTick": 5e-05, "minSize": 20000.0, "sizeIncrement": 1000.0, **changes}
    return ContractDetails(**values)


def test_an_instrument_is_built_from_what_the_gateway_reports():
    assert tr.instrument_from(details(), "EUR.USD", InstrumentKind.CASH) == EURUSD
    yen = Contract(secType="CASH", conId=5, symbol="USD", currency="JPY", exchange="IDEALPRO")
    assert tr.instrument_from(details(yen, minTick=0.005), "USD.JPY", InstrumentKind.CASH) == USDJPY
    cfd = Contract(secType="CFD", conId=6, symbol="EUR", currency="USD", exchange="SMART")
    built = tr.instrument_from(details(cfd, minSize=1000.0, sizeIncrement=1.0), "EUR.USD", InstrumentKind.CFD)
    assert built == EURUSD_CFD


@pytest.mark.parametrize(
    "changes",
    [{"minTick": 0.0}, {"minTick": NAN}, {"minSize": 0.0}, {"sizeIncrement": 0.0}, {"minSize": 0.5},
     {"sizeIncrement": 0.01}, {"minSize": NAN}],
)  # fmt: skip
def test_a_gateway_answer_that_leaves_a_number_out_is_refused_not_defaulted(changes):
    with pytest.raises(TranslationError):
        tr.instrument_from(details(**changes), "EUR.USD", InstrumentKind.CASH)
    with pytest.raises(TranslationError):
        nameless = ContractDetails(minTick=5e-05, minSize=1.0, sizeIncrement=1.0)
        tr.instrument_from(nameless, "EUR.USD", InstrumentKind.CASH)


def test_an_answer_for_another_contract_is_refused():
    with pytest.raises(TranslationError):
        tr.instrument_from(details(), "USD.JPY", InstrumentKind.CASH)
    with pytest.raises(TranslationError):
        tr.instrument_from(details(), "EUR.USD", InstrumentKind.CFD)


@pytest.mark.parametrize(
    ("contract", "pair"),
    [
        (forex(), "EUR.USD"),
        (Contract(secType="CFD", symbol="USD", currency="JPY"), "USD.JPY"),
        (Contract(secType="STK", symbol="IBM", currency="USD"), None),
        (Contract(secType="CASH", symbol="EURO", currency="USD"), None),
        (Contract(secType="CASH", symbol="eur", currency="USD"), None),
        (Contract(secType="CASH", symbol="EUR", currency=""), None),
        (None, None),
    ],
)
def test_only_a_forex_contract_stands_for_a_pair(contract, pair):
    assert tr.pair_of(contract) == pair


# ---- quotes ---------------------------------------------------------------------------------------------------------

prices = st.integers(1, 10**9)
sizes = st.integers(0, 10**10)


@given(prices, prices, sizes, sizes)
def test_a_quote_loses_nothing_on_its_way_through_the_library(bid, ask, bid_size, ask_size):
    for instrument in (EURUSD, USDJPY):
        scale = instrument.price_scale
        sent = {"bid": decoded(bid, scale), "ask": decoded(ask, scale)}
        sent |= {"bidSize": float(bid_size), "askSize": float(ask_size)}
        assert tr.quote_from(a_ticker(**sent), instrument) == (bid, ask, bid_size, ask_size)


@pytest.mark.parametrize(
    "changes", [{"bid": NAN}, {"ask": NAN}, {"bid": -1.0}, {"ask": 0.0}, {"bid": UNSET}, {"bid": NAN, "ask": NAN}]
)
def test_a_one_sided_or_empty_book_is_not_a_quote(changes):
    ticker = a_ticker(**{"bid": 1.085, "ask": 1.0851, "bidSize": 1e6, "askSize": 1e6, **changes})
    assert tr.quote_from(ticker, EURUSD) is None


def test_a_new_ticker_from_the_library_has_no_quote_whatever_it_was_built_with():
    assert tr.quote_from(Ticker(bid=1.085, ask=1.0851), EURUSD) is None  # the library blanks them on creation
    assert tr.quote_from(Ticker(), EURUSD) is None


def test_a_missing_size_is_zero_and_a_price_off_the_grid_is_an_error():
    assert tr.quote_from(a_ticker(bid=1.085, ask=1.0851), EURUSD) == (108_500, 108_510, 0, 0)
    assert tr.quote_from(a_ticker(bid=1.085, ask=1.0851, bidSize=0, askSize=-1), EURUSD) == (108_500, 108_510, 0, 0)
    with pytest.raises(TranslationError, match=r"EUR\.USD bid"):
        tr.quote_from(a_ticker(bid=1.085001, ask=1.0851), EURUSD)
    with pytest.raises(TranslationError, match="bid size"):
        tr.quote_from(a_ticker(bid=1.085, ask=1.0851, bidSize=1000.5, askSize=1.0), EURUSD)


def test_depth_is_levels_of_exact_prices_and_sizes():
    ticker = Ticker()
    ticker.domBids = [DOMLevel(1.085, 1e6, ""), DOMLevel(1.08495, 2e6, "")]
    ticker.domAsks = [DOMLevel(1.0851, 5e5, ""), DOMLevel(NAN, 1.0, "")]
    assert tr.depth_from(ticker, EURUSD) == (((108_500, 1_000_000), (108_495, 2_000_000)), ((108_510, 500_000),))


def test_a_historical_tick_becomes_a_quote_dated_by_the_venue():
    at = datetime(2026, 10, 9, 12, 0, 5, tzinfo=UTC)
    tick = HistoricalTickBidAsk(at, TickAttribBidAsk(), 1.085, 1.0851, 1e6, 2e6)
    quote = tr.history_quote(tick, EURUSD, 3)
    assert (quote.bid, quote.ask, quote.bid_size, quote.ask_size) == (108_500, 108_510, 1_000_000, 2_000_000)
    assert quote.recv_ts_ns == tr.ns_of(at) and quote.seq == 3
    undated = HistoricalTickBidAsk(at.replace(tzinfo=None), TickAttribBidAsk(), 1.085, 1.0851, 1.0, 1.0)
    one_sided = HistoricalTickBidAsk(at, TickAttribBidAsk(), NAN, 1.0851, 1.0, 1.0)
    assert tr.history_quote(undated, EURUSD, 0) is None and tr.history_quote(one_sided, EURUSD, 0) is None


# ---- orders ---------------------------------------------------------------------------------------------------------

def an_order(number, side, lots, limit, tif) -> Order:
    order_id = client_order_id(sleeve="control", pair="EUR.USD", side=side, decision_seq=number)
    kind = OrderType.MARKET if limit is None else OrderType.LIMIT
    return Order(order_id, "control", "EUR.USD", side, lots * 1_000, kind, tif, 1, None if limit is None else limit * 5)


orders = st.builds(
    an_order,
    st.integers(0, 10**9),
    st.sampled_from(Side),
    st.integers(20, 10**6),
    st.none() | st.integers(1, 10**8),
    st.sampled_from(TimeInForce),
)


@given(orders)
def test_an_order_loses_nothing_on_its_way_to_the_broker_and_back(order):
    sent = tr.to_ib_order(order, EURUSD, "DU1234567")
    assert tr.order_terms(sent, EURUSD) == (order.side, order.quantity, order.order_type, order.limit_price, order.tif)
    assert sent.orderRef == order.client_order_id and is_ours(sent.orderRef) and sent.account == "DU1234567"
    assert type(sent.totalQuantity) is int and sent.transmit is True
    assert (order.limit_price is None) or isinstance(sent.lmtPrice, Decimal), "no float leaves this platform"


@given(orders, orders)
def test_the_client_order_id_maps_one_to_one_onto_the_order_reference(first, second):
    refs = tr.to_ib_order(first, EURUSD, "DU1").orderRef, tr.to_ib_order(second, EURUSD, "DU1").orderRef
    assert (refs[0] == refs[1]) == (first.client_order_id == second.client_order_id)


def test_the_limit_price_is_sent_as_exact_decimal_text():
    order = Order(OURS, "control", "EUR.USD", Side.BUY, 20_000, OrderType.LIMIT, TimeInForce.DAY, 1, 108_500)
    sent = tr.to_ib_order(order, EURUSD, "DU1")
    assert (sent.action, sent.orderType, sent.tif, str(sent.lmtPrice)) == ("BUY", "LMT", "DAY", "1.08500")


def test_an_order_decoded_from_the_wire_with_floats_is_read_exactly():
    wire = IbOrder(action="SELL", totalQuantity=25000.0, orderType="LMT", lmtPrice=1.08515, tif="GTC")
    assert tr.order_terms(wire, EURUSD) == (Side.SELL, 25_000, OrderType.LIMIT, 108_515, TimeInForce.GTC)
    market = IbOrder(action="BUY", totalQuantity=1000.0, orderType="MKT")
    assert tr.order_terms(market, EURUSD)[3:] == (None, TimeInForce.DAY)


@pytest.mark.parametrize(
    "wire",
    [
        IbOrder(action="SSHORT", totalQuantity=1.0, orderType="MKT"),
        IbOrder(action="BUY", totalQuantity=1.0, orderType="STP"),
        IbOrder(action="BUY", totalQuantity=1.0, orderType="MKT", tif="OPG"),
        IbOrder(action="BUY", totalQuantity=1.5, orderType="MKT"),
        IbOrder(action="BUY", totalQuantity=1.0, orderType="LMT", lmtPrice=1.085001),
    ],
)
def test_an_order_this_platform_could_not_have_sent_is_an_error(wire):
    with pytest.raises(TranslationError):
        tr.order_terms(wire, EURUSD)


@pytest.mark.parametrize(
    ("status", "filled", "acknowledged", "code", "expected"),
    [
        ("PendingSubmit", 0, False, 0, OrderState.SUBMITTED),
        ("ApiPending", 0, False, 0, OrderState.SUBMITTED),
        ("PreSubmitted", 0, False, 0, OrderState.WORKING),
        ("Submitted", 0, False, 0, OrderState.WORKING),
        ("Submitted", 5_000, True, 0, OrderState.PARTIALLY_FILLED),
        ("ValidationError", 0, True, 399, OrderState.WORKING),
        ("Filled", 20_000, True, 0, OrderState.FILLED),
        ("Cancelled", 0, True, 202, OrderState.CANCELLED),
        ("Cancelled", 0, False, 202, OrderState.CANCELLED),
        ("Cancelled", 0, False, 0, OrderState.CANCELLED),
        ("ApiCancelled", 0, False, 0, OrderState.CANCELLED),
        ("Cancelled", 0, False, 201, OrderState.REJECTED),
        ("Cancelled", 0, False, 110, OrderState.REJECTED),
        ("Cancelled", 0, True, 201, OrderState.CANCELLED),
        ("Cancelled", 5_000, False, 201, OrderState.CANCELLED),
        ("Inactive", 0, False, 0, OrderState.REJECTED),
        ("PendingCancel", 0, True, 0, None),
        ("SomethingNew", 0, True, 0, None),
    ],
)
def test_each_broker_status_has_one_core_state(status, filled, acknowledged, code, expected):
    assert tr.state_from(status, filled, acknowledged=acknowledged, error_code=code) is expected


def a_trade(reference=OURS, **status) -> Trade:
    order = IbOrder(orderId=7, permId=991, action="BUY", totalQuantity=40000.0, orderType="LMT", lmtPrice=1.085)
    order.tif, order.orderRef = "DAY", reference
    state = {"status": "Submitted", "filled": 10000.0, "remaining": 30000.0, **status}
    return Trade(forex(), order, OrderStatus(**state))


def test_a_working_order_is_described_in_core_terms():
    found = tr.open_order_from(a_trade(), EURUSD)
    assert (found.client_order_id, found.broker_order_id, found.pair) == (OURS, "991", "EUR.USD")
    assert (found.quantity, found.filled_quantity, found.limit_price) == (40_000, 10_000, 108_500)
    assert (found.side, found.order_type, found.tif, found.state) == (
        Side.BUY, OrderType.LIMIT, TimeInForce.DAY, OrderState.PARTIALLY_FILLED,
    )  # fmt: skip


@pytest.mark.parametrize("reference", ["", "manual order", "wt-b-20261009-0123456789ab", "hft-control-short"])
def test_an_order_this_platform_did_not_send_has_no_client_order_id(reference):
    assert tr.open_order_from(a_trade(reference), EURUSD).client_order_id is None


# ---- executions -----------------------------------------------------------------------------------------------------


def an_execution(**changes) -> Execution:
    values = {"execId": "0000e0d5.1", "time": datetime(2026, 10, 9, 12, 0, 5, tzinfo=UTC), "side": "BOT"}
    values |= {"shares": 20000.0, "price": 1.0851, "orderRef": OURS, "lastLiquidity": 2, **changes}
    return Execution(**values)


def test_an_execution_becomes_the_fields_of_a_fill():
    terms = tr.execution_terms(an_execution(), EURUSD)
    assert (terms.fill_id, terms.client_order_id, terms.pair) == ("0000e0d5.1", OURS, "EUR.USD")
    assert (terms.side, terms.quantity, terms.price, terms.liquidity) == (Side.BUY, 20_000, 108_510, Liquidity.TAKER)
    assert terms.venue_ts_ns == 1_791_547_205 * 10**9
    sold = tr.execution_terms(an_execution(side="SLD", lastLiquidity=1, time=datetime(2026, 10, 9, 12, 0, 5)), EURUSD)
    assert (sold.side, sold.liquidity, sold.venue_ts_ns) == (Side.SELL, Liquidity.MAKER, None)
    assert tr.execution_terms(an_execution(lastLiquidity=3), EURUSD).liquidity is Liquidity.UNKNOWN


@given(st.sampled_from(Side), st.integers(1, 10**9), prices)
def test_an_execution_loses_nothing_on_its_way_through_the_library(side, quantity, price):
    report = an_execution(side="BOT" if side is Side.BUY else "SLD", shares=float(quantity), price=decoded(price, 5))
    terms = tr.execution_terms(report, EURUSD)
    assert (terms.side, terms.quantity, terms.price) == (side, quantity, price)


@pytest.mark.parametrize(
    "changes", [{"side": "BUY"}, {"side": ""}, {"execId": ""}, {"shares": 0.5}, {"price": 1.085101}, {"price": NAN}]
)
def test_an_execution_that_cannot_be_booked_exactly_is_an_error(changes):
    with pytest.raises(TranslationError):
        tr.execution_terms(an_execution(**changes), EURUSD)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(2.0, 2_000_000), (0.434048, 434_048), (1.7361925, 1_736_192), (1.7361935, 1_736_194), (0.0, 0),
     (-0.25, -250_000), (2.1702400000000002, 2_170_240)],
)  # fmt: skip
def test_a_commission_is_rounded_half_even_to_a_millionth(amount, expected):
    report = CommissionReport(execId="x.1", commission=amount, currency="USD")
    assert tr.commission_from(report) == ("x.1", expected, "USD")


@pytest.mark.parametrize(
    "report",
    [
        CommissionReport(),
        CommissionReport(execId="x.1", commission=2.0),
        CommissionReport(commission=2.0, currency="USD"),
        CommissionReport(execId="x.1", commission=UNSET, currency="USD"),
        CommissionReport(execId="x.1", commission=NAN, currency="USD"),
    ],
)
def test_an_empty_commission_report_is_no_commission_yet(report):
    assert tr.commission_from(report) is None


def test_a_position_is_whole_base_units_of_a_forex_contract():
    assert tr.position_from(IbPosition("DU1", forex(), -25000.0, 1.085)).quantity == -25_000
    assert tr.position_from(IbPosition("DU1", forex(), 0.0, 0.0)) is None
    assert tr.position_from(IbPosition("DU1", Contract(secType="STK", symbol="IBM", currency="USD"), 10.0, 1.0)) is None
    cfd = Contract(secType="CFD", symbol="EUR", currency="USD")
    assert tr.position_from(IbPosition("DU1", cfd, 1000.0, 1.0)).kind is InstrumentKind.CFD
    with pytest.raises(TranslationError):
        tr.position_from(IbPosition("DU1", forex(), 0.5, 1.0))
