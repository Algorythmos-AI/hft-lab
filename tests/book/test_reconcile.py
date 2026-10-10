"""Reconciliation: a missed fill is booked once, and a position that cannot be explained is a break until it can."""
from __future__ import annotations

import pytest

from hftlab.book import DESK, Book, BreakEvent, Difference, Reconciler, Reported, differences
from hftlab.core import Fill, InstrumentKind, Side
from hftlab.journal import Journal, read, verify
from hftlab.venues.port import VenuePosition
from tests.venues.kit import ASK, BID, EURUSD, EURUSD_CFD, MS, SECOND, T0, USDJPY, a_broker, after, an_order, of

GRACE = 5 * SECOND
CASH, CFD = InstrumentKind.CASH, InstrumentKind.CFD


class Rig:
    """A fake broker, a book fed by hand, and a reconciler between them."""

    def __init__(self, folder, **options) -> None:
        self.clock, self.broker = a_broker()
        self.book = Book([EURUSD, USDJPY])
        self.path = folder / "reconcile.jsonl"
        self.heard: list[object] = []
        self.options = options
        self.reconciler = self.build()

    def build(self) -> Reconciler:
        return Reconciler(
            self.broker, self.book, Journal(self.path), clock=self.clock, grace_ns=GRACE, sink=self.sink, **self.options
        )

    def sink(self, event: object) -> None:
        self.heard.append(event)
        self.book.take(event)

    def trade(self, number: int = 1, side: Side = Side.BUY, quantity: int = 20_000, *, booked: bool = True) -> list:
        """An order filled at the venue. Its events reach the book, or (booked=False) are lost on the way."""
        self.broker.place(an_order(number, side=side, quantity=quantity), EURUSD)
        events = self.broker.poll() + after(self.clock, self.broker, 50 * MS)
        if booked:
            for event in events:
                self.book.take(event)
        return events

    def kinds(self) -> list[str]:
        return [str(entry["type"]) for entry in read(self.path)[1]]

    def breaks(self) -> list[BreakEvent]:
        return [event for event in self.heard if isinstance(event, BreakEvent)]


# ---- executions -----------------------------------------------------------------------------------------------------


def test_when_the_book_holds_what_the_venue_holds_nothing_is_found_and_nothing_is_written(tmp_path):
    rig = Rig(tmp_path)
    rig.trade(1)
    rig.trade(2, Side.SELL, 40_000)
    found = rig.reconciler.check()
    assert found.asked and found.recovered == () and found.differences == () and not found.broken
    assert found.event is None and rig.heard == [] and rig.kinds() == []


def test_a_fill_the_book_never_saw_is_booked_from_the_venues_executions_with_its_commission(tmp_path):
    rig = Rig(tmp_path)
    rig.trade(1, booked=False)  # the fill happened; its report never arrived
    assert rig.book.position(DESK, "EUR.USD").quantity == 0
    found = rig.reconciler.check()
    assert [(f.side, f.quantity, f.price) for f in found.recovered] == [(Side.BUY, 20_000, ASK)]
    assert rig.book.position(DESK, "EUR.USD").quantity == 20_000 and rig.book.fees() == {"USD": 2_000_000}
    assert found.differences == () and not found.broken and rig.kinds() == ["recovered"]
    assert [type(event) for event in rig.heard] == [Fill], "it went through the sink, like a fill that arrived"


def test_a_recovered_fill_is_booked_once_however_often_the_venue_reports_it(tmp_path):
    rig = Rig(tmp_path)
    rig.trade(1, booked=False)
    rig.reconciler.check()
    before = rig.book.snapshot()
    for _ in range(3):
        again = rig.reconciler.check()
        assert again.recovered == () and again.differences == ()
    assert rig.book.snapshot() == before and rig.kinds() == ["recovered"]


def test_a_fill_seen_as_it_happened_and_reported_again_by_the_venue_changes_nothing(tmp_path):
    rig = Rig(tmp_path)
    events = rig.trade(1)
    before = rig.book.snapshot()
    assert rig.reconciler.check().recovered == () and rig.book.snapshot() == before
    for fill in of(events, Fill):  # and a late copy of the report itself
        assert rig.book.apply(fill) is False
    assert rig.book.snapshot() == before and rig.heard == []


def test_only_the_missing_ones_are_recovered_from_a_list_that_holds_both(tmp_path):
    rig = Rig(tmp_path)
    rig.trade(1)
    rig.trade(2, booked=False)
    rig.trade(3)
    rig.trade(4, Side.SELL, 40_000, booked=False)
    found = rig.reconciler.check()
    assert sorted(f.quantity for f in found.recovered) == [20_000, 40_000]
    assert rig.book.position(DESK, "EUR.USD").quantity == 20_000 and found.differences == ()


def test_executions_are_asked_for_from_a_little_before_the_last_one_seen(tmp_path):
    rig = Rig(tmp_path)
    asked: list[int] = []
    since = rig.broker.market.executions

    class Listening:
        def executions_since(self, ts_ns: int):
            asked.append(ts_ns)
            return rig.broker.executions_since(ts_ns)

        def positions(self):
            return rig.broker.positions()

    rig.reconciler = Reconciler(Listening(), rig.book, Journal(rig.path), clock=rig.clock, grace_ns=GRACE)
    rig.reconciler.check()
    rig.trade(1, booked=False)
    rig.reconciler.check()
    rig.clock.advance(60 * SECOND)
    rig.reconciler.check()
    last = since()[-1].at_ns
    assert asked == [0, 0, max(0, last - 5 * 60 * SECOND)], "the default overlap is five minutes"


# ---- positions ------------------------------------------------------------------------------------------------------


def test_a_difference_is_not_a_break_until_it_has_outlived_the_grace_time(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.market.carry(EURUSD, 20_000)  # the venue holds it; no execution explains it
    first = rig.reconciler.check()
    assert first.differences == (Difference("EUR.USD", CASH, 0, 20_000),) and not first.broken and first.event is None
    rig.clock.advance(GRACE - 1)
    assert not rig.reconciler.check().broken and rig.kinds() == ["differing"]
    rig.clock.advance(1)
    opened = rig.reconciler.check()
    assert opened.broken and rig.reconciler.broken
    assert opened.event == BreakEvent(True, T0 + GRACE, first.differences) and rig.breaks() == [opened.event]
    assert rig.kinds() == ["differing", "break_opened"]
    assert read(rig.path)[1][-1]["differences"] == [["EUR.USD", "CASH", 0, 20_000]]
    rig.clock.advance(60 * SECOND)
    assert rig.reconciler.check().event is None and len(rig.breaks()) == 1, "said once, not at every check"


def test_a_position_the_venue_has_and_the_book_does_not_is_a_break(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.market.carry(USDJPY, -40_000)
    rig.reconciler.check()
    rig.clock.advance(GRACE)
    found = rig.reconciler.check()
    assert found.broken and found.differences == (Difference("USD.JPY", CASH, 0, -40_000),)


def test_a_position_the_book_has_and_the_venue_does_not_is_a_break(tmp_path):
    rig = Rig(tmp_path)
    rig.book.apply(Fill("phantom", an_order(9).client_order_id, "EUR.USD", Side.BUY, 20_000, ASK, T0))
    rig.reconciler.check()
    rig.clock.advance(GRACE)
    found = rig.reconciler.check()
    assert found.broken and found.differences == (Difference("EUR.USD", CASH, 20_000, 0),)


def test_a_difference_that_goes_away_within_the_grace_time_was_explained_and_is_no_break(tmp_path):
    rig = Rig(tmp_path)
    events = rig.trade(1, booked=False)
    venue_only = [VenuePosition("EUR.USD", CASH, 20_000)]
    assert differences(rig.book, venue_only) == (Difference("EUR.USD", CASH, 0, 20_000),)
    rig.reconciler = Reconciler(Unexplaining(rig.broker), rig.book, Journal(rig.path), clock=rig.clock, grace_ns=GRACE)
    assert rig.reconciler.check().differences and not rig.reconciler.broken
    rig.clock.advance(GRACE - 1)
    for event in events:
        rig.book.take(event)  # the late report arrives after all
    found = rig.reconciler.check()
    assert found.differences == () and not found.broken and rig.kinds() == ["differing", "explained"]
    rig.clock.advance(10 * GRACE)
    assert not rig.reconciler.check().broken


class Unexplaining:
    """A venue whose execution list is of no help: only its positions are compared."""

    def __init__(self, venue) -> None:
        self.venue = venue

    def executions_since(self, ts_ns: int):
        return ()

    def positions(self):
        return self.venue.positions()


def test_a_break_clears_only_when_the_books_agree_again_and_the_record_says_so(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.market.carry(EURUSD, 40_000)
    rig.reconciler.check()
    rig.clock.advance(GRACE)
    assert rig.reconciler.check().broken
    rig.book.apply(Fill("half", an_order(8).client_order_id, "EUR.USD", Side.BUY, 20_000, ASK, T0))
    rig.clock.advance(SECOND)
    still = rig.reconciler.check()
    assert still.broken and still.event is None, "a smaller difference is still a difference"
    assert still.differences == (Difference("EUR.USD", CASH, 20_000, 40_000),)
    rig.clock.advance(3_600 * SECOND)
    assert rig.reconciler.check().broken, "time alone clears nothing"
    rig.book.apply(Fill("rest", an_order(9).client_order_id, "EUR.USD", Side.BUY, 20_000, ASK, T0))
    cleared = rig.reconciler.check()
    assert not cleared.broken and not rig.reconciler.broken and cleared.differences == ()
    assert cleared.event == BreakEvent(False, rig.clock.now_ns()) and rig.breaks()[-1] == cleared.event
    assert rig.kinds() == ["differing", "break_opened", "break_cleared"] and verify(rig.path).entries == 3


def test_the_flag_does_not_clear_if_the_agreement_cannot_be_written_down(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.broker.market.carry(EURUSD, 20_000)
    rig.reconciler.check()
    rig.clock.advance(GRACE)
    assert rig.reconciler.check().broken
    rig.book.apply(Fill("found", an_order(9).client_order_id, "EUR.USD", Side.BUY, 20_000, ASK, T0))

    def full_disk() -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(rig.reconciler._journal, "flush", full_disk)
    with pytest.raises(OSError):
        rig.reconciler.check()
    assert rig.reconciler.broken and rig.breaks()[-1].broken, "no record an operator can read, no cleared flag"


def test_a_break_survives_a_restart_and_says_so_at_once(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.market.carry(EURUSD, 20_000)
    rig.reconciler.check()
    rig.clock.advance(GRACE)
    rig.reconciler.check()
    rig.heard.clear()
    restarted = rig.build()
    assert restarted.broken and rig.heard == [BreakEvent(True, rig.clock.now_ns())]
    rig.book.apply(Fill("found", an_order(9).client_order_id, "EUR.USD", Side.BUY, 20_000, ASK, T0))
    assert not restarted.check().broken
    rig.heard.clear()
    assert not rig.build().broken and rig.heard == [], "and a cleared one stays cleared"


def test_the_grace_time_is_not_started_again_by_a_restart(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.market.carry(EURUSD, 20_000)
    rig.reconciler.check()
    rig.clock.advance(GRACE - 1)
    restarted = rig.build()
    assert not restarted.check().broken
    rig.clock.advance(1)
    assert restarted.check().broken, "five seconds after the difference was first seen, not after the restart"


def test_with_no_grace_time_a_difference_is_a_break_on_the_spot(tmp_path):
    rig = Rig(tmp_path)
    rig.reconciler = Reconciler(rig.broker, rig.book, Journal(rig.path), clock=rig.clock, grace_ns=0)
    rig.broker.market.carry(EURUSD, 20_000)
    assert rig.reconciler.check().broken


def test_a_clock_set_back_does_not_buy_a_difference_more_time(tmp_path):
    class SetClock:
        at_ns = T0

        def now_ns(self) -> int:
            return self.at_ns

    rig = Rig(tmp_path)
    clock = SetClock()
    rig.broker.market.carry(EURUSD, 20_000)
    rig.reconciler = Reconciler(Unexplaining(rig.broker), rig.book, Journal(rig.path), clock=clock, grace_ns=GRACE)
    rig.reconciler.check()
    clock.at_ns = T0 - 3_600 * SECOND
    assert not rig.reconciler.check().broken
    clock.at_ns = T0 + GRACE
    assert rig.reconciler.check().broken


def test_a_venue_that_cannot_be_asked_changes_nothing_either_way(tmp_path):
    rig = Rig(tmp_path)
    rig.broker.market.carry(EURUSD, 20_000)
    rig.reconciler.check()
    rig.clock.advance(GRACE)
    rig.reconciler.check()
    rig.broker.disconnect()
    blind = rig.reconciler.check()
    assert not blind.asked and blind.broken and blind.recovered == () and blind.event is None
    assert blind.differences == (Difference("EUR.USD", CASH, 0, 20_000),), "what was last seen, not a clean sheet"


# ---- the comparison itself ------------------------------------------------------------------------------------------


def test_positions_are_compared_per_pair_and_form_and_a_form_the_book_does_not_trade_is_a_difference():
    book = Book([EURUSD, USDJPY])
    book.apply(Fill("a", an_order(1).client_order_id, "EUR.USD", Side.BUY, 20_000, ASK, T0))
    agree = [VenuePosition("EUR.USD", CASH, 20_000)]
    assert differences(book, agree) == () and differences(book, [*agree, VenuePosition("USD.JPY", CASH, 0)]) == ()
    assert differences(book, []) == (Difference("EUR.USD", CASH, 20_000, 0),)
    as_cfd = [VenuePosition("EUR.USD", CFD, 20_000)]
    assert differences(book, as_cfd) == (Difference("EUR.USD", CASH, 20_000, 0), Difference("EUR.USD", CFD, 0, 20_000))
    unknown = [*agree, VenuePosition("GBP.USD", CASH, -5_000)]
    assert differences(book, unknown) == (Difference("GBP.USD", CASH, 0, -5_000),)
    split = [VenuePosition("EUR.USD", CASH, 15_000), VenuePosition("EUR.USD", CASH, 5_000)]
    assert differences(book, split) == (), "two lines of one pair and form are one position"
    assert EURUSD_CFD.pair == EURUSD.pair


def test_it_is_the_desk_position_that_is_compared_whoever_the_sleeves_are():
    book = Book([EURUSD])
    for number, (sleeve, side) in enumerate([("control", Side.BUY), ("ml", Side.SELL)]):
        from hftlab.core import client_order_id

        order_id = client_order_id(sleeve=sleeve, pair="EUR.USD", side=side, decision_seq=number)
        book.apply(Fill(f"f{number}", order_id, "EUR.USD", side, 20_000, BID, T0))
    assert differences(book, []) == (), "one sleeve long and one short is nothing at the venue"


def test_a_fill_the_book_cannot_take_is_recorded_and_shows_up_as_the_difference_it_is(tmp_path):
    rig = Rig(tmp_path)
    rig.book = Book([USDJPY])  # a book that was never given EUR.USD
    rig.reconciler = Reconciler(rig.broker, rig.book, Journal(rig.path), clock=rig.clock, grace_ns=0)
    rig.trade(1, booked=False)
    found = rig.reconciler.check()
    assert found.recovered == () and found.broken and rig.kinds() == ["unbookable", "differing", "break_opened"]
    assert found.differences == (Difference("EUR.USD", CASH, 0, 20_000),)


def test_what_the_reconciler_asks_of_a_venue_has_nothing_in_it_that_trades():
    public = {name for name in dir(Reported) if not name.startswith("_")}
    assert public == {"positions", "executions_since"}
    with pytest.raises(ValueError):
        Reconciler(None, Book([EURUSD]), Journal("unused"), clock=None, grace_ns=-1)
