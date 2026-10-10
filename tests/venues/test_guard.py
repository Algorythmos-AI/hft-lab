"""What every venue guarantees, proven on the fake broker: no order without a checked, reconciled connection."""
from __future__ import annotations

import ast
import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.core import InstrumentKind, OrderEvent, OrderState, Side, SimClock
from hftlab.safety import PaperGate, PaperOnlyError
from hftlab.safety.gate import Permit
from hftlab.venues.fake import FakeBroker, FakeConfig, build
from hftlab.venues.guard import GuardedVenue
from hftlab.venues.port import (
    ConnectionEvent,
    ConnectionState,
    NotReady,
    OrderRefused,
    UnknownInstrument,
    UnknownOrder,
    VenueError,
    VenueUnavailable,
)
from hftlab.venues.supervisor import Backoff
from tests.support import called_name, imports, sources
from tests.venues.kit import (
    EURUSD,
    EURUSD_CFD,
    INSTRUMENTS,
    MS,
    SECOND,
    T0,
    USDJPY,
    a_broker,
    after,
    an_order,
    brief,
    of,
)

S = ConnectionState
LIVE = ("U7654321",)


def states(events) -> list[str]:
    return [e.state.name for e in of(events, ConnectionEvent)]


# ---- the connection states ------------------------------------------------------------------------------------------


def test_a_new_venue_is_disconnected_and_does_nothing():
    _, broker = a_broker(ready=False)
    assert broker.state is S.DISCONNECTED and broker.poll() == []
    with pytest.raises(NotReady):
        broker.place(an_order(), EURUSD)
    with pytest.raises(NotReady):
        broker.cancel(an_order().client_order_id)
    for query in (broker.open_orders, broker.positions, broker.managed_accounts, broker.server_time_ns):
        with pytest.raises(VenueUnavailable):
            query()
    with pytest.raises(VenueUnavailable):
        broker.instrument("EUR.USD", InstrumentKind.CASH)


def test_a_connection_is_reconciling_until_the_caller_says_it_has_reconciled():
    _, broker = a_broker(ready=False)
    broker.connect()
    assert broker.state is S.RECONCILING and states(broker.poll()) == ["CONNECTING", "RECONCILING"]
    instrument = broker.instrument("EUR.USD", InstrumentKind.CASH)
    with pytest.raises(NotReady, match="RECONCILING"):
        broker.place(an_order(), instrument)
    assert broker.open_orders() == () and broker.positions() == ()  # looking is allowed
    with pytest.raises(UnknownOrder):
        broker.cancel(an_order().client_order_id)  # and so is cancelling: there is just nothing to cancel
    broker.mark_reconciled()
    assert broker.state is S.READY and states(broker.poll()) == ["READY"]
    broker.place(an_order(), instrument)
    assert brief(broker.poll()) == [("order", "SUBMITTED", 0)]


def test_connecting_twice_and_reconciling_twice_change_nothing():
    _, broker = a_broker()
    broker.connect()
    broker.mark_reconciled()
    assert broker.state is S.READY and broker.poll() == []


def test_there_is_nothing_to_reconcile_without_a_connection():
    _, broker = a_broker(ready=False)
    with pytest.raises(NotReady):
        broker.mark_reconciled()


def test_disconnecting_on_request_stays_disconnected():
    clock, broker = a_broker(backoff=Backoff())
    broker.disconnect()
    assert broker.state is S.DISCONNECTED
    for _ in range(5):
        assert states(after(clock, broker, 100 * SECOND)) in (["DISCONNECTED"], [])
    with pytest.raises(NotReady):
        broker.place(an_order(), EURUSD)


# ---- stage one and stage two of the lock ---------------------------------------------------------------------------


class Counting(FakeBroker):
    """A fake broker that counts how often the transport was opened and what was sent."""

    opened = 0

    def _open(self):
        self.opened += 1
        return super()._open()


def counting(environ=None, config=None) -> tuple[SimClock, Counting]:
    clock = SimClock(T0)
    config = config or FakeConfig()
    gate = PaperGate(port=config.port, env={"MODE": "paper"} if environ is None else environ)
    return clock, Counting(clock, build(clock, INSTRUMENTS, config).market, gate=gate, backoff=Backoff())


@pytest.mark.parametrize(
    ("environ", "port"),
    [({"MODE": "live"}, 4002), ({}, 4002), ({"MODE": "paper", "LIVE_TRADING_ENABLED": "1"}, 4002),
     ({"MODE": "paper"}, 4001), ({"MODE": "paper"}, 7496), ({"MODE": "paper"}, 5000)],
)  # fmt: skip
def test_stage_one_refuses_before_the_transport_is_touched(environ, port):
    _, broker = counting(environ, FakeConfig(port=port))
    with pytest.raises(PaperOnlyError):
        broker.connect()
    assert broker.opened == 0 and broker.state is S.REFUSED
    assert states(broker.poll()) == ["REFUSED"]


@pytest.mark.parametrize("accounts", [LIVE, ("DU1234567", "U7654321"), (), ("DF1234567",), ("du1234567",)])
def test_stage_two_refuses_a_connection_whose_accounts_are_not_all_paper(accounts):
    _, broker = counting(config=FakeConfig(accounts=accounts))
    with pytest.raises(PaperOnlyError):
        broker.connect()
    assert broker.opened == 1 and broker.state is S.REFUSED and broker._link is False
    events = broker.poll()
    assert states(events) == ["CONNECTING", "REFUSED"]
    assert "7654321" not in of(events, ConnectionEvent)[-1].reason


def test_a_refusal_is_terminal_nothing_retries_and_nothing_is_sent():
    clock, broker = counting(config=FakeConfig(accounts=LIVE))
    with pytest.raises(PaperOnlyError):
        broker.connect()
    broker.poll()
    for _ in range(10):
        assert after(clock, broker, 120 * SECOND) == []
    assert broker.opened == 1
    commands = [
        broker.connect,
        broker.managed_accounts,
        broker.open_orders,
        lambda: broker.place(an_order(), EURUSD),
        lambda: broker.cancel("hft-control-0"),
        lambda: broker.instrument("EUR.USD", InstrumentKind.CASH),
    ]
    for command in commands:
        with pytest.raises(PaperOnlyError):
            command()
    assert not broker.market.holds(an_order().client_order_id)
    broker.disconnect()
    assert broker.state is S.REFUSED


# ---- stage three: before every order -------------------------------------------------------------------------------


def test_the_lock_is_checked_again_before_every_order():
    environ = {"MODE": "paper"}
    _, broker = a_broker(environ=environ)
    broker.place(an_order(1), EURUSD)
    environ["LIVE_TRADING_ENABLED"] = "true"
    with pytest.raises(PaperOnlyError, match="LIVE_TRADING_ENABLED"):
        broker.place(an_order(2), EURUSD)
    assert broker.state is S.REFUSED and broker._link is False
    assert broker.market.holds(an_order(1).client_order_id) and not broker.market.holds(an_order(2).client_order_id)
    environ.pop("LIVE_TRADING_ENABLED")
    with pytest.raises(PaperOnlyError):
        broker.place(an_order(3), EURUSD)


def test_the_lock_is_checked_before_a_cancel_too():
    environ = {"MODE": "paper"}
    _, broker = a_broker(environ=environ)
    broker.place(an_order(1, limit=100_000), EURUSD)
    environ["MODE"] = "live"
    with pytest.raises(PaperOnlyError):
        broker.cancel(an_order(1).client_order_id)
    assert broker.state is S.REFUSED


def test_an_account_list_that_turns_live_stops_the_next_order():
    _, broker = a_broker()
    broker.market.config = dataclasses.replace(broker.market.config, accounts=("DU1234567", "U7654321"))
    with pytest.raises(PaperOnlyError, match="live account id"):
        broker.place(an_order(), EURUSD)
    assert broker.state is S.REFUSED and not broker.market.holds(an_order().client_order_id)


# ---- reconnection: a new connection starts unchecked ---------------------------------------------------------------


def outage(start_ms: int, end_ms: int) -> FakeConfig:
    return FakeConfig(outages=((T0 + start_ms * MS, T0 + end_ms * MS),))


def test_no_order_is_sent_while_disconnected():
    clock, broker = a_broker(outage(10, 5_000), backoff=Backoff())
    assert states(after(clock, broker, 20 * MS)) == ["DISCONNECTED"]
    with pytest.raises(NotReady, match="DISCONNECTED"):
        broker.place(an_order(), EURUSD)
    with pytest.raises(NotReady):
        broker.cancel(an_order().client_order_id)
    assert not broker.market.holds(an_order().client_order_id)


def test_a_reconnect_passes_the_lock_again_and_waits_to_be_reconciled():
    clock, broker = a_broker(outage(10, 500), backoff=Backoff())
    old_permit = broker._permit
    after(clock, broker, 20 * MS)
    assert broker._permit is None
    assert states(after(clock, broker, 2 * SECOND)) == ["CONNECTING", "RECONCILING"]
    assert broker._permit is not None and broker._permit is not old_permit
    with pytest.raises(NotReady, match="RECONCILING"):
        broker.place(an_order(), EURUSD)
    broker.mark_reconciled()
    broker.place(an_order(), EURUSD)
    assert broker.market.holds(an_order().client_order_id)


def test_a_reconnect_to_a_live_account_is_refused_for_good():
    clock, broker = a_broker(outage(10, 500), backoff=Backoff())
    after(clock, broker, 20 * MS)
    broker.market.config = dataclasses.replace(broker.market.config, accounts=LIVE)
    assert states(after(clock, broker, 2 * SECOND)) == ["CONNECTING", "REFUSED"]
    with pytest.raises(PaperOnlyError):
        broker.place(an_order(), EURUSD)
    assert after(clock, broker, 600 * SECOND) == []


def test_reconnection_follows_the_backoff_schedule():
    clock, broker = a_broker(outage(10, 20_000), backoff=Backoff())
    after(clock, broker, 20 * MS)
    attempts = []
    for _ in range(40_000 // 250):
        if "CONNECTING" in states(after(clock, broker, 250 * MS)):
            attempts.append((clock.now_ns() - T0) // SECOND)
    assert attempts == [1, 3, 7, 15, 31]  # 1 s, then 2, 4, 8 and 16 s after each failure
    assert broker.state is S.RECONCILING


def test_without_a_backoff_a_lost_connection_stays_lost():
    clock, broker = a_broker(outage(10, 500))
    after(clock, broker, 20 * MS)
    assert after(clock, broker, 600 * SECOND) == [] and broker.state is S.DISCONNECTED
    broker.connect()
    assert broker.state is S.RECONCILING


def test_a_failed_first_connection_raises_and_is_retried_by_poll():
    clock = SimClock(T0)
    broker = build(clock, INSTRUMENTS, outage(0, 1_500), backoff=Backoff(),
                   gate=PaperGate(port=4002, env={"MODE": "paper"}))  # fmt: skip
    with pytest.raises(VenueUnavailable):
        broker.connect()
    assert states(broker.poll()) == ["CONNECTING", "DISCONNECTED"]
    assert states(after(clock, broker, 1 * SECOND)) == ["CONNECTING", "DISCONNECTED"]
    assert states(after(clock, broker, 2 * SECOND)) == ["CONNECTING", "RECONCILING"]


def test_subscriptions_survive_a_reconnect():
    clock, broker = a_broker(outage(10, 500), backoff=Backoff())
    broker.market.script_quote(T0 + 200 * MS, "EUR.USD", 108_550, 108_560)  # during the outage: never seen
    broker.market.script_quote(T0 + 5 * SECOND, "EUR.USD", 108_600, 108_610)
    assert states(after(clock, broker, 20 * MS)) == ["DISCONNECTED"]
    assert states(after(clock, broker, 2 * SECOND)) == ["CONNECTING", "RECONCILING"]
    assert brief(after(clock, broker, 4 * SECOND)) == [("quote", "EUR.USD", 108_600, 108_610)]


# ---- what place() refuses before anything is sent ------------------------------------------------------------------


def test_an_order_must_fit_an_instrument_this_venue_looked_up():
    _, broker = a_broker()
    stranger = dataclasses.replace(EURUSD, min_order_size=1)  # never returned by this venue's lookup
    with pytest.raises(UnknownInstrument):
        broker.place(an_order(quantity=5_000), stranger)
    with pytest.raises(UnknownInstrument):
        broker.place(an_order(pair="USD.JPY"), USDJPY)  # exists at the venue, but was not looked up here
    with pytest.raises(OrderRefused, match="cannot be sent as"):
        broker.place(an_order(pair="USD.JPY"), EURUSD)
    with pytest.raises(OrderRefused, match="below the minimum"):
        broker.place(an_order(quantity=19_000), EURUSD)
    with pytest.raises(OrderRefused, match="size increment"):
        broker.place(an_order(quantity=20_500), EURUSD)
    with pytest.raises(OrderRefused, match="price increment"):
        broker.place(an_order(limit=108_512), EURUSD)
    assert broker.market.working() == [] and broker.poll() == []


def test_a_client_order_id_is_sent_at_most_once():
    clock, broker = a_broker()
    broker.place(an_order(1), EURUSD)
    with pytest.raises(OrderRefused, match="already sent"):
        broker.place(an_order(1), EURUSD)
    events = after(clock, broker, 50 * MS)
    assert len([e for e in of(events, OrderEvent) if e.state is OrderState.FILLED]) == 1


def test_one_form_per_pair_at_a_time():
    _, broker = a_broker()
    cfd = broker.instrument("EUR.USD", InstrumentKind.CFD)
    assert cfd == EURUSD_CFD
    with pytest.raises(VenueError, match="already subscribed as CASH"):
        broker.subscribe_quotes(cfd)
    broker.unsubscribe_quotes(EURUSD)
    broker.subscribe_quotes(cfd)
    broker.subscribe_quotes(cfd)


def test_every_event_has_the_receive_time_and_the_next_sequence_number():
    clock, broker = a_broker(ready=False)
    broker.connect()
    instrument = broker.instrument("EUR.USD", InstrumentKind.CASH)
    broker.subscribe_quotes(instrument)
    broker.mark_reconciled()
    broker.market.script_quote(T0 + 3 * MS, "EUR.USD", 108_505, 108_515)
    broker.place(an_order(), instrument)
    events = broker.poll() + after(clock, broker, 50 * MS)
    numbers = [e.seq for e in events]
    assert numbers == list(range(1, len(events) + 1)) and len({type(e) for e in events}) >= 5
    times = [getattr(e, "ts_ns", None) or e.recv_ts_ns for e in events]
    assert set(times) == {T0, T0 + 50 * MS} and times == sorted(times)


# ---- the structure that makes it so ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", GuardedVenue.SEALED)
def test_a_subclass_cannot_replace_a_guarded_method(name):
    with pytest.raises(TypeError, match="may not replace the guarded methods"):
        type("Sneaky", (FakeBroker,), {name: lambda self, *a, **k: None})


def test_the_order_path_is_sealed():
    assert {"connect", "place", "cancel", "mark_reconciled", "poll"} <= set(GuardedVenue.SEALED)
    assert all(hasattr(GuardedVenue, name) for name in GuardedVenue.SEALED)


def test_sending_directly_without_the_current_permit_goes_nowhere():
    clock, broker = a_broker(outage(10, 500), backoff=Backoff())
    stale = broker._permit
    after(clock, broker, 20 * MS)
    after(clock, broker, 2 * SECOND)  # reconnected: a new permit
    with pytest.raises(VenueUnavailable):
        broker._send(an_order(), EURUSD, stale)
    with pytest.raises(PaperOnlyError):
        broker._send(an_order(), EURUSD, Permit(broker._gate, 99, object()))
    assert not broker.market.holds(an_order().client_order_id)


def calls(tree: ast.AST, names: set[str]) -> list[str]:
    return [called_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call) and called_name(n) in names]


def test_only_the_guard_calls_send_and_only_the_gate_mints_permits():
    """A scan of the package: one caller of `_send` and `_cancel`, one constructor of `Permit`."""
    for rel, tree in sources():
        if rel != "src/hftlab/venues/guard.py":
            assert calls(tree, {"_send", "_cancel"}) == [], f"{rel} calls the send path directly"
        if rel != "src/hftlab/safety/gate.py":
            assert calls(tree, {"Permit"}) == [], f"{rel} builds a permit"
        if rel != "src/hftlab/venues/fake/broker.py":
            assert calls(tree, {"submit", "request_cancel"}) == [], f"{rel} reaches the fake market's order entry"
    guard = dict(sources())["src/hftlab/venues/guard.py"]
    assert calls(guard, {"_send", "_cancel"}) == ["_send", "_cancel"]


def called_on(tree: ast.AST, names: set[str]) -> list[str]:
    """What each call to one of `names` is made on, as written: `self._venue.place`."""
    found = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call) and called_name(n) in names]
    return sorted(found)


def test_only_the_order_manager_places_or_cancels_on_a_venue_the_platform_trades_on():
    """A scan of the package for every call named `place`, `cancel` or `mark_reconciled`.

    The order manager makes one of each, on the venue it was built with. The two other callers were there
    before it and neither can reach the trader's venue: the shadow runner builds its own shadow venue and can
    be handed no other (tests/shadow/test_structure.py), and the capability probe is a command the owner runs,
    which places nothing unless asked and cancels what it placed (tests/venues/test_probe.py). Anything else
    that calls one of the three fails here, whatever it calls it on.
    """
    order_calls = {rel: called_on(tree, {"place", "cancel"}) for rel, tree in sources()}
    assert {rel: found for rel, found in order_calls.items() if found} == {
        "src/hftlab/exec/manager.py": ["self._venue.cancel", "self._venue.place"],
        "src/hftlab/shadow/runner.py": ["self._venue.cancel", "self._venue.place"],
        "src/hftlab/venues/ibkr/probe.py": ["self.venue.cancel", "self.venue.cancel", "self.venue.place"],
    }
    marks = {rel: called_on(tree, {"mark_reconciled"}) for rel, tree in sources()}
    assert {rel: found for rel, found in marks.items() if found} == {
        "src/hftlab/exec/manager.py": ["self._venue.mark_reconciled"],
        "src/hftlab/shadow/runner.py": ["self._venue.mark_reconciled"],
        "src/hftlab/venues/ibkr/probe.py": ["venue.mark_reconciled"],
    }


def test_the_order_manager_reaches_its_venue_by_one_name_and_nothing_by_a_computed_one():
    package = [(rel, tree) for rel, tree in sources() if rel.startswith("src/hftlab/exec/")]
    assert [rel.rsplit("/", 1)[-1] for rel, _ in package] == ["__init__.py", "manager.py", "outbox.py", "states.py"]
    bound = []
    for rel, tree in package:
        assert calls(tree, {"getattr", "setattr", "__import__", "import_module"}) == [], rel
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else []
            names = [n.attr for target in targets for n in ast.walk(target) if isinstance(n, ast.Attribute)]
            if "_venue" in names:
                bound.append((rel, ast.unparse(node.value)))
        reached = sorted({name for _, name in imports(tree, rel) if name.startswith("hftlab.venues")})
        assert all(name.startswith("hftlab.venues.port.") for name in reached), f"{rel} imports {reached}"
    assert len(bound) == 1 and bound[0][0].endswith("manager.py"), "the venue is the one the manager was built with"
    assert "venue" in bound[0][1].split(",")[0]


# ---- the same guarantee, for any sequence of events ----------------------------------------------------------------

OPERATIONS = ["connect", "reconcile", "place", "cancel", "wait", "long wait", "disconnect", "go live", "back to paper"]


@settings(max_examples=150)
@given(st.lists(st.sampled_from(OPERATIONS), max_size=25), st.booleans())
def test_an_order_reaches_the_broker_only_through_a_ready_checked_connection(operations, with_outage):
    environ = {"MODE": "paper"}
    config = outage(40, 900) if with_outage else FakeConfig()
    clock, broker = a_broker(config, environ=environ, ready=False, backoff=Backoff())
    sent, number, refused = [], 0, False
    for operation in operations:
        state_before = broker.state
        try:
            if operation == "connect":
                broker.connect()
                broker.instrument("EUR.USD", InstrumentKind.CASH)
            elif operation == "reconcile":
                broker.mark_reconciled()
            elif operation == "place":
                number += 1
                broker.place(an_order(number, side=Side.SELL, limit=200_000), EURUSD)
                assert state_before is S.READY and environ["MODE"] == "paper" and not refused
                sent.append(an_order(number, side=Side.SELL, limit=200_000).client_order_id)
            elif operation == "cancel" and sent:
                broker.cancel(sent[-1])
                assert state_before in (S.RECONCILING, S.READY) and environ["MODE"] == "paper"
            elif operation in ("wait", "long wait"):
                after(clock, broker, 30 * MS if operation == "wait" else 3 * SECOND)
            elif operation == "disconnect":
                broker.disconnect()
            elif operation == "go live":
                environ["MODE"] = "live"
            elif operation == "back to paper":
                environ["MODE"] = "paper"
        except PaperOnlyError:
            refused = True
        except (NotReady, VenueUnavailable, UnknownInstrument, UnknownOrder):
            pass
        if refused:
            assert broker.state is S.REFUSED
        assert sorted(booked.order.client_order_id for booked in broker.market._orders.values()) == sorted(sent)
