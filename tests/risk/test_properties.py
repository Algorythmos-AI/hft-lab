"""What holds for any sequence: the same events give the same verdicts, and no allowed order can breach a limit."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hftlab.book import DESK, BreakEvent
from hftlab.core import OrderEvent, OrderState, Side
from tests.risk.kit import ASK, BID, FLATTEN, KILL, MS, SECOND, T0, Desk, an_order

BUY, SELL = Side.BUY, Side.SELL
SIZES = st.sampled_from([20_000, 40_000, 80_000])
SLEEVES = st.sampled_from(["control", "ml"])

# ---- a recorded day replays to the same verdicts -------------------------------------------------------------------

STEP = st.one_of(
    st.tuples(st.just("wait"), st.integers(0, 90_000)),
    st.tuples(st.just("quote"), st.integers(-200, 200), st.integers(0, 6)),
    st.tuples(st.just("order"), st.sampled_from(Side), SIZES, SLEEVES, st.booleans()),
    st.tuples(st.just("fill"), st.integers(0, 50), st.booleans()),
    st.tuples(st.just("end"), st.integers(0, 50), st.sampled_from([OrderState.CANCELLED, OrderState.REJECTED])),
    st.tuples(st.just("switch"), st.sampled_from([KILL, FLATTEN])),
    st.tuples(st.just("flatten")),
    st.tuples(st.just("break"), st.booleans()),
)


def play(folder, steps) -> tuple[list, bytes]:
    """Run a script against a fresh gate. Returns every verdict it gave and the journal it wrote."""
    desk = Desk(folder)
    said: list = []
    allowed: list = []
    mid = (BID + ASK) // 2

    def propose(order) -> None:
        verdict = desk.check(order)
        said.append((order.client_order_id, verdict.outcome.value, verdict.allowed, verdict.code))
        if verdict.allowed:
            allowed.append(order)

    for number, step in enumerate(steps, 1):
        kind = step[0]
        if kind == "wait":
            desk.after(step[1] * MS)
        elif kind == "quote":
            mid = max(1_000, mid + 5 * step[1])
            desk.quote(mid - 5 * step[2], mid + 5)
        elif kind == "order":
            _, side, quantity, sleeve, limited = step
            propose(an_order(number, side, quantity, sleeve=sleeve, limit=mid if limited else None))
        elif kind == "fill" and allowed:
            order = allowed[step[1] % len(allowed)]
            desk.fill(order, mid, order.quantity // 2 if step[2] else order.quantity, report=not step[2])
        elif kind == "end" and allowed:
            desk.end(allowed.pop(step[1] % len(allowed)), step[2])
        elif kind == "switch":
            desk.switched ^= {step[1]}
        elif kind == "flatten":
            for order in desk.gate.flatten_orders():
                propose(order)
        elif kind == "break":
            desk.gate.observe(BreakEvent(step[1], desk.clock.now_ns()))
    said.append(desk.gate.latches())
    return said, (folder / "risk.jsonl").read_bytes()


@settings(max_examples=60)
@given(steps=st.lists(STEP, max_size=40))
def test_the_same_events_clock_and_limits_give_the_same_verdicts_and_the_same_journal(tmp_path_factory, steps):
    first = play(tmp_path_factory.mktemp("first"), steps)
    again = play(tmp_path_factory.mktemp("again"), steps)
    assert first == again, "the gate is a function of what it was shown: a recorded day replays to its verdicts"


# ---- the position limit, against any order of fills and reports -----------------------------------------------------

MOVE = st.one_of(
    st.tuples(st.just("order"), st.sampled_from(Side), SIZES, SLEEVES),
    st.tuples(st.just("execute"), st.integers(0, 50), st.booleans(), st.booleans()),
    st.tuples(st.just("show"), st.integers(0, 50)),
    st.tuples(st.just("close"), st.integers(0, 50)),
    st.tuples(st.just("report"), st.integers(0, 50)),
)
CAPS = {"control": 60_000, "ml": 60_000, DESK: 100_000}
QUIET = {"desk/max_orders_per_minute": 10**6, "per_sleeve/control/max_orders_per_minute": 10**6,
         "per_sleeve/ml/max_orders_per_minute": 10**6, "desk/max_order_to_trade_ratio": 10**6}  # fmt: skip


class AtVenue:
    """The truth about one allowed order: what the venue executed, and how much of that the gate has been shown."""

    def __init__(self, order) -> None:
        self.order, self.executed, self.shown, self.closed = order, 0, 0, False
        self.unshown: list[int] = []

    @property
    def could_still_add(self) -> int:
        return (self.executed if self.closed else self.order.quantity) - self.shown


@settings(max_examples=150)
@given(moves=st.lists(MOVE, max_size=50))
def test_no_allowed_order_can_breach_a_position_limit_however_fills_and_reports_arrive(tmp_path_factory, moves):
    desk = Desk(tmp_path_factory.mktemp("gate"), QUIET)
    orders: list[AtVenue] = []
    for number, move in enumerate(moves, 1):
        kind = move[0]
        chosen = orders[move[1] % len(orders)] if kind != "order" and orders else None
        if kind == "order":
            order = an_order(number, move[1], move[2], sleeve=move[3])
            if desk.check(order).allowed:
                orders.append(AtVenue(order))
        elif chosen is None:
            continue
        elif kind == "execute" and not chosen.closed and chosen.executed < chosen.order.quantity:
            left = chosen.order.quantity - chosen.executed
            size = left // 2 if move[2] and left > 10_000 else left
            chosen.executed += size
            if move[3]:
                chosen.unshown.append(size)  # the venue filled it; the fill has not reached this process yet
            else:
                chosen.shown += size
                desk.fill(chosen.order, ASK, size, report=False)
        elif kind == "show" and chosen.unshown:
            size = chosen.unshown.pop()
            chosen.shown += size
            desk.fill(chosen.order, ASK, size, report=False)
        elif kind == "close" and not chosen.closed:
            chosen.closed = True
            done = chosen.executed == chosen.order.quantity
            desk.end(chosen.order, OrderState.FILLED if done else OrderState.CANCELLED, chosen.executed)
        elif kind == "report" and chosen.executed:  # a report of progress, possibly ahead of the fills
            state = OrderState.FILLED if chosen.executed == chosen.order.quantity else OrderState.PARTIALLY_FILLED
            desk.gate.observe(OrderEvent(chosen.order.client_order_id, state, T0, 0, filled_quantity=chosen.executed))
            chosen.closed = chosen.closed or state is OrderState.FILLED
        for scope, cap in CAPS.items():
            held = desk.gate.book.position(scope, "EUR.USD").quantity
            mine = [o for o in orders if scope in (DESK, o.order.sleeve)]
            buys = sum(o.could_still_add for o in mine if o.order.side is BUY)
            sells = sum(o.could_still_add for o in mine if o.order.side is SELL)
            assert held + buys <= cap and held - sells >= -cap, (scope, held, buys, sells)
            assert abs(held) <= cap


# ---- the clock ------------------------------------------------------------------------------------------------------


class SetClock:
    """A clock a test can set anywhere, backwards included, as a corrected wall clock can be."""

    def __init__(self, at_ns: int) -> None:
        self.at_ns = at_ns

    def now_ns(self) -> int:
        return self.at_ns


def test_after_a_clock_set_back_new_quotes_look_old_until_time_has_caught_up(tmp_path):
    clock = SetClock(T0)
    desk = Desk(tmp_path, clock=clock)
    assert desk.check(an_order(1)).allowed
    clock.at_ns = T0 - 10 * SECOND  # the wall clock is corrected ten seconds back
    desk.quote()  # stamped with the corrected time
    assert desk.code(an_order(2)) == "STALE_QUOTE", "the gate's own time did not go back with it"
    clock.at_ns = T0 - 1
    desk.quote()
    assert desk.check(an_order(3)).allowed, "within the limit of where the gate's time stands"


def test_a_clock_set_back_does_not_reopen_a_spent_minute(tmp_path):
    clock = SetClock(T0)
    roomy = {"per_pair/EUR.USD/max_position": 10**9, "per_sleeve/control/max_position": 10**9}
    desk = Desk(tmp_path, {"per_sleeve/control/max_orders_per_minute": 2, **roomy}, clock=clock)
    assert desk.check(an_order(1)).allowed and desk.check(an_order(2)).allowed
    clock.at_ns = T0 - 3_600 * SECOND
    assert desk.code(an_order(3)) == "ORDER_RATE_SLEEVE"
    clock.at_ns = T0 + 60 * SECOND
    desk.quote()
    assert desk.check(an_order(4)).allowed


def test_after_a_clock_that_jumps_forward_every_quote_is_stale_until_a_new_one_arrives(tmp_path):
    clock = SetClock(T0)
    desk = Desk(tmp_path, clock=clock)
    clock.at_ns = T0 + 3_600 * SECOND
    assert desk.code(an_order(1)) == "STALE_QUOTE"
    desk.quote()
    assert desk.check(an_order(2)).allowed
