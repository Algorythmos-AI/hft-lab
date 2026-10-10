"""The outbox: the order manager's write-ahead journal (ADR 0016).

An order is written here, and the write is on the disk, BEFORE the venue is handed it. So whatever kills the
process, and wherever, a restart finds every order that could possibly be at the venue, by the same client
order id the venue knows it by. What the outbox cannot say is whether the send happened; that is asked of the
venue, never guessed.

It is a hash-chained journal (`hftlab.journal`): append-only, a torn last line is cut off on opening, and a
whole line that does not verify stops the start. Its entries:

  intent      an order the risk gate allowed, in full, with the gate's code. Durable before the send.
  refused     an order that went no further, and the code that stopped it. Never an order.
  stage       one move of one order in the state machine, with its cause
  fill        a fill, booked once, in full
  commission  the commission of a fill
  cancel      a cancel request that was sent
  fault       something that cannot happen, and did
  start, reconciled, positions_differ    the manager's own milestones

Everything in it is an integer or text. Prices and sizes are the integers the platform trades in.
"""
from __future__ import annotations

from pathlib import Path

from hftlab.core import Fill, Liquidity, Order, OrderType, Side, TimeInForce
from hftlab.journal import Journal, JournalError, read
from hftlab.venues.port import CommissionEvent


class OutboxError(JournalError):
    """The outbox holds an entry that cannot be read back as what it claims to be. The start stops here."""


def whole(entry: dict[str, object], key: str) -> int:
    value = entry.get(key)
    if type(value) is not int:
        raise OutboxError(f"entry {entry.get('i')}: {key} is not a whole number")
    return value


def text(entry: dict[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise OutboxError(f"entry {entry.get('i')}: {key} is not text")
    return value


def _optional_whole(entry: dict[str, object], key: str) -> int | None:
    return None if entry.get(key) is None else whole(entry, key)


def order_fields(order: Order) -> dict[str, object]:
    return {
        "client_order_id": order.client_order_id, "sleeve": order.sleeve, "pair": order.pair,
        "side": order.side.value, "quantity": order.quantity, "order_type": order.order_type.value,
        "tif": order.tif.value, "created_ts_ns": order.created_ts_ns, "limit_price": order.limit_price,
    }  # fmt: skip


def order_from(entry: dict[str, object]) -> Order:
    try:
        return Order(
            text(entry, "client_order_id"), text(entry, "sleeve"), text(entry, "pair"), Side(text(entry, "side")),
            whole(entry, "quantity"), OrderType(text(entry, "order_type")), TimeInForce(text(entry, "tif")),
            whole(entry, "created_ts_ns"), _optional_whole(entry, "limit_price"),
        )  # fmt: skip
    except (TypeError, ValueError) as exc:
        raise OutboxError(f"entry {entry.get('i')} is not an order: {exc}") from exc


def fill_fields(fill: Fill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id, "client_order_id": fill.client_order_id, "pair": fill.pair, "side": fill.side.value,
        "quantity": fill.quantity, "price": fill.price, "ts_ns": fill.ts_ns, "liquidity": fill.liquidity.value,
        "commission": fill.commission, "commission_currency": fill.commission_currency,
        "venue_ts_ns": fill.venue_ts_ns, "seq": fill.seq,
    }  # fmt: skip


def fill_from(entry: dict[str, object]) -> Fill:
    currency = entry.get("commission_currency")
    try:
        return Fill(
            fill_id=text(entry, "fill_id"), client_order_id=text(entry, "client_order_id"), pair=text(entry, "pair"),
            side=Side(text(entry, "side")), quantity=whole(entry, "quantity"), price=whole(entry, "price"),
            ts_ns=whole(entry, "ts_ns"), liquidity=Liquidity(text(entry, "liquidity")),
            commission=whole(entry, "commission"), commission_currency=None if currency is None else str(currency),
            venue_ts_ns=_optional_whole(entry, "venue_ts_ns"), seq=whole(entry, "seq"),
        )  # fmt: skip
    except (TypeError, ValueError) as exc:
        raise OutboxError(f"entry {entry.get('i')} is not a fill: {exc}") from exc


def commission_from(entry: dict[str, object]) -> CommissionEvent:
    return CommissionEvent(text(entry, "fill_id"), whole(entry, "commission"), text(entry, "currency"), 0, 0)


class Outbox:
    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        self._journal = Journal(path, fsync=fsync)

    def open(self) -> list[dict[str, object]]:
        """Verify the chain, cut off a torn last line, and return every entry so far, oldest first."""
        entries = read(self._journal.path)[1]
        self._journal.open()
        return entries

    def record(self, kind: str, **fields: object) -> None:
        """Append one entry. It is in the file at once, and on the disk after the next `commit`."""
        self._journal.write(kind, **fields)
        self._journal.push()

    def commit(self) -> None:
        """Everything recorded so far is on the disk when this returns."""
        self._journal.flush()

    def intend(self, order: Order, *, code: str, outcome: str, at_ns: int) -> None:
        """Record an order that is about to be sent, and do not return until that record is on the disk."""
        self.record("intent", at_ns=at_ns, code=code, outcome=outcome, **order_fields(order))
        self.commit()

    def close(self) -> None:
        self._journal.close()
