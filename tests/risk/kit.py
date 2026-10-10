"""Shared pieces for the risk tests: limits that are in force, and a gate at a quiet moment of the week."""
from __future__ import annotations

from pathlib import Path

from hftlab.calendar import Calendar
from hftlab.core import (
    Fill,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Quote,
    Side,
    SimClock,
    TimeInForce,
    client_order_id,
)
from hftlab.journal import Journal, read
from hftlab.risk import RiskConfig, RiskGate, parse_risk_config
from hftlab.venues.port import ConnectionEvent, ConnectionState
from tests.venues.kit import ASK, BID, EURUSD, MS, SECOND, T0, USDJPY

MINUTE = 60 * SECOND
TRADED = (EURUSD, USDJPY)
JPY_BID, JPY_ASK = 151_230, 151_240
KILL, FLATTEN = "KILL", "FLATTEN"



def _latches() -> dict:
    return {
        "USD": {"daily_loss": "100.00", "drawdown": "150.00"},
        "JPY": {"daily_loss": "15000.00", "drawdown": "20000.00"},
    }


def _pair() -> dict:
    return {
        "max_position": 100_000, "max_order_size": 60_000, "price_collar_ticks": 10, "exit_collar_ticks": 40,
        "max_spread_ticks": 4,
        "stale_quote_ms": 2_000,
    }  # fmt: skip


def _sleeve() -> dict:
    return {"max_position": 60_000, "max_orders_per_minute": 20, "loss_latches": _latches()}


def _limits() -> dict:
    return {
        "status": "decided",
        "in_force": True,
        "decision": "HFT-DEC-9999",  # a test's stand-in: no such record exists, and no shipped value comes from here
        "desk": {
            "max_gross_exposure": None, "max_leverage": None, "max_open_pairs": 2, "max_orders_per_minute": 30,
            "max_order_to_trade_ratio": 10, "order_to_trade_window_seconds": 300, "reject_streak_breaker": 3,
            "loss_latches": _latches(),
        },
        "per_pair": {"EUR.USD": _pair(), "USD.JPY": _pair()},
        "per_sleeve": {"control": _sleeve(), "ml": _sleeve()},
        "market_guards": {
            "rollover_no_entry_minutes_before": 10, "rollover_no_entry_minutes_after": 20,
            "weekly_close_no_entry_minutes": 30,
        },
        "reconciliation": {"grace_seconds": 5},
        "switches": {"kill": KILL, "flatten": FLATTEN, "flatten_retry_seconds": 5},
    }  # fmt: skip


def raw(changes: dict[str, object] | None = None) -> dict:
    """The test limits as a loaded file, with `changes` applied: {"desk/max_open_pairs": 1}. `...` removes a key."""
    loaded = _limits()
    for path, value in (changes or {}).items():
        *parents, last = path.split("/")
        section = loaded
        for name in parents:
            section = section[name]
        if value is ...:
            del section[last]
        else:
            section[last] = value
    return loaded


def limits(changes: dict[str, object] | None = None) -> RiskConfig:
    return parse_risk_config(raw(changes))


def an_order(
    number: int = 1,
    side: Side = Side.BUY,
    quantity: int = 20_000,
    *,
    sleeve: str = "control",
    pair: str = "EUR.USD",
    limit: int | None = None,
    at_ns: int = T0,
) -> Order:
    order_id = client_order_id(sleeve=sleeve, pair=pair, side=side, decision_seq=number)
    kind = OrderType.MARKET if limit is None else OrderType.LIMIT
    return Order(order_id, sleeve, pair, side, quantity, kind, TimeInForce.FOK, at_ns, limit)


class Desk:
    """A gate with its clock, its switches and its journal, and short ways to show it what a venue would."""

    def __init__(
        self,
        folder: Path,
        changes: dict[str, object] | None = None,
        *,
        calendar: Calendar | None = None,
        at_ns: int = T0,
        clock=None,
        ready: bool = True,
    ) -> None:
        self.clock = clock or SimClock(at_ns)
        self.folder, self.changes, self.calendar = folder, changes, calendar or Calendar()
        self.switched: set[str] = set()
        self.unreadable = False
        self.seq = 0
        self.booked: list[Fill] = []
        self.gate = self.build()
        if ready:
            self.gate.observe(ConnectionEvent(ConnectionState.READY, self.clock.now_ns(), 0))
            self.quote()
            self.quote(JPY_BID, JPY_ASK, pair="USD.JPY")

    def build(self) -> RiskGate:
        """A gate on the same journal: the first one, or the one a restart would make."""
        return RiskGate(
            limits(self.changes), TRADED, calendar=self.calendar, clock=self.clock, switches=self._switches,
            journal=Journal(self.folder / "risk.jsonl"),
        )  # fmt: skip

    def _switches(self) -> set[str]:
        if self.unreadable:
            raise PermissionError("the state directory cannot be read")
        return set(self.switched)

    def reconnect(self) -> None:
        for state in (ConnectionState.DISCONNECTED, ConnectionState.RECONCILING, ConnectionState.READY):
            self.gate.observe(ConnectionEvent(state, self.clock.now_ns(), 0))

    def restart(self, *, ready: bool = True) -> RiskGate:
        """A new gate on the same journal, shown the fills the outbox would replay to it."""
        self.gate = self.build()
        for fill in self.booked:
            self.gate.observe(fill)
        if ready:
            self.gate.observe(ConnectionEvent(ConnectionState.READY, self.clock.now_ns(), 0))
        return self.gate

    def after(self, duration_ns: int) -> None:
        self.clock.advance(duration_ns)

    def quote(self, bid: int = BID, ask: int = ASK, *, pair: str = "EUR.USD") -> None:
        self.seq += 1
        self.gate.observe(Quote(pair, bid, ask, 1_000_000, 1_000_000, self.clock.now_ns(), self.seq))

    def check(self, order: Order):
        return self.gate.check(order)

    def code(self, order: Order) -> str:
        return self.gate.check(order).code

    def fill(self, order: Order, price: int, quantity: int | None = None, *, report: bool = True) -> Fill:
        """Show the gate a fill of `order`, and (unless told not to) the order event that goes with it."""
        quantity = order.quantity if quantity is None else quantity
        now = self.clock.now_ns()
        fill = Fill(f"x{len(self.booked) + 1}", order.client_order_id, order.pair, order.side, quantity, price, now)
        self.booked.append(fill)
        self.gate.observe(fill)
        if report:
            done = quantity == order.quantity
            state = OrderState.FILLED if done else OrderState.PARTIALLY_FILLED
            self.gate.observe(OrderEvent(order.client_order_id, state, now, 0, filled_quantity=quantity))
        return fill

    def end(self, order: Order, state: OrderState = OrderState.CANCELLED, filled: int = 0) -> None:
        self.gate.observe(OrderEvent(order.client_order_id, state, self.clock.now_ns(), 0, filled_quantity=filled))

    def hold(self, number: int, side: Side = Side.BUY, quantity: int = 20_000, *, price: int | None = None, **more):
        """Take a position: an allowed order, filled in full at the touch."""
        order = an_order(number, side, quantity, **more)
        assert self.gate.check(order).allowed, "the test's own entry was refused"
        bid, ask = (BID, ASK) if order.pair == "EUR.USD" else (JPY_BID, JPY_ASK)
        self.fill(order, price or (ask if side is Side.BUY else bid))
        return order

    def journal(self) -> list[dict[str, object]]:
        return read(self.folder / "risk.jsonl")[1]


__all__ = [
    "ASK", "BID", "EURUSD", "FLATTEN", "KILL", "MINUTE", "MS", "SECOND", "T0", "USDJPY", "Desk", "an_order", "limits",
    "raw",
]  # fmt: skip
