"""The outbox keeps an order and a fill exactly: what is read back is what was written, to the last unit."""
from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import Fill, Liquidity, Order, OrderType, Side, TimeInForce, client_order_id
from hftlab.exec import Outbox, OutboxError
from hftlab.exec.outbox import fill_fields, fill_from, order_fields, order_from
from hftlab.journal import read, verify

BIG = st.integers(1, 10**18)
AN_ID = client_order_id(sleeve="control", pair="EUR.USD", side=Side.BUY, decision_seq=7)


def an_order(side: Side, quantity: int, tif: TimeInForce, at_ns: int, limit: int | None) -> Order:
    kind = OrderType.MARKET if limit is None else OrderType.LIMIT
    return Order(AN_ID, "control", "EUR.USD", side, quantity, kind, tif, at_ns, limit)


def a_fill(number: int, side: Side, quantity: int, price: int, at_ns: int, liquidity: Liquidity, fee, venue_at) -> Fill:
    return Fill(f"x{number}", AN_ID, "USD.JPY", side, quantity, price, at_ns, liquidity, *fee, venue_at, number)


FEES = st.just((0, None)) | st.tuples(st.integers(-(10**12), 10**12), st.sampled_from(["USD", "JPY", "AUD"]))
ORDERS = st.builds(an_order, st.sampled_from(Side), BIG, st.sampled_from(TimeInForce), BIG, st.none() | BIG)
FILLS = st.builds(
    a_fill, st.integers(0, 10**9), st.sampled_from(Side), BIG, BIG, BIG, st.sampled_from(Liquidity), FEES,
    st.none() | BIG,
)  # fmt: skip


@given(ORDERS, FILLS)
def test_an_order_and_a_fill_come_back_from_the_outbox_exactly_as_they_went_in(tmp_path_factory, order, fill):
    path = tmp_path_factory.mktemp("outbox") / "outbox.jsonl"
    box = Outbox(path, fsync=False)
    assert box.open() == []
    box.intend(order, code="ALLOW", outcome="ALLOW", at_ns=order.created_ts_ns)
    box.record("fill", **fill_fields(fill))
    box.close()
    intent, booked = Outbox(path, fsync=False).open()
    assert order_from(intent) == order and fill_from(booked) == fill
    assert verify(path).entries == 2 and all(type(v) in (int, str, type(None)) for v in {**intent, **booked}.values())


def test_a_recorded_line_is_in_the_file_at_once_and_on_the_disk_after_a_commit(tmp_path, monkeypatch):
    synced: list[int] = []
    real = __import__("os").fsync
    monkeypatch.setattr("hftlab.journal.os.fsync", lambda descriptor: (synced.append(descriptor), real(descriptor))[1])
    box = Outbox(tmp_path / "outbox.jsonl")
    box.open()
    created = len(synced)
    assert created == 2, "a new outbox: the file, and its name in the directory"
    box.record("refused", code="KILL_SWITCH")
    assert len(read(tmp_path / "outbox.jsonl")[1]) == 1 and len(synced) == created, "readable, not yet synced"
    box.commit()
    box.commit()
    assert len(synced) == created + 1, "one sync for what was written, none when nothing was"
    box.close()


def test_an_entry_that_is_not_what_it_says_it_is_cannot_be_read_back():
    good = order_fields(an_order(Side.BUY, 1, TimeInForce.DAY, 0, None))
    for damage in ({"quantity": 0}, {"quantity": 1.5}, {"side": "LONG"}, {"limit_price": 5}, {"pair": "EURUSD"}):
        with pytest.raises(OutboxError):
            order_from({**good, **damage, "i": 3})
    with pytest.raises(OutboxError, match="entry 7"):
        fill_from({"i": 7, "fill_id": "x1"})
