"""Shared pieces for the execution tests: a fake broker, the real risk gate and an order manager, wired as a
trader would wire them, and a way to kill the process and start it again on the same files."""
from __future__ import annotations

from pathlib import Path

from hftlab.book import Reconciler
from hftlab.calendar import Calendar
from hftlab.core import InstrumentKind, Order, OrderType, Side, SimClock, TimeInForce, client_order_id
from hftlab.exec import OrderManager, Outbox
from hftlab.journal import Journal, read
from hftlab.risk import RiskGate
from hftlab.venues.fake import FakeBroker, FakeConfig, FakeMarket
from hftlab.venues.port import ConnectionState, VenueUnavailable
from hftlab.venues.supervisor import Backoff
from tests.risk.kit import limits
from tests.venues.kit import ASK, BID, EURUSD, MS, SECOND, T0, USDJPY

TRADED = (EURUSD, USDJPY)
JPY_BID, JPY_ASK = 151_230, 151_240
UP = (ConnectionState.RECONCILING, ConnectionState.READY)
# Limits wide enough that only the check a test is about can refuse.
WIDE = {
    "per_pair/EUR.USD/max_position": 10**9, "per_pair/EUR.USD/max_order_size": 10**9,
    "per_pair/USD.JPY/max_position": 10**9, "per_pair/USD.JPY/max_order_size": 10**9,
    "per_pair/EUR.USD/price_collar_ticks": 1_000, "per_pair/USD.JPY/price_collar_ticks": 1_000,
    "per_sleeve/control/max_position": 10**9, "per_sleeve/ml/max_position": 10**9,
    "per_sleeve/control/max_orders_per_minute": 10**6, "per_sleeve/ml/max_orders_per_minute": 10**6,
    "desk/max_orders_per_minute": 10**6, "desk/max_order_to_trade_ratio": 10**6, "desk/reject_streak_breaker": 10**6,
    "desk/loss_latches/USD/daily_loss": "1000000.00", "desk/loss_latches/USD/drawdown": "1000000.00",
    "per_sleeve/control/loss_latches/USD/daily_loss": "1000000.00",
    "per_sleeve/control/loss_latches/USD/drawdown": "1000000.00",
    "per_sleeve/ml/loss_latches/USD/daily_loss": "1000000.00", "per_sleeve/ml/loss_latches/USD/drawdown": "1000000.00",
}  # fmt: skip


class Floor:
    """One broker that lives on, and a trading process on top of it that can be killed and started again."""

    def __init__(
        self,
        folder: Path,
        config: FakeConfig | None = None,
        *,
        changes: dict[str, object] | None = None,
        reject=None,
        fsync: bool = False,
        up: bool = True,
        at_ns: int = T0,
    ) -> None:
        self.folder, self.fsync = folder, fsync
        self.changes = WIDE if changes is None else {**WIDE, **changes}
        self.clock = SimClock(at_ns)
        self.market = FakeMarket(TRADED, config, reject=reject)
        self.switched: set[str] = set()
        self.environ = {"MODE": "paper"}
        self.beat = True  # whether the trader confirms quiet books as fresh, as it does while the venue answers
        self.starts = 0
        self.start()
        if up:
            self.up()

    # ---- the process --------------------------------------------------------------------------------------------

    def start(self) -> None:
        """A process start: a new connection object, a new gate and a new manager, on the same files and broker."""
        from hftlab.safety import PaperGate

        self.starts += 1
        lock = PaperGate(port=self.market.config.port, env=self.environ)
        self.broker = FakeBroker(self.clock, self.market, gate=lock, backoff=Backoff())
        self.gate = RiskGate(
            limits(self.changes), TRADED, calendar=Calendar(), clock=self.clock, switches=lambda: self.switched,
            journal=Journal(self.folder / "risk.jsonl"),
        )  # fmt: skip
        self.oms = self.manager(self.broker)
        self.reconciler = Reconciler(
            self.broker, self.oms.book, Journal(self.folder / "reconcile.jsonl"), clock=self.clock,
            grace_ns=limits(self.changes).reconciliation_grace_ns, sink=self.oms.handle,
        )  # fmt: skip
        self.events: list = []
        self.subscribed = False

    def manager(self, venue) -> OrderManager:
        outbox = Outbox(self.folder / "outbox.jsonl", fsync=self.fsync)
        return OrderManager(venue, self.gate, outbox, clock=self.clock, instruments=TRADED)

    def connect(self) -> None:
        """Connect and subscribe. If the link is down, the venue keeps trying and `pump` subscribes when it is up."""
        try:
            self.broker.connect()
        except VenueUnavailable:
            return
        self._subscribe()

    def _subscribe(self) -> None:
        for instrument in TRADED:
            self.broker.subscribe_quotes(self.broker.instrument(instrument.pair, InstrumentKind.CASH))
        self.subscribed = True
        self.tick()

    def tick(self, bid: int = BID, ask: int = ASK, *, pair: str | None = None) -> None:
        """The broker's book as of now: the given quote for one pair, or the usual one for both."""
        now = self.clock.now_ns()
        if pair is None:
            self.market.script_quote(now, "EUR.USD", BID, ASK)
            self.market.script_quote(now, "USD.JPY", JPY_BID, JPY_ASK)
        else:
            self.market.script_quote(now, pair, bid, ask)

    def up(self) -> None:
        """Connect and reconcile until orders may flow."""
        if self.broker.state not in UP:
            self.connect()
        assert self.settle(), "the connection could not be made safe to trade on"

    def restart(self, *, up: bool = True) -> None:
        """The process is killed and started again. Nothing it held in memory survives; the broker goes on."""
        self.start()
        if up:
            self.up()

    # ---- the loop a trader would run ------------------------------------------------------------------------------

    def pump(self, duration_ns: int = 0) -> list:
        """Let time pass, poll, and hand every event to the manager, as the trader's loop does."""
        self.clock.advance(duration_ns)
        events = self.broker.poll()
        for event in events:
            self.oms.handle(event)
        self.events += events
        if self.broker.state in UP and not self.subscribed:
            self._subscribe()
        if self.broker.state in UP and self.beat:
            for instrument in TRADED:  # the venue answered: a quiet book is not a stale one
                self.gate.quiet(instrument.pair, self.clock.now_ns())
        return events

    def settle(self, tries: int = 60, step_ns: int = 10 * MS) -> bool:
        for _ in range(tries):
            self.pump(step_ns)
            if self.oms.reconcile():
                self.pump()
                return True
        return False

    def order(
        self,
        number: int,
        side: Side = Side.BUY,
        quantity: int = 20_000,
        *,
        sleeve: str = "control",
        pair: str = "EUR.USD",
        limit: int | None = None,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        order_id = client_order_id(sleeve=sleeve, pair=pair, side=side, decision_seq=number)
        kind = OrderType.MARKET if limit is None else OrderType.LIMIT
        return Order(order_id, sleeve, pair, side, quantity, kind, tif, self.clock.now_ns(), limit)

    def trade(self, number: int, side: Side = Side.BUY, quantity: int = 20_000, **more) -> Order:
        """Propose an order and follow it until the venue has had time to fill it."""
        order = self.order(number, side, quantity, **more)
        assert self.oms.propose(order).sent, "the test's own order was not sent"
        self.pump(50 * MS)
        return order

    # ---- reading --------------------------------------------------------------------------------------------------

    def outbox(self) -> list[dict[str, object]]:
        return read(self.folder / "outbox.jsonl")[1]

    def kinds(self) -> list[str]:
        return [str(entry["type"]) for entry in self.outbox()]

    def stage(self, order: Order) -> str | None:
        tracked = self.oms.tracked(order.client_order_id)
        return tracked.stage.value if tracked else None

    def at_broker(self) -> list[str]:
        """The client order ids the broker holds, working or done."""
        return sorted(self.market._orders)

    def held(self, pair: str = "EUR.USD") -> tuple[int, int, int]:
        """(the broker book's desk position, the gate's, the broker's own) in a pair."""
        from hftlab.book import DESK

        ours = self.oms.book.position(DESK, pair).quantity
        gates = self.gate.book.position(DESK, pair).quantity
        theirs = self.market.positions().get((pair, InstrumentKind.CASH), 0)
        return ours, gates, theirs


__all__ = ["ASK", "BID", "EURUSD", "MS", "SECOND", "T0", "TRADED", "USDJPY", "WIDE", "Floor"]
