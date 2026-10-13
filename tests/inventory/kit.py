"""Shared pieces for the tests of a cash account with a float (ADR 0031).

A fake broker that lends nothing: it holds cash in one currency to begin with, refuses an order it cannot pay
for, and may not hold yen. On top of it either the trader as the command wires it (`Bureau`), or the risk gate,
the order manager and the reconciler alone (`Till`), which is quicker and is what the property tests drive.
Every balance here is made up.
"""
from __future__ import annotations

from pathlib import Path

from hftlab.book import FLOAT, CashCheck, Reconciler, anchor_of, effect
from hftlab.calendar import Calendar
from hftlab.core import Instrument, InstrumentKind, Order, OrderType, Side, SimClock, TimeInForce, client_order_id
from hftlab.exec import OrderManager, Outbox
from hftlab.journal import Journal, read
from hftlab.risk import FloatState, RiskConfig, RiskGate, parse_risk_config
from hftlab.safety import PaperGate
from hftlab.venues.fake import FakeBroker, FakeConfig, FakeMarket
from hftlab.venues.supervisor import Backoff
from tests.exec.kit import WIDE
from tests.risk.kit import raw
from tests.trader.kit import NEVER, Desk, put_stage, raw_settings
from tests.venues.kit import EURUSD, MS, SECOND, T0, USDJPY

UNIT = 1_000_000
AUDUSD = Instrument.from_pair(
    "AUD.USD", price_scale=5, min_order_size=25_000, kind=InstrumentKind.CASH, size_increment=1_000, price_increment=5
)
TRADED = (EURUSD, AUDUSD, USDJPY)
PAIRS = tuple((instrument.pair, InstrumentKind.CASH) for instrument in TRADED)
EUR_BID, EUR_ASK = 108_500, 108_510
AUD_BID, AUD_ASK = 69_890, 69_900
JPY_BID, JPY_ASK = 151_230, 151_240
QUOTES = {"EUR.USD": (EUR_BID, EUR_ASK), "AUD.USD": (AUD_BID, AUD_ASK), "USD.JPY": (JPY_BID, JPY_ASK)}
START = (("AUD", 1_000_000 * UNIT),)  # the account as a new paper account is: everything in one currency
FLOATS = {"USD": 100_000, "EUR": 60_000, "AUD": 80_000}
RESERVES = {"USD": 5_000, "EUR": 5_000, "AUD": 5_000}


def account(cash=START, **changes) -> FakeConfig:
    """A cash account: it lends nothing, may not hold yen, and charges its commission in its own currency."""
    settings = {
        "cash": tuple(cash), "cash_only": True, "forbidden": ("JPY",), "commission_currency": "AUD",
        "odd_lot_below": 20_000,
    }  # fmt: skip
    return FakeConfig(**{**settings, **changes})


def inventory(**changes) -> dict:
    block = {
        "hub": "USD", "float": dict(FLOATS), "reserve": dict(RESERVES),
        "minimum_entry": {"EUR.USD": 20_000, "AUD.USD": 25_000}, "shadow_only": ["USD.JPY"],
        "funding_buffer_bps": 50, "cash_tolerance": "1.00", "settle_seconds": 2,
    }  # fmt: skip
    return {**block, **changes}


def loaded_limits(changes: dict[str, object] | None = None, **block) -> dict:
    """The test limits for a cash account as a loaded risk file: what `limits` parses, and what a command reads."""
    loaded = raw({**WIDE, "per_pair/USD.JPY": ...})
    loaded["per_pair"]["AUD.USD"] = dict(loaded["per_pair"]["EUR.USD"])
    loaded["inventory"] = inventory(**block)
    # The account charges its commission in its own currency, and a fee is a loss in it: without a latch in
    # that currency the first fill would stop every entry (UNLATCHED_CURRENCY).
    for section in (loaded["desk"], *loaded["per_sleeve"].values()):
        section["loss_latches"]["AUD"] = dict(section["loss_latches"]["USD"])
    for path, value in (changes or {}).items():
        *parents, last = path.split("/")
        section = loaded
        for name in parents:
            section = section[name]
        section[last] = value
    return loaded


def limits(changes: dict[str, object] | None = None, **block) -> RiskConfig:
    """The test limits for a cash account: EUR.USD and AUD.USD on paper, USD.JPY on the shadow venue only."""
    return parse_risk_config(loaded_limits(changes, **block))


def settings(**changes) -> dict:
    """The trader's settings with the three pairs and a busy control sleeve."""
    loaded = raw_settings(pairs=[{"pair": pair, "form": "cash"} for pair, _ in PAIRS])
    return {**loaded, **changes}


class Bureau(Desk):
    """The trader, wired as the command wires it, on a cash account."""

    def __init__(self, folder: Path, config: FakeConfig | None = None, *, risk: RiskConfig | None = None, **more):
        super().__init__(
            folder, config or account(), settings=more.pop("settings", None) or settings(), traded=TRADED,
            pairs=PAIRS, risk=risk or limits(), **more,
        )  # fmt: skip

    def quotes(self, seconds: int, *, every_ms: int = 100, size: int | None = None) -> None:
        """Three still books that are confirmed often enough to stay fresh, from where the last call left off."""
        begin = max(self.quoted, self.clock.now_ns())  # a turn may have run a little past where the last call ended
        for number in range(seconds * 1_000 // every_ms):
            at = begin + MS + number * every_ms * MS
            wiggle = 5 * (number % 2)  # the top changes, so the venue has something to say
            for pair, (bid, ask) in QUOTES.items():
                self.market.script_quote(at, pair, bid + wiggle, ask + wiggle)
        self.quoted = begin + seconds * SECOND

    def cash(self, code: str) -> int:
        """What the broker itself holds of a currency, in whole units, rounded down."""
        return self.market.cash().get(code, 0) // UNIT

    def float_lines(self) -> dict[str, int]:
        book = self.trader.broker_book
        return {pair: held for (name, pair), held in book.open_lines().items() if name == FLOAT}

    def ledger(self) -> list[dict[str, object]]:
        return read(self.folder / "inventory.jsonl")[1]

    def reconciled(self) -> list[dict[str, object]]:
        """What the reconciler wrote down: nothing at all while the venue and the book agree."""
        return read(self.folder / "reconcile.jsonl")[1]

    def fund(self, seconds: int = 30) -> None:
        """Ask for the float and let the trader convert until it stands."""
        self.run(3)
        self.ask("fund", "float")
        self.run(seconds)
        assert self.trader.float_status()["phase"] == "funded", self.of("float")


class Till:
    """The risk gate, the order manager and the reconciler on a cash account, with no trader above them."""

    def __init__(
        self, folder: Path, config: FakeConfig | None = None, *, risk: RiskConfig | None = None,
        spot_on_cash: bool = True,
    ) -> None:  # fmt: skip
        self.folder, self.risk = folder, risk or limits()
        self.clock = SimClock(T0)
        self.market = FakeMarket(TRADED, config or account())
        lock = PaperGate(port=self.market.config.port, env={"MODE": "paper"})
        self.broker = FakeBroker(self.clock, self.market, gate=lock, backoff=Backoff())
        self.gate = RiskGate(
            self.risk, TRADED, calendar=Calendar(), clock=self.clock, switches=frozenset,
            journal=Journal(folder / "risk.jsonl"),
        )  # fmt: skip
        self.oms = OrderManager(
            self.broker, self.gate, Outbox(folder / "outbox.jsonl"), clock=self.clock, instruments=TRADED,
            spot_on_cash=spot_on_cash,  # as the trader builds it wherever the limits hold a float
        )  # fmt: skip
        self.anchor: dict[str, int] | None = None
        check = CashCheck(self.broker.cash, lambda: self.anchor, self.risk.inventory.tolerance_micro)
        self.reconciler = Reconciler(
            self.broker, self.oms.book, Journal(folder / "reconcile.jsonl"), clock=self.clock,
            grace_ns=self.risk.reconciliation_grace_ns, sink=self.oms.handle, cash=check,
            spot_on_cash=spot_on_cash,
        )  # fmt: skip
        self.number = 0
        self.broker.connect()
        for instrument in TRADED:
            self.broker.subscribe_quotes(self.broker.instrument(instrument.pair, InstrumentKind.CASH))
        self.tick()
        for _ in range(60):
            self.pump(10 * MS)
            if self.oms.reconcile():
                break
        self.pump()

    def tick(self, moved: int = 0) -> None:
        """Every book as of now, `moved` price units from where it usually is."""
        for pair, (bid, ask) in QUOTES.items():
            self.market.script_quote(self.clock.now_ns(), pair, bid + moved, ask + moved)

    def pump(self, duration_ns: int = 0) -> list:
        self.clock.advance(duration_ns)
        events = self.broker.poll()
        for event in events:
            self.oms.handle(event)
        for instrument in TRADED:
            self.gate.quiet(instrument.pair, self.clock.now_ns())
        return events

    def show(self, *, funded: bool, open_: bool = False, anchor: bool = True) -> None:
        """Take the anchor from the venue as it stands, and tell the gate where the float stands."""
        if anchor and self.anchor is None:
            seen = {held.currency: held.amount for held in self.broker.cash()}
            self.anchor = anchor_of(seen, self.oms.book)
        self.gate.observe(FloatState(self.anchor, funded, open_))

    def order(self, side: Side, quantity: int, *, pair: str = "EUR.USD", sleeve: str = "control") -> Order:
        self.number += 1
        order_id = client_order_id(sleeve=sleeve, pair=pair, side=side, decision_seq=self.number)
        return Order(order_id, sleeve, pair, side, quantity, OrderType.MARKET, TimeInForce.IOC, self.clock.now_ns())

    def send(self, side: Side, quantity: int, **more) -> str:
        """Propose an order and give the venue time to answer. Returns the code it was let through or stopped with."""
        proposal = self.oms.propose(self.order(side, quantity, **more))
        self.pump(50 * MS)
        return proposal.code

    def convert(self, side: Side, quantity: int, pair: str) -> str:
        """One conversion of the float, as the trader would propose it while a funding is open."""
        return self.send(side, quantity, pair=pair, sleeve=FLOAT)

    def fund(self) -> None:
        """Give the account the float the limits ask for, by hand: sell the surplus for the hub, then buy."""
        self.show(funded=False, open_=True)
        assert self.convert(Side.SELL, 250_000, "AUD.USD") == "ALLOW"
        self.settle()
        assert self.convert(Side.BUY, 60_000, "EUR.USD") == "ALLOW"
        self.settle()
        self.show(funded=True)

    def settle(self) -> None:
        """Give every order on its way the time to end, however slow the venue is to answer."""
        for _ in range(100):
            if not self.oms.in_flight():
                return
            self.pump(100 * MS)
        raise AssertionError("an order did not end in ten seconds")

    def held(self, code: str) -> int:
        return self.market.cash().get(code, 0)

    def agrees(self) -> bool:
        """Is the venue's cash exactly the anchor plus what the book's fills moved, in every currency?"""
        assert self.anchor is not None
        moved, seen = effect(self.oms.book), self.market.cash()
        return all(seen.get(c, 0) == self.anchor.get(c, 0) + moved.get(c, 0) for c in set(seen) | set(moved))


__all__ = [
    "AUDUSD", "FLOAT", "FLOATS", "NEVER", "PAIRS", "QUOTES", "RESERVES", "START", "TRADED", "UNIT", "Bureau", "Till",
    "account", "inventory", "limits", "loaded_limits", "put_stage", "settings",
]  # fmt: skip
