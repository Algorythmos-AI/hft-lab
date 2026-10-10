"""The accounting invariants, for any session: generated orders, generated faults, kills and restarts in between.

  * cash is conserved, and a position is the sum of its fills: the books equal what the broker executed;
  * each fill is booked exactly once, however often and in whatever order it was reported;
  * no order the gate allowed can take a sleeve or the desk past its position limit, measured at the broker,
    with every order still working there counted as if it filled in full.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.book import DESK
from hftlab.core import Side, TimeInForce, sleeve_of
from hftlab.venues.fake import FakeConfig
from hftlab.venues.port import ConnectionState
from tests.exec.kit import MS, SECOND, T0, WIDE, Floor
from tests.faults.kit import consistent
from tests.risk.kit import KILL

SLEEVE_CAP, PAIR_CAP = 60_000, 100_000
CAPPED = {
    **WIDE,
    "per_pair/EUR.USD/max_position": PAIR_CAP, "per_sleeve/control/max_position": SLEEVE_CAP,
    "per_sleeve/ml/max_position": SLEEVE_CAP, "per_pair/EUR.USD/max_order_size": 60_000,
}  # fmt: skip
UP = (ConnectionState.RECONCILING, ConnectionState.READY)

ORDER = st.tuples(
    st.just("order"), st.sampled_from(Side), st.sampled_from([20_000, 40_000, 60_000]),
    st.sampled_from(["control", "ml"]), st.sampled_from(["market", "market", "rest", "ioc", "fok"]),
)  # fmt: skip
WAIT = st.tuples(st.just("wait"), st.one_of(st.integers(0, 120), st.integers(0, 120), st.just(1_500)))
WITHDRAW = st.tuples(st.just("withdraw"), st.integers(0, 30))
RARE = st.sampled_from([("restart",), ("check",), ("check",), ("switch",)])
ACTION = st.one_of(ORDER, ORDER, ORDER, ORDER, ORDER, ORDER, WAIT, WAIT, WAIT, WITHDRAW, RARE)
OUTAGES = st.one_of(
    st.just(()),
    st.tuples(st.integers(20, 400), st.integers(50, 3_000)).map(
        lambda span: ((T0 + span[0] * MS, T0 + (span[0] + span[1]) * MS),)
    ),
)
CONFIG = st.builds(
    FakeConfig,
    fill_chunks=st.integers(1, 3),
    chunk_gap_ns=st.sampled_from([0, 10 * MS]),
    respect_displayed_size=st.booleans(),
    duplicate_every=st.integers(0, 3),
    reorder_every=st.integers(0, 3),
    late_ack_every=st.integers(0, 2),
    echo_every=st.integers(0, 3),
    outages=OUTAGES,
)


def within_limits_at_the_broker(floor: Floor) -> None:
    """The broker's own records, not the gate's: position plus everything still working, each way, by scope."""
    for scope, cap in (("control", SLEEVE_CAP), ("ml", SLEEVE_CAP), (DESK, PAIR_CAP)):
        held = buys = sells = 0
        for order_id, booked in floor.market._orders.items():
            if booked.order.pair != "EUR.USD" or scope not in (DESK, sleeve_of(order_id)):
                continue
            held += booked.order.side.sign * booked.executed
            if booked.open and booked.order.side is Side.BUY:
                buys += booked.remaining
            elif booked.open:
                sells += booked.remaining
        assert held + buys <= cap and held - sells >= -cap, (scope, held, buys, sells)


def come_to_rest(floor: Floor) -> None:
    """Wait out any outage, reconcile, take back whatever still works, and let the reconciler have a last look."""
    over = max((end for _, end in floor.market.config.outages), default=T0)
    while floor.clock.now_ns() <= over:
        floor.pump(SECOND)
    for _ in range(200):
        if floor.settle(tries=3):
            break
        floor.pump(SECOND)
    else:
        raise AssertionError("the session could not be reconciled at its end")
    for order_id in floor.oms.in_flight():
        floor.oms.withdraw(order_id)
    floor.pump(SECOND)
    floor.pump(SECOND)
    assert floor.settle()
    floor.reconciler.check()


def run(floor: Floor, actions, stats: dict[str, int] | None = None) -> list[str]:
    """Play a generated session, checking the broker's side of the position limits after every action."""
    sent: list[str] = []
    for number, action in enumerate(actions, 1):
        kind = action[0]
        if kind == "order":
            _, side, quantity, sleeve, how = action
            limit = {"rest": 108_400 if side is Side.BUY else 108_600}.get(how)
            tif = {"ioc": TimeInForce.IOC, "fok": TimeInForce.FOK}.get(how, TimeInForce.DAY)
            order = floor.order(number, side, quantity, sleeve=sleeve, limit=limit, tif=tif)
            proposal = floor.oms.propose(order)
            if proposal.sent:
                sent.append(order.client_order_id)
            if stats is not None:
                stats[proposal.code] = stats.get(proposal.code, 0) + 1
            floor.pump(2 * MS)
        elif kind == "wait":
            floor.pump(action[1] * MS)
            for _ in range(3):  # what a trader's loop does whenever it finds itself blind
                if floor.oms.blind and floor.oms.reconcile():
                    floor.pump()
                floor.pump(MS)
        elif kind == "withdraw" and sent:
            floor.oms.withdraw(sent[action[1] % len(sent)])
        elif kind == "restart":
            floor.restart(up=False)  # killed between two steps; the broker goes on without it
            floor.connect()
        elif kind == "check":
            floor.reconciler.check()
        elif kind == "switch":
            floor.switched ^= {KILL}
        within_limits_at_the_broker(floor)
    return sent


@settings(max_examples=150)
@given(config=CONFIG, actions=st.lists(ACTION, min_size=15, max_size=50), rejected=st.integers(0, 4))
def test_whatever_is_ordered_and_whatever_goes_wrong_the_books_are_right_and_no_limit_is_breached(
    tmp_path_factory, config, actions, rejected
):
    def refuse(order) -> str | None:  # the broker rejects a fixed share of the orders, chosen by their ids
        return "not this one" if int(order.client_order_id[-1], 16) < rejected else None

    floor = Floor(tmp_path_factory.mktemp("floor"), config, changes=CAPPED, reject=refuse)
    sent = run(floor, actions)
    come_to_rest(floor)
    within_limits_at_the_broker(floor)
    assert floor.oms.faults == (), "nothing the fake broker does to its reports is a fault"
    assert set(sent) <= set(floor.at_broker())
    consistent(floor)


def test_the_generated_sessions_do_trade_fill_break_and_restart(tmp_path):
    """A check on the test above: a session of its kind is not a string of refusals with nothing behind them."""
    config = FakeConfig(
        fill_chunks=2, chunk_gap_ns=10 * MS, duplicate_every=2, reorder_every=3, late_ack_every=2, echo_every=3,
        outages=((T0 + 300 * MS, T0 + 900 * MS),),
    )  # fmt: skip
    floor = Floor(tmp_path, config, changes=CAPPED)
    sides = (Side.BUY, Side.BUY, Side.SELL)
    actions = []
    for number in range(40):
        actions.append(("order", sides[number % 3], 20_000 * (1 + number % 3), ("control", "ml")[number % 2],
                        ("market", "rest", "ioc", "fok")[number % 4]))  # fmt: skip
        actions.append(("wait", 30 + number))
        if number in (12, 30):
            actions.append(("restart",))
        if number % 9 == 8:
            actions.append(("withdraw", number))
    stats: dict[str, int] = {}
    sent = run(floor, actions, stats)
    come_to_rest(floor)
    assert len(sent) >= 15 and len(floor.market.executions()) >= 15, stats
    assert {"ALLOW", "POSITION_LIMIT", "NOT_RECONCILED"} <= set(stats), stats
    assert floor.starts == 3 and floor.kinds().count("reconciled") >= 3
    assert {t.stage.value for t in floor.oms._orders.values()} >= {"FILLED", "CANCELLED"}
    assert floor.oms.faults == ()
    consistent(floor)
