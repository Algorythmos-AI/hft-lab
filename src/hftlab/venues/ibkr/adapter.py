"""The Interactive Brokers venue: the port over an `IbClient`, on top of the shared guard.

Thin on purpose. The guard decides whether anything may be sent; `translate` turns the library's objects into
core types; this module keeps the few tables that join the two (which contract is which instrument, which
trade is which order) and maps the gateway's connection messages onto the guard's states.

Connection messages relied on, all UNVERIFIED without a gateway:

  1100         the gateway lost its own connection to the broker; the API socket stays up. Treated as a lost
               connection: no orders while blind.
  1101, 1102   it is back (with or without the market-data subscriptions). Treated as a new connection: the
               lock is passed again on the account list and the caller reconciles before orders resume.
  10147, 10148 a cancel for an order that is already done.
  a daily restart of the gateway closes the socket; the supervisor reconnects on its backoff schedule.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ib_async import CommissionReport, Contract, Ticker, Trade
from ib_async import Fill as IbFill

from hftlab.core import Clock, Fill, Instrument, InstrumentKind, Order, OrderState, Quote, is_ours
from hftlab.safety.gate import PaperGate, Permit
from hftlab.safety.lock import declared_usernames
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.ibkr import translate
from hftlab.venues.ibkr.client import Handlers, IbClient
from hftlab.venues.ibkr.translate import TranslationError
from hftlab.venues.port import (
    Capabilities,
    OpenOrder,
    OrderRefused,
    UnknownInstrument,
    UnknownOrder,
    Venue,
    VenuePosition,
    VenueUnavailable,
)
from hftlab.venues.supervisor import Backoff

UPSTREAM_LOST: Final = 1100
UPSTREAM_BACK: Final = frozenset({1101, 1102})
CANCEL_TOO_LATE: Final = frozenset({10147, 10148})
_REPORTED_STATES: Final = (OrderState.REJECTED, OrderState.CANCELLED)


@dataclass(frozen=True, slots=True)
class IbkrSettings:
    host: str = "127.0.0.1"
    port: int = 4002  # IB Gateway in paper mode; the lock refuses anything but a paper port
    client_id: int = 77  # one fixed id: the gateway lets a client cancel only the orders of its own id
    timeout_s: float = 10.0
    account: str | None = None  # needed only when the login manages several paper accounts


class IbkrVenue(GuardedVenue):
    def __init__(
        self,
        *,
        clock: Clock,
        client: IbClient,
        settings: IbkrSettings | None = None,
        gate: PaperGate | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        self._settings = settings or IbkrSettings()
        if gate is None:
            username, declared = declared_usernames()
            gate = PaperGate(port=self._settings.port, username=username, live_username=declared)
        super().__init__(clock=clock, gate=gate, backoff=backoff)
        self._client = client
        self._contracts: dict[Instrument, Contract] = {}
        self._by_contract: dict[int, Instrument] = {}
        self._trades: dict[str, Trade] = {}
        self._acknowledged: set[str] = set()
        self._reported: dict[str, tuple[OrderState, int]] = {}
        self._streaming: set[Instrument] = set()
        self._deep: set[Instrument] = set()
        self._upstream = True
        self._closing = False
        client.listen(
            Handlers(
                tickers=self._on_tickers,
                order_status=self._on_order_status,
                execution=self._on_execution,
                commission=self._on_commission,
                error=self._on_error,
                disconnected=self._on_disconnected,
            )
        )

    @property
    def name(self) -> str:
        return "ibkr_paper"

    @property
    def capabilities(self) -> Capabilities:
        # Whether the paper account really has depth and history is for the probe to say; the port offers both.
        kinds = frozenset({InstrumentKind.CASH, InstrumentKind.CFD})
        return Capabilities(kinds, depth=True, historical_quotes=True)

    # ---- connection ---------------------------------------------------------------------------------------------

    def _port(self) -> int:
        return self._settings.port

    def _ask[T](self, request: Callable[[], T]) -> T:
        """Run one request against the gateway; a dead socket or a timeout is `VenueUnavailable`."""
        try:
            return request()
        except OSError as exc:  # ConnectionError and TimeoutError are both OSError
            raise VenueUnavailable(f"the gateway did not answer: {type(exc).__name__}: {exc}") from exc

    def _open(self) -> Sequence[str]:
        if self._client.connected():
            if not self._upstream:
                raise VenueUnavailable("the gateway has no connection to the broker")
            return self._ask(self._client.accounts)
        settings = self._settings
        self._upstream = True
        self._trades.clear()  # a new socket: ask the gateway for an order rather than trust a stale handle
        self._streaming.clear()  # and no subscription survives it
        self._deep.clear()
        return self._ask(
            lambda: self._client.handshake(settings.host, settings.port, settings.client_id, settings.timeout_s)
        )

    def _close(self) -> None:
        self._closing = True
        try:
            self._client.close()
        finally:
            self._closing = False

    def _accounts(self) -> Sequence[str]:
        return self._ask(self._client.accounts)

    def _pump(self, timeout_ns: int) -> None:
        self._client.pump(timeout_ns / 1_000_000_000)

    def _on_disconnected(self) -> None:
        if not self._closing:
            self._lost("the gateway closed the connection")

    def _on_error(self, request: int, code: int, message: str) -> None:
        if code == UPSTREAM_LOST:
            self._upstream = False
            self._lost(f"the gateway lost its connection to the broker ({code})")
        elif code in UPSTREAM_BACK:
            self._upstream = True
            self._retry_now()
            self._emit_notice(f"ib:{code}", message)
        elif code in CANCEL_TOO_LATE:
            self._emit_notice("cancel-too-late", message, self._order_of(request))
        else:
            self._emit_notice(f"ib:{code}", message, self._order_of(request))

    def _order_of(self, request: int) -> str | None:
        for client_order_id, trade in self._trades.items():
            if trade.order.orderId == request:
                return client_order_id
        return None

    # ---- instruments and market data ----------------------------------------------------------------------------

    def _lookup(self, pair: str, kind: InstrumentKind) -> Instrument:
        wanted = translate.contract_for(pair, kind)
        answers = self._ask(lambda: self._client.contract_details(wanted))
        if len(answers) != 1:
            found = "no contract" if not answers else f"{len(answers)} contracts"
            raise UnknownInstrument(f"the gateway knows {found} for {pair} as {kind.value}")
        try:
            instrument = translate.instrument_from(answers[0], pair, kind)
        except TranslationError as exc:
            raise UnknownInstrument(str(exc)) from exc
        contract = answers[0].contract
        assert contract is not None
        self._contracts[instrument] = contract
        self._by_contract[contract.conId] = instrument
        return instrument

    def _start_quotes(self, instrument: Instrument) -> None:
        contract = self._contracts[instrument]
        if instrument in self._streaming:
            # Back on a socket that never dropped: the old subscription may be alive or dead. End it either
            # way, so there is exactly one.
            self._ask(lambda: self._client.unsubscribe_ticker(contract))
        self._ask(lambda: self._client.subscribe_ticker(contract))
        self._streaming.add(instrument)

    def _stop_quotes(self, instrument: Instrument) -> None:
        self._streaming.discard(instrument)
        self._ask(lambda: self._client.unsubscribe_ticker(self._contracts[instrument]))

    def _start_depth(self, instrument: Instrument, levels: int) -> None:
        contract = self._contracts[instrument]
        if instrument in self._deep:
            self._ask(lambda: self._client.unsubscribe_depth(contract))
        self._ask(lambda: self._client.subscribe_depth(contract, levels))
        self._deep.add(instrument)

    def _stop_depth(self, instrument: Instrument) -> None:
        self._deep.discard(instrument)
        self._ask(lambda: self._client.unsubscribe_depth(self._contracts[instrument]))

    def _on_tickers(self, tickers: Iterable[Ticker]) -> None:
        for ticker in tickers:
            instrument = self._by_contract.get(ticker.contract.conId) if ticker.contract else None
            if instrument is None:
                continue
            pair = instrument.pair
            try:
                if self._quotes.get(pair) == instrument:
                    # The ticker fires for any of its fields; the guard passes on only a changed top of book.
                    quote = translate.quote_from(ticker, instrument)
                    if quote is not None:
                        self._emit_quote(pair, *quote)
                if self._depth.get(pair, (None, 0))[0] == instrument and (ticker.domBids or ticker.domAsks):
                    self._emit_depth(pair, *translate.depth_from(ticker, instrument))
            except TranslationError as exc:
                self._emit_notice("translation", str(exc))

    def _history(self, instrument: Instrument, end_ts_ns: int, count: int) -> tuple[Quote, ...]:
        contract = self._contracts[instrument]
        ticks = self._ask(lambda: self._client.historical_bid_ask(contract, translate.utc(end_ts_ns), count))
        quotes = (translate.history_quote(tick, instrument, seq) for seq, tick in enumerate(ticks))
        return tuple(quote for quote in quotes if quote is not None)

    # ---- orders -------------------------------------------------------------------------------------------------

    def _order_account(self) -> str:
        accounts = list(self._accounts())
        if self._settings.account is not None:
            if self._settings.account not in accounts:
                raise OrderRefused("the configured account is not one the gateway manages")
            return self._settings.account
        if len(accounts) != 1:
            raise OrderRefused("the login manages several accounts and none is configured for orders")
        return accounts[0]

    def _send(self, order: Order, instrument: Instrument, permit: Permit) -> None:
        if not self._gate.holds(permit):
            raise VenueUnavailable("the connection this order was checked for is gone")
        ib_order = translate.to_ib_order(order, instrument, self._order_account())
        contract = self._contracts[instrument]
        self._trades[order.client_order_id] = self._ask(lambda: self._client.place_order(contract, ib_order))

    def _cancel(self, client_order_id: str, permit: Permit) -> None:
        if not self._gate.holds(permit):
            raise VenueUnavailable("the connection this cancel was checked for is gone")
        trade = self._trades.get(client_order_id)
        if trade is None:  # sent by an earlier connection or an earlier process: ask the gateway for it
            working = self._ask(self._client.open_trades)
            trade = next((t for t in working if t.order.orderRef == client_order_id), None)
        if trade is None:
            raise UnknownOrder(f"the gateway holds no working order {client_order_id}")
        self._trades[client_order_id] = trade
        order = trade.order
        self._ask(lambda: self._client.cancel_order(order))

    def _on_order_status(self, trade: Trade) -> None:
        client_order_id = trade.order.orderRef
        if not is_ours(client_order_id):
            return  # a manual order or another program's: not this platform's to report
        last = trade.log[-1] if trade.log else None
        try:
            filled = translate.whole(trade.orderStatus.filled, "filled quantity")
        except TranslationError as exc:
            self._emit_notice("translation", str(exc), client_order_id)
            return
        state = translate.state_from(
            trade.orderStatus.status,
            filled,
            acknowledged=client_order_id in self._acknowledged,
            error_code=last.errorCode if last else 0,
        )
        if state is None or state is OrderState.SUBMITTED:
            return  # the guard reported SUBMITTED when the order left; a cancel in flight changes nothing yet
        if state in (OrderState.WORKING, OrderState.PARTIALLY_FILLED, OrderState.FILLED):
            self._acknowledged.add(client_order_id)
        if self._reported.get(client_order_id) == (state, filled):
            return
        self._reported[client_order_id] = (state, filled)
        permanent = trade.orderStatus.permId or trade.order.permId
        self._emit_order(
            client_order_id,
            state,
            filled_quantity=filled,
            broker_order_id=str(permanent) if permanent else None,
            reason=(last.message or None) if last and state in _REPORTED_STATES else None,
        )

    def _instrument_of(self, contract: Contract | None) -> Instrument | None:
        if contract is None:
            return None
        known = self._by_contract.get(contract.conId)
        if known is not None:
            return known
        pair, kind = translate.pair_of(contract), translate.KINDS.get(contract.secType)
        return next((i for i in self._contracts if i.pair == pair and i.kind is kind), None)

    def _on_execution(self, trade: Trade, fill: IbFill) -> None:
        client_order_id = fill.execution.orderRef or trade.order.orderRef
        if not is_ours(client_order_id):
            return
        instrument = self._instrument_of(fill.contract)
        if instrument is None:
            self._emit_notice("unknown-instrument", "an execution on an instrument never looked up", client_order_id)
            return
        try:
            terms = translate.execution_terms(fill.execution, instrument)
        except TranslationError as exc:
            self._emit_notice("translation", str(exc), client_order_id)
            return
        self._emit_fill(
            fill_id=terms.fill_id,
            client_order_id=client_order_id,
            pair=terms.pair,
            side=terms.side,
            quantity=terms.quantity,
            price=terms.price,
            liquidity=terms.liquidity,
            venue_ts_ns=terms.venue_ts_ns,
        )

    def _on_commission(self, trade: Trade, fill: IbFill, report: CommissionReport) -> None:
        if is_ours(fill.execution.orderRef or trade.order.orderRef) and (parsed := translate.commission_from(report)):
            self._emit_commission(*parsed)

    # ---- reconciliation queries ---------------------------------------------------------------------------------

    def _open_orders(self) -> tuple[OpenOrder, ...]:
        out = []
        for trade in self._ask(self._client.open_trades):
            instrument = self._instrument_of(trade.contract)
            if instrument is None:
                self._emit_notice("foreign-order", "a working order on an instrument this platform has not looked up")
                continue
            try:
                out.append(translate.open_order_from(trade, instrument))
            except TranslationError as exc:
                self._emit_notice("translation", str(exc))
        return tuple(out)

    def _positions(self) -> tuple[VenuePosition, ...]:
        out = []
        for position in self._ask(self._client.positions):
            try:
                found = translate.position_from(position)
            except TranslationError as exc:
                self._emit_notice("translation", str(exc))
                continue
            if found is not None:
                out.append(found)
        return tuple(sorted(out, key=lambda p: (p.pair, p.kind.value)))

    def _executions_since(self, ts_ns: int) -> tuple[Fill, ...]:
        now = self._clock.now_ns()
        out: list[Fill] = []
        for fill in self._ask(self._client.executions):
            instrument = self._instrument_of(fill.contract)
            if instrument is None or not is_ours(fill.execution.orderRef):
                continue
            try:
                terms = translate.execution_terms(fill.execution, instrument)
            except TranslationError as exc:
                self._emit_notice("translation", str(exc))
                continue
            if terms.venue_ts_ns is not None and terms.venue_ts_ns < ts_ns:
                continue  # one the gateway did not date is kept: better to reconcile it twice than never
            _, commission, currency = translate.commission_from(fill.commissionReport) or ("", 0, None)
            out.append(
                Fill(
                    fill_id=terms.fill_id,
                    client_order_id=terms.client_order_id,
                    pair=terms.pair,
                    side=terms.side,
                    quantity=terms.quantity,
                    price=terms.price,
                    ts_ns=now,
                    liquidity=terms.liquidity,
                    commission=commission,
                    commission_currency=currency,
                    venue_ts_ns=terms.venue_ts_ns,
                    seq=len(out),
                )
            )
        return tuple(out)

    def _server_time_ns(self) -> int:
        answered = translate.ns_of(self._ask(self._client.server_time))
        if answered is None:
            raise VenueUnavailable("the gateway's clock answer carried no time zone")
        return answered


def _is_a_venue(venue: IbkrVenue) -> Venue:
    return venue  # checked by mypy: the adapter satisfies the port
