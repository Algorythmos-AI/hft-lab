"""A stand-in for the broker library's client, built from the library's own objects, over the fake market.

It plays the gateway: the same market that sits under the fake broker decides what happens, and this turns
each happening into what `ib_async` would hand the adapter (a `Ticker` with float prices, a `Trade` whose
status and log change, an `Execution`, a `CommissionReport`). Because both venues then see the same market,
the contract tests can demand the same port events from both.

It also checks what the adapter sends: no float in an order, and the client order id in `orderRef`.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ib_async import AccountValue, CommissionReport, Contract, ContractDetails, Execution, OrderStatus, Ticker, Trade
from ib_async import Fill as IbFill
from ib_async import Order as IbOrder
from ib_async import Position as IbPosition
from ib_async.objects import DOMLevel, HistoricalTickBidAsk, TickAttribBidAsk, TradeLogEntry

from hftlab.core import (
    Instrument,
    InstrumentKind,
    Liquidity,
    Order,
    OrderType,
    Side,
    SimClock,
    TimeInForce,
    from_scaled,
    sleeve_of,
    to_scaled,
)
from hftlab.venues.fake.market import (
    Acked,
    BookedOrder,
    Cancelled,
    CancelTooLate,
    Commissioned,
    Executed,
    FakeMarket,
    Happening,
    QuoteTick,
    Rejected,
    Said,
)
from hftlab.venues.ibkr.client import Handlers

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SEC_TYPE = {InstrumentKind.CASH: "CASH", InstrumentKind.CFD: "CFD"}
EXCHANGE = {InstrumentKind.CASH: "IDEALPRO", InstrumentKind.CFD: "SMART"}
TIF = {"DAY": TimeInForce.DAY, "GTC": TimeInForce.GTC, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK}
CLIENT_ID = 77


def moment(at_ns: int) -> datetime:
    return EPOCH + timedelta(microseconds=at_ns // 1_000)


def as_float(value: int, scale: int) -> float:
    """What the library would decode from the wire text of an exact value."""
    return float(from_scaled(value, scale))


class StubIb:
    def __init__(self, clock: SimClock, market: FakeMarket) -> None:
        self.clock, self.market = clock, market
        self.up = False
        self.calls: list[str] = []
        self.sent: list[IbOrder] = []
        self._handlers: Handlers | None = None
        self._pumped = clock.now_ns()
        self._contracts = {
            1000 + number: (instrument, self._contract(instrument, 1000 + number))
            for number, instrument in enumerate(market.instruments())
        }
        self._tickers: dict[int, Ticker] = {}
        self._depth: dict[int, int] = {}
        self._trades: dict[str, Trade] = {}
        self._fills: dict[str, IbFill] = {}
        self._orders = 0

    # ---- connection ---------------------------------------------------------------------------------------------

    def listen(self, handlers: Handlers) -> None:
        self._handlers = handlers

    def handshake(self, host: str, port: int, client_id: int, timeout_s: float) -> list[str]:
        self.calls.append(f"handshake {host}:{port} as {client_id}")
        now = self.clock.now_ns()
        if not self.market.link_up(now):
            raise ConnectionRefusedError("the gateway is not listening")
        self.market.advance(now)
        self._pumped = now
        self.up = True
        self._trades.clear()  # the library resets its state with the connection and learns orders afresh
        return list(self.market.config.accounts)

    def connected(self) -> bool:
        return self.up

    def accounts(self) -> list[str]:
        self._need_connection()
        return list(self.market.config.accounts)

    def close(self) -> None:
        self.calls.append("close")
        was_up, self.up = self.up, False
        if was_up and self._handlers:
            self._handlers.disconnected()

    def pump(self, timeout_s: float) -> None:
        now = self.clock.now_ns()
        if not self.up:
            return
        down_at = self.market.outage_start(self._pumped, now)
        for happening in self.market.advance(now):
            if down_at is None or happening.at_ns < down_at:
                self._deliver(happening)
        self._pumped = now
        if down_at is not None:
            self.up = False
            assert self._handlers
            self._handlers.disconnected()

    def _need_connection(self) -> None:
        if not self.up:
            raise ConnectionError("Not connected")

    def _ask(self, name: str) -> None:
        self.calls.append(name)
        self._need_connection()

    def say(self, code: int, message: str, request: int = -1) -> None:
        """The gateway sends an error or status message."""
        assert self._handlers
        self._handlers.error(request, code, message)

    # ---- contracts and market data ------------------------------------------------------------------------------

    @staticmethod
    def _contract(instrument: Instrument, con_id: int) -> Contract:
        return Contract(
            secType=SEC_TYPE[instrument.kind], conId=con_id, symbol=instrument.base, currency=instrument.quote,
            exchange=EXCHANGE[instrument.kind], localSymbol=instrument.pair,
        )  # fmt: skip

    def _known(self, contract: Contract) -> tuple[Instrument, Contract]:
        for instrument, qualified in self._contracts.values():
            same = (qualified.secType, qualified.symbol, qualified.currency) == (
                contract.secType, contract.symbol, contract.currency,
            )  # fmt: skip
            if qualified.conId == contract.conId or (not contract.conId and same):
                return instrument, qualified
        raise KeyError(contract)

    def contract_details(self, contract: Contract) -> list[ContractDetails]:
        self._ask("contract_details")
        try:
            instrument, qualified = self._known(contract)
        except KeyError:
            return []
        return [
            ContractDetails(
                contract=qualified,
                minTick=as_float(instrument.price_increment, instrument.price_scale),
                minSize=float(instrument.min_order_size),
                sizeIncrement=float(instrument.size_increment),
            )
        ]

    def subscribe_ticker(self, contract: Contract) -> None:
        self._ask("subscribe_ticker")
        self._tickers.setdefault(contract.conId, Ticker(contract=contract))

    def unsubscribe_ticker(self, contract: Contract) -> None:
        self._ask("unsubscribe_ticker")
        self._tickers.pop(contract.conId, None)

    def subscribe_depth(self, contract: Contract, rows: int) -> None:
        self._ask("subscribe_depth")
        self._depth[contract.conId] = rows

    def unsubscribe_depth(self, contract: Contract) -> None:
        self._ask("unsubscribe_depth")
        self._depth.pop(contract.conId, None)

    def historical_bid_ask(self, contract: Contract, end: datetime, count: int) -> list[HistoricalTickBidAsk]:
        self._ask("historical_bid_ask")
        instrument, _ = self._known(contract)
        end_ns = int((end - EPOCH).total_seconds()) * 1_000_000_000
        scale = instrument.price_scale
        out = []
        for tick in self.market.quotes_before(instrument.pair, end_ns, count):
            bid, ask = as_float(tick.bid, scale), as_float(tick.ask, scale)
            sizes = float(tick.bid_size), float(tick.ask_size)
            out.append(HistoricalTickBidAsk(moment(tick.at_ns), TickAttribBidAsk(), bid, ask, *sizes))
        return out

    def server_time(self) -> datetime:
        self._ask("server_time")
        return moment(self.clock.now_ns() + self.market.config.server_clock_offset_ns)

    # ---- orders -------------------------------------------------------------------------------------------------

    def place_order(self, contract: Contract, order: IbOrder) -> Trade:
        self._ask("place_order")
        instrument, qualified = self._known(contract)
        assert type(order.totalQuantity) is int, "a quantity must leave as an int"
        limit = None
        if order.orderType == "LMT":
            assert isinstance(order.lmtPrice, Decimal), "a limit price must leave as a Decimal, never a float"
            limit = to_scaled(order.lmtPrice, instrument.price_scale)
        self._orders += 1
        order.orderId, order.clientId = self._orders, CLIENT_ID
        kind = OrderType.LIMIT if order.orderType == "LMT" else OrderType.MARKET
        core = Order(
            order.orderRef, sleeve_of(order.orderRef) or "unknown", instrument.pair,
            Side.BUY if order.action == "BUY" else Side.SELL, order.totalQuantity,
            kind, TIF[order.tif], self.clock.now_ns(), limit,
        )  # fmt: skip
        self.market.submit(core, instrument, self.clock.now_ns())
        status = OrderStatus(orderId=order.orderId, status="PendingSubmit")
        trade = Trade(qualified, order, status, [], [TradeLogEntry(moment(self.clock.now_ns()), "PendingSubmit")])
        self._trades[order.orderRef] = trade
        self.sent.append(order)
        return trade

    def cancel_order(self, order: IbOrder) -> None:
        self._ask("cancel_order")
        self.market.request_cancel(order.orderRef, self.clock.now_ns())

    def _trade_of(self, booked: BookedOrder) -> Trade:
        """The trade for an order the broker holds; rebuilt as the library would decode it if this client lost it."""
        order = booked.order
        known = self._trades.get(order.client_order_id)
        if known is not None:
            return known
        _, contract = next(v for v in self._contracts.values() if v[0] == booked.instrument)
        ib_order = IbOrder(
            orderId=int(booked.broker_order_id[1:]), clientId=CLIENT_ID, permId=int(booked.broker_order_id[1:]),
            action=order.side.value, totalQuantity=float(order.quantity),
            orderType="LMT" if order.limit_price is not None else "MKT", tif=order.tif.value,
            orderRef=order.client_order_id,
        )  # fmt: skip
        if order.limit_price is not None:
            ib_order.lmtPrice = as_float(order.limit_price, booked.instrument.price_scale)
        status = OrderStatus(
            orderId=ib_order.orderId, status="Submitted" if booked.acked else "PreSubmitted",
            filled=float(booked.executed), remaining=float(booked.remaining), permId=ib_order.permId,
        )  # fmt: skip
        trade = Trade(contract, ib_order, status, [], [])
        self._trades[order.client_order_id] = trade
        return trade

    def forget_trades(self) -> None:
        """As after a restart of the process: the library no longer knows the orders it placed."""
        self._trades.clear()

    def open_trades(self) -> list[Trade]:
        self._ask("open_trades")
        self._settle()
        return [self._trade_of(booked) for booked in self.market.working()]

    def positions(self) -> list[IbPosition]:
        self._ask("positions")
        self._settle()
        account = self.market.config.accounts[0]
        out = []
        for (pair, kind), quantity in sorted(self.market.positions().items()):
            _, contract = next(v for v in self._contracts.values() if (v[0].pair, v[0].kind) == (pair, kind))
            out.append(IbPosition(account, contract, float(quantity), 0.0))
        return out

    def account_values(self, account: str) -> list[AccountValue]:
        self._ask("account_values")
        self._settle()
        held = [
            AccountValue(account, "$LEDGER-CashBalance", from_scaled(amount, 6), currency, "")
            for currency, amount in sorted(self.market.cash().items())
        ]
        base = AccountValue(account, "$LEDGER-CashBalance", "0", "BASE", "")
        others = [base, AccountValue(account, "Leverage", "1", "", "")]
        return [*held, *others]

    def executions(self) -> list[IbFill]:
        self._ask("executions")
        self._settle()
        out = []
        for executed in self.market.executions():
            report = self.market.commission_of(executed.exec_id)
            out.append(self._fill(executed, report))
        return out

    def _settle(self) -> None:
        if self.up:
            self.pump(0.0)

    # ---- what the gateway reports -------------------------------------------------------------------------------

    def _fill(self, executed: Executed, report: Commissioned | None) -> IbFill:
        booked = next(b for b in self.market._orders.values() if b.order.client_order_id == executed.client_order_id)
        _, contract = next(v for v in self._contracts.values() if v[0] == booked.instrument)
        permanent = int(executed.broker_order_id[1:])
        execution = Execution(
            execId=executed.exec_id, time=moment(executed.at_ns), acctNumber=self.market.config.accounts[0],
            exchange=contract.exchange, side="BOT" if executed.side is Side.BUY else "SLD",
            shares=float(executed.quantity), price=as_float(executed.price, booked.instrument.price_scale),
            permId=permanent, clientId=CLIENT_ID, orderId=permanent, cumQty=float(executed.cumulative),
            orderRef=executed.client_order_id, lastLiquidity=1 if executed.liquidity is Liquidity.MAKER else 2,
        )  # fmt: skip
        commission = CommissionReport()
        if report is not None:
            amount = as_float(report.commission, 6)
            commission = CommissionReport(execId=report.exec_id, commission=amount, currency=report.currency)
        return IbFill(contract, execution, commission, moment(executed.at_ns))

    def _deliver(self, happening: Happening) -> None:
        handlers = self._handlers
        assert handlers
        at = moment(happening.at_ns)
        if isinstance(happening, QuoteTick):
            self._deliver_quote(happening)
            return
        if isinstance(happening, Commissioned):
            executed = next(e for e in self.market.executions() if e.exec_id == happening.exec_id)
            fill = self._fill(executed, happening)
            trade = self._trade_of(self.market._orders[executed.client_order_id])
            handlers.commission(trade, fill, fill.commissionReport)
            return
        trade = self._trade_of(self.market._orders[happening.client_order_id])
        status = trade.orderStatus
        if isinstance(happening, Said):
            handlers.error(trade.order.orderId, happening.code, happening.text)
        elif isinstance(happening, Rejected) and happening.late:
            # After the acknowledgement. The real gateway's order then ended REJECTED through the adapter, which
            # only the status Inactive gives for an acknowledged order: inferred from that, not seen as a status.
            status.status = "Inactive"
            trade.log.append(TradeLogEntry(at, "Inactive", happening.reason, 201))
            handlers.order_status(trade)
        elif isinstance(happening, Acked):
            status.status, status.permId = "Submitted", int(happening.broker_order_id[1:])
            trade.order.permId = status.permId
            trade.log.append(TradeLogEntry(at, "Submitted"))
            handlers.order_status(trade)
        elif isinstance(happening, Rejected):
            status.status = "Cancelled"
            message = f"Error 201, reqId {trade.order.orderId}: Order rejected - reason:{happening.reason}"
            trade.log.append(TradeLogEntry(at, "Cancelled", message, 201))
            handlers.order_status(trade)
        elif isinstance(happening, Executed):
            fill = self._fill(happening, None)
            trade.fills.append(fill)
            handlers.execution(trade, fill)
            status.filled = float(happening.cumulative)
            status.remaining = float(happening.order_quantity - happening.cumulative)
            status.status = "Filled" if happening.cumulative == happening.order_quantity else "Submitted"
            trade.log.append(TradeLogEntry(at, status.status))
            handlers.order_status(trade)
        elif isinstance(happening, Cancelled):
            status.status = "Cancelled"
            trade.log.append(TradeLogEntry(at, "Cancelled", happening.reason, 202))
            handlers.order_status(trade)
        elif isinstance(happening, CancelTooLate):
            message = f"OrderId {trade.order.orderId} that needs to be cancelled cannot be cancelled, state: Filled."
            handlers.error(trade.order.orderId, 10148, message)

    def _deliver_quote(self, tick: QuoteTick) -> None:
        assert self._handlers
        for con_id, (instrument, _) in self._contracts.items():
            if instrument.pair != tick.pair:
                continue
            scale = instrument.price_scale
            ticker = self._tickers.get(con_id)
            rows = self._depth.get(con_id)
            if ticker is None and rows is None:
                continue
            ticker = ticker or Ticker(contract=self._contracts[con_id][1])
            if con_id in self._tickers:
                ticker.bid, ticker.ask = as_float(tick.bid, scale), as_float(tick.ask, scale)
                ticker.bidSize, ticker.askSize = float(tick.bid_size), float(tick.ask_size)
            if rows is not None:
                step = instrument.price_increment
                ticker.domBids = [
                    DOMLevel(as_float(tick.bid - i * step, scale), float(tick.bid_size), "")
                    for i in range(rows)
                    if tick.bid - i * step > 0
                ]
                ticker.domAsks = [
                    DOMLevel(as_float(tick.ask + i * step, scale), float(tick.ask_size), "") for i in range(rows)
                ]
            self._handlers.tickers({ticker})
