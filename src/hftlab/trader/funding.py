"""The float of a cash account: what to convert to fund it, and where the funding stands (ADR 0031).

The broker's spot account lends nothing. A sleeve can only sell a currency the account holds and only buy with
one it holds, so before any sleeve trades on paper the account is given a float: an amount of every traded
currency, bought once, by conversions that are orders like any other. They go through the order manager and
the risk gate, they are booked in the broker book under the name `FLOAT`, and no sleeve and no shadow book
ever sees them.

Two things live here, and neither can send an order:

  * `plan`, a pure function: from the cash the account holds, the quotes and the configured float, the
    conversions that reach the float, or `CannotFund` when the account cannot cover them. Every currency of
    the float but the hub is the base of a pair against the hub, so the plan has two kinds of step: sell a
    currency the account has more of than its float, for the hub; buy a currency it has less of, with the hub.
    Each conversion is priced at the worst price its collar allows, and a little more of the hub is raised
    than that comes to, so the buys that follow are covered;
  * `FloatLedger`, the journal of what an operator asked for and what came of it. It holds no balance that is
    not also the venue's to say: the anchor (where the account's cash began, as far as the broker book is
    concerned) and the float position each pair is being taken to.

A funding is idempotent twice over. The plan is worked out from what the account holds, so a float that is
there needs no conversion; and the ledger knows a float stands, so asking again does nothing at all. A
funding that was interrupted goes on from the ledger and the book: what is left to convert is the target less
what the book holds, whatever happened in between.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from hftlab.core import CASH_SCALE, Instrument, Side
from hftlab.journal import Journal, read
from hftlab.risk import Inventory, PairLimits

UNIT: Final = 10**CASH_SCALE
NONE, FUNDING, FUNDED, UNFUNDING = "none", "funding", "funded", "unfunding"
FUND, UNFUND = "fund", "unfund"
ANCHOR: Final = "anchor"  # what an operator's request to take the venue's cash as the account's is called
OPERATOR: Final = "operator"  # why an anchor was taken, when it was asked for by itself
FILE: Final = "inventory"  # the ledger's journal, by name, among the trader's state journals
_BPS: Final = 10_000


class CannotFund(ValueError):
    """The account cannot be given the configured float from what it holds, or a price is missing to say."""


@dataclass(frozen=True, slots=True)
class Conversion:
    """One step of a funding: an amount of a pair's base currency bought or sold against the hub."""

    pair: str
    side: Side
    quantity: int  # base-currency units
    pays: str  # the currency it is paid in
    worst_micro: int  # the most it can cost (a buy) or the least it can bring (a sell), in millionths of the hub
    odd_lot: bool  # smaller than the pair's minimum entry: the venue takes it and prices it worse

    def line(self) -> str:
        verb = "BUY " if self.side is Side.BUY else "SELL"
        hub = f"{self.worst_micro // UNIT}"
        about = f"pays at most {hub}" if self.side is Side.BUY else f"brings at least {hub}"
        note = "  (odd lot: under the pair's minimum)" if self.odd_lot else ""
        return f"{verb} {self.quantity:>10} {self.pair}   {about} {self.pair[4:]}{note}"


def _up(value: int, step: int) -> int:
    return -(-value // step) * step


def worst(side: Side, bid: int, ask: int, instrument: Instrument, limits: PairLimits) -> int:
    """The furthest from the touch a market order of this side can fill and still have passed the gate's collar."""
    away = limits.exit_collar_ticks * instrument.price_increment
    return ask + away if side is Side.BUY else max(instrument.price_increment, bid - away)


def chunk(remaining: int, largest: int, step: int = 1) -> int:
    """The size of the next order of a conversion: what is left, in as few orders of near-equal size as the
    pair's order limit allows, so that no order but one that is small by itself falls under a minimum."""
    if remaining <= largest:
        return remaining
    count = -(-remaining // largest)
    return min(largest, _up(-(-remaining // count), step))


def plan(
    cash: Mapping[str, int],
    quotes: Mapping[str, tuple[int, int]],
    instruments: Mapping[str, Instrument],
    inventory: Inventory,
    pairs: Mapping[str, PairLimits],
) -> tuple[Conversion, ...]:
    """The conversions that take the account from `cash` to the configured float, sells first.

    Raises `CannotFund`, with the reason in words, when a quote is missing or the account cannot cover them.
    """
    hub = inventory.hub

    def priced(code: str, side: Side) -> tuple[Instrument, int, int]:
        pair = f"{code}.{hub}"
        instrument, limits, quote = instruments.get(pair), pairs.get(pair), quotes.get(pair)
        if instrument is None or limits is None:
            raise CannotFund(f"{pair} is not a traded pair, so {code} cannot be converted")
        if quote is None:
            raise CannotFund(f"there is no quote for {pair} to price a conversion at")
        scale = int(10 ** (CASH_SCALE - instrument.price_scale))
        return instrument, worst(side, quote[0], quote[1], instrument, limits) * scale, limits.max_order_size

    buys: list[Conversion] = []
    cost = 0
    for code in sorted(set(inventory.float_micro) - {hub}):
        short = inventory.float_micro[code] - cash.get(code, 0)
        if short <= 0:
            continue
        instrument, price, _ = priced(code, Side.BUY)
        quantity = _up(-(-short // UNIT), instrument.size_increment)
        least = inventory.minimum_entry.get(instrument.pair, 0)
        buys.append(Conversion(instrument.pair, Side.BUY, quantity, hub, quantity * price, quantity < least))
        cost += quantity * price
    wanted = inventory.float_micro.get(hub, 0) + -(-cost * (_BPS + inventory.funding_buffer_bps) // _BPS)
    short = wanted - cash.get(hub, 0)
    sells: list[Conversion] = []
    bought = {step.pair[:3] for step in buys}
    spare = {
        code: cash.get(code, 0) - inventory.float_micro[code]
        for code in set(inventory.float_micro) - {hub} - bought
        if cash.get(code, 0) > inventory.float_micro[code]
    }
    for code in sorted(spare, key=lambda name: (-spare[name], name)):
        if short <= 0:
            break
        instrument, price, _ = priced(code, Side.SELL)
        step = instrument.size_increment
        most = spare[code] // UNIT // step * step
        quantity = min(most, _up(-(-short // price), step))
        if quantity <= 0:
            continue
        least = inventory.minimum_entry.get(instrument.pair, 0)
        sells.append(Conversion(instrument.pair, Side.SELL, quantity, code, quantity * price, quantity < least))
        short -= quantity * price
    if short > 0:
        raise CannotFund(
            f"the account is short of {hub}: the float and its conversions need {-(-short // UNIT)} more {hub} than it "
            "holds or can raise from the currencies it has more of than their float"
        )
    return (*sells, *buys)


def targets(held: Mapping[str, int], steps: Sequence[Conversion]) -> dict[str, int]:
    """The float position each pair is taken to: what the book's float line holds now, and the plan on top."""
    out = dict(held)
    for step in steps:
        out[step.pair] = out.get(step.pair, 0) + step.side.sign * step.quantity
    return {pair: quantity for pair, quantity in out.items() if pair in {step.pair for step in steps}}


class FloatLedger:
    """What was asked of the float and what came of it, in a hash-chained journal of its own.

        anchor   where the account's cash began, by currency, when an operator asked for a funding (or said the
                 venue's cash is to be believed again): the venue's cash at that moment less everything the
                 broker book's fills had moved. One an operator asked for by itself (`--reanchor`) also says
                 what it took for granted: `absorbed`, how far the venue's cash was from the old anchor and
                 the fills, by currency; `restated`, the position of every sleeve the book held then, which
                 the new anchor takes as true; and `baseline`, the float's baseline from then on
        open     a funding or an unfunding began: the float position each pair is being taken to, in order
        closed   it ended: for a funding, with the baseline it left; for an unfunding, with what could not be
                 converted back

    The phase is a function of the journal: none, funding, funded, unfunding. It is rebuilt from the file when
    the ledger is built, so a restart in the middle of a funding goes on with it.
    """

    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self.anchor: dict[str, int] | None = None
        self.phase = NONE
        self.targets: dict[str, int] = {}
        self.order: tuple[str, ...] = ()
        self.baseline: dict[str, int] | None = None
        self.operator = ""
        for entry in read(journal.path)[1]:
            self._restore(entry)
        journal.open()

    @property
    def funded(self) -> bool:
        return self.phase == FUNDED

    @property
    def converting(self) -> bool:
        return self.phase in (FUNDING, UNFUNDING)

    def set_anchor(self, cash: Mapping[str, int], *, operator: str, why: str, at_ns: int) -> None:
        self.anchor = dict(sorted(cash.items()))
        self._journal.write("anchor", cash=self.anchor, operator=operator, why=why, at_ns=at_ns)
        self._journal.flush()

    def reanchor(
        self,
        cash: Mapping[str, int],
        *,
        operator: str,
        at_ns: int,
        absorbed: Mapping[str, int],
        restated: Sequence[Sequence[object]],
        baseline: Mapping[str, int] | None,
    ) -> None:
        """An operator said the venue's cash is to be believed. The entry says what that took for granted."""
        self.anchor = dict(sorted(cash.items()))
        if self.phase == FUNDED and baseline is not None:
            self.baseline = dict(sorted(baseline.items()))
        self._journal.write(
            "anchor", cash=self.anchor, operator=operator, why=OPERATOR, at_ns=at_ns,
            absorbed=dict(sorted(absorbed.items())), restated=[list(line) for line in restated],
            baseline=self.baseline,
        )  # fmt: skip
        self._journal.flush()

    def open(self, what: str, wanted: Mapping[str, int], order: Sequence[str], *, operator: str, at_ns: int) -> None:
        self.phase = FUNDING if what == FUND else UNFUNDING
        self.targets, self.order, self.operator = dict(wanted), tuple(order), operator
        if what == UNFUND:
            self.baseline = None
        self._journal.write(
            "open", what=what, targets=self.targets, order=list(self.order), operator=operator, at_ns=at_ns
        )
        self._journal.flush()

    def close(self, *, baseline: Mapping[str, int] | None, left: Mapping[str, int], at_ns: int) -> str:
        """The conversion in hand ended. Returns what it was: a funding leaves a float, an unfunding leaves none."""
        what = FUND if self.phase == FUNDING else UNFUND
        self.phase = FUNDED if what == FUND else NONE
        self.baseline = dict(sorted(baseline.items())) if baseline is not None and what == FUND else None
        self.targets, self.order = {}, ()
        self._journal.write("closed", what=what, baseline=self.baseline, left=dict(left), at_ns=at_ns)
        self._journal.flush()
        return what

    def _restore(self, entry: dict[str, object]) -> None:
        kind = entry.get("type")
        if kind == "anchor" and isinstance(cash := entry.get("cash"), dict):
            self.anchor = {str(code): int(amount) for code, amount in cash.items()}
            if isinstance(kept := entry.get("baseline"), dict) and self.phase == FUNDED:
                self.baseline = {str(code): int(amount) for code, amount in kept.items()}
        elif kind == "open" and isinstance(wanted := entry.get("targets"), dict):
            self.phase = FUNDING if entry.get("what") == FUND else UNFUNDING
            self.targets = {str(pair): int(quantity) for pair, quantity in wanted.items()}
            order = entry.get("order")
            self.order = tuple(str(pair) for pair in order) if isinstance(order, list) else tuple(self.targets)
            self.operator = str(entry.get("operator") or "")
            if self.phase == UNFUNDING:
                self.baseline = None
        elif kind == "closed":
            self.phase = FUNDED if entry.get("what") == FUND else NONE
            kept = entry.get("baseline")
            self.baseline = {str(c): int(a) for c, a in kept.items()} if isinstance(kept, dict) else None
            self.targets, self.order = {}, ()

    def shut(self) -> None:
        self._journal.close()
