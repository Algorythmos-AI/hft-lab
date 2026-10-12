"""The seam between the adapter and the broker library: a narrow client, and the real one over `ib_async.IB`.

The adapter depends on `IbClient`, a dozen calls that take and return the library's own objects. The real
client below is a pass-through with no logic of its own, so the adapter can be tested against a stub that
implements the same dozen calls, and what remains untested without a gateway is only what each line here
says it does.

Two things here lean on how `ib_async` 2.1.0 is built, both read from its source and pinned by the lock file:

  * `handshake` calls `ib.client.connectAsync` rather than `ib.connect`. `ib.connect` also fetches positions,
    orders and executions before it returns, and the paper-only lock must see the account list before
    anything else is asked of the gateway.
  * It therefore sets `ib.wrapper.clientId` itself, as `ib.connect` would have, because the library keys its
    orders by it.
"""
from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ib_async import IB, AccountValue, CommissionReport, Contract, ContractDetails, ExecutionFilter, Ticker, Trade
from ib_async import Fill as IbFill
from ib_async import Order as IbOrder
from ib_async import Position as IbPosition
from ib_async.objects import HistoricalTickBidAsk


@dataclass(frozen=True, slots=True)
class Handlers:
    """What the adapter wants to be told. Each is called from inside `pump`, never from another thread."""

    tickers: Callable[[Iterable[Ticker]], None]
    order_status: Callable[[Trade], None]
    execution: Callable[[Trade, IbFill], None]
    commission: Callable[[Trade, IbFill, CommissionReport], None]
    error: Callable[[int, int, str], None]
    disconnected: Callable[[], None]


class IbClient(Protocol):
    def listen(self, handlers: Handlers) -> None: ...

    def handshake(self, host: str, port: int, client_id: int, timeout_s: float) -> list[str]:
        """Open the socket, complete the API handshake and return the managed accounts. Request nothing else."""
        ...

    def connected(self) -> bool: ...

    def accounts(self) -> list[str]: ...

    def close(self) -> None: ...

    def pump(self, timeout_s: float) -> None:
        """Run the library's event loop until something arrives or `timeout_s` passes; handlers fire here."""
        ...

    def contract_details(self, contract: Contract) -> list[ContractDetails]: ...

    def subscribe_ticker(self, contract: Contract) -> None: ...

    def unsubscribe_ticker(self, contract: Contract) -> None: ...

    def subscribe_depth(self, contract: Contract, rows: int) -> None: ...

    def unsubscribe_depth(self, contract: Contract) -> None: ...

    def place_order(self, contract: Contract, order: IbOrder) -> Trade: ...

    def cancel_order(self, order: IbOrder) -> None: ...

    def open_trades(self) -> list[Trade]: ...

    def positions(self) -> list[IbPosition]: ...

    def executions(self) -> list[IbFill]: ...

    def server_time(self) -> datetime: ...

    def account_values(self, account: str) -> list[AccountValue]:
        """The account's values as the gateway keeps them up to date. Asks for nothing but to be told them."""
        ...

    def historical_bid_ask(self, contract: Contract, end: datetime, count: int) -> list[HistoricalTickBidAsk]: ...


def _ensure_event_loop() -> None:
    """The library runs its own event loop from synchronous code and expects the thread to have one already.

    Asking for the thread's loop when there is none is deprecated, and the library does exactly that, so one
    is made here first. A loop that is already there, open, is kept.
    """
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            current = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            current = None
    if current is None or current.is_closed():
        asyncio.set_event_loop(asyncio.new_event_loop())


class IbAsyncClient:
    """`IbClient` over a real `ib_async.IB`. Nothing here decides anything."""

    def __init__(self, ib: IB | None = None, *, request_timeout_s: float = 10.0) -> None:
        _ensure_event_loop()
        self._ib = ib if ib is not None else IB()
        # Without this a request the gateway never answers blocks for ever; with it the request raises.
        self._ib.RequestTimeout = request_timeout_s
        self._followed: str | None = None  # the account whose values this connection has asked to be told

    def listen(self, handlers: Handlers) -> None:
        ib = self._ib
        ib.pendingTickersEvent += handlers.tickers
        ib.orderStatusEvent += handlers.order_status
        ib.execDetailsEvent += handlers.execution
        ib.commissionReportEvent += handlers.commission
        ib.errorEvent += lambda request, code, message, _contract: handlers.error(request, code, message)
        ib.disconnectedEvent += handlers.disconnected

    def handshake(self, host: str, port: int, client_id: int, timeout_s: float) -> list[str]:
        ib = self._ib
        ib.wrapper.clientId = client_id
        self._followed = None  # a subscription does not outlive its socket
        ib.run(ib.client.connectAsync(host, port, client_id, timeout_s))
        return list(ib.client.getAccounts())

    def connected(self) -> bool:
        return self._ib.client.isReady()

    def accounts(self) -> list[str]:
        return list(self._ib.client.getAccounts())

    def close(self) -> None:
        self._ib.disconnect()

    def pump(self, timeout_s: float) -> None:
        if timeout_s > 0:
            self._ib.waitOnUpdate(timeout_s)
        else:
            self._ib.sleep(0)

    def contract_details(self, contract: Contract) -> list[ContractDetails]:
        return self._ib.reqContractDetails(contract)

    def subscribe_ticker(self, contract: Contract) -> None:
        # Seen on the real paper gateway (HFT-EXP-0007, HFT-EXP-0008): streaming top-of-book for forex needs no
        # generic ticks and no market-data subscription on a paper account. Updates do NOT arrive every few
        # milliseconds, as was assumed: about one to three a second per pair in an ordinary session.
        ticker = self._ib.reqMktData(contract, "", False, False)
        # The library keeps one ticker per contract for the life of the process. What it still shows is from an
        # earlier subscription; start from an empty book so a quote is never half old and half new.
        ticker.bid = ticker.ask = ticker.bidSize = ticker.askSize = float("nan")

    def unsubscribe_ticker(self, contract: Contract) -> None:
        self._ib.cancelMktData(contract)

    def subscribe_depth(self, contract: Contract, rows: int) -> None:
        self._ib.reqMktDepth(contract, rows)

    def unsubscribe_depth(self, contract: Contract) -> None:
        self._ib.cancelMktDepth(contract)

    def place_order(self, contract: Contract, order: IbOrder) -> Trade:
        return self._ib.placeOrder(contract, order)

    def cancel_order(self, order: IbOrder) -> None:
        self._ib.cancelOrder(order)

    def open_trades(self) -> list[Trade]:
        # UNVERIFIED: this lists the working orders of every client id on the account, with their orderRef.
        return self._ib.reqAllOpenOrders()

    def positions(self) -> list[IbPosition]:
        return self._ib.reqPositions()

    def executions(self) -> list[IbFill]:
        # UNVERIFIED: an empty filter returns the executions of the current day for every client id.
        return self._ib.reqExecutions(ExecutionFilter())

    def server_time(self) -> datetime:
        return self._ib.reqCurrentTime()

    def account_values(self, account: str) -> list[AccountValue]:
        # UNVERIFIED: once asked, the gateway sends the account's values when one changes and at least every
        # three minutes, and a cash balance arrives as the tag `CashBalance`, once per currency held. The first
        # call on a connection subscribes and waits for the first full set; later ones read what has arrived.
        if self._followed != account:
            self._ib.reqAccountUpdates(account)
            self._followed = account
        return self._ib.accountValues(account)

    def historical_bid_ask(self, contract: Contract, end: datetime, count: int) -> list[HistoricalTickBidAsk]:
        ticks = self._ib.reqHistoricalTicks(contract, "", end, count, "BID_ASK", useRth=False)
        return [tick for tick in ticks if isinstance(tick, HistoricalTickBidAsk)]


def _is_a_client(client: IbAsyncClient) -> IbClient:
    return client  # checked by mypy: the real client satisfies the seam
