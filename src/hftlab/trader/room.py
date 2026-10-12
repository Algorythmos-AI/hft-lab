"""The room on the event tape: a soft limit, a hard limit, and the reserve kept for closing (ADR 0027, addendum).

    0 ............ soft .............. hard ............................ quota
      everything     exits only           closing only: the reserve

  * Below the SOFT limit (`tape.soft_percent` of `tape.quota_bytes`) the trader trades as its stages allow.
  * From the soft limit on it opens nothing: every sleeve is on exits only, by the risk gate's own verdict.
  * From the HARD limit on it closes what it holds, by the gate's FLATTEN orders, and records those closes into
    the reserve. The hard limit is the quota less the reserve.

No entry reaches the reserve. A turn is under a limit when its own line ends at or past it, so an entry is
taken only on a turn whose line ended under the soft limit; and a start is refused unless the hard limit lies
above the soft limit by the sends of the orders the gate lets through at once, which is all such a turn can
still write for its entries. What becomes of orders that were on their way when the hard limit is reached is
counted in the reserve, below.

How the reserve is sized. From the hard limit on, a turn is taken on a beat and on nothing else, with the
newest quote of each pair (`hftlab.trader.tape`). The closing has `tape.flatten_seconds` before the owner is
paged, and the reserve is what the tape can take in that time, the worst of everything at once:

    reserve  =  STARTS x head                              a session's two first lines, for each start
             +  turns  x (step + asked)                    every beat of the time limit
             +  orders x order                             every order the gate can let through in it
             +  the last line

    turns    one a beat for the whole of the time limit, and two for its ends
    step     a turn's line with a quote for every pair and a beat
    asked    the venue's answers when a connection is made safe on that turn: every order that can be listed
             as working, every fill that can be listed, a position for every pair, and for a cash
             account with a float its cash in every currency, twice. What can be listed is what
             the desk's `max_orders_per_minute` lets through in the time limit and the five minutes before
             it, which is how far back the reconciler asks
    orders   what the desk's `max_orders_per_minute` lets through in the time limit, exits, retries and
             every sleeve's own orders alike: the gate counts them all, and refuses the next. And the minute
             before it, for the orders still on their way when the hard limit is reached
    order    its send and its cancel as the order manager saw them, its reports, its fills and commissions

The largest book the limits allow is every sleeve at its cap (the pair's `max_position` or its own, whichever
is less) in `max_open_pairs` pairs: sleeves may hold against each other, so the desk's cap does not make it
smaller. Closing it takes `closes` orders of at most `max_order_size`, each filled in full, and a start is
refused unless `orders` covers them and the deepest line can be closed one order a beat inside the time
limit. So the reserve holds the closing of every book the limits allow, and every retry the rate limit lets
through on top of it.

Three things are not limits and are assumed, each generously for what the fake broker and the adapter send:
no number on a line is larger than 2^63, no text the venue writes into one field is longer than `TEXT`
bytes, and one order has at most `REPORTS_PER_ORDER` reports and `FILLS_PER_ORDER` fills. Where one does not
hold, or the closing takes longer than its time limit, the reserve runs out sooner than this says. Then the
tape stops and the session does not (`hftlab.trader.tape`, `["dark"]`).

Everything here is arithmetic on the two settings files. It reads no tape and no clock.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from hftlab.core import Fill, Liquidity, OrderEvent, OrderState, OrderType, Quote, Side, TimeInForce
from hftlab.risk import RiskConfig
from hftlab.risk.room import HARD, ROOM, SOFT
from hftlab.trader.config import Settings, TraderConfigError
from hftlab.trader.tape import DARK, encode
from hftlab.venues.port import CommissionEvent, ConnectionState

MINUTE: Final = 60 * 1_000_000_000
NAMES: Final = {ROOM: "room", SOFT: "soft", HARD: "hard"}  # a level as the journal and the status write it
BIG: Final = 2**63 - 1  # ENGINEERING ASSUMPTION: no number on a line of the tape is larger
TEXT: Final = 96  # ENGINEERING ASSUMPTION: the bytes of text a venue writes into one field of a report
REPORTS_PER_ORDER: Final = 4  # ENGINEERING ASSUMPTION: the order reports a venue sends about one order
FILLS_PER_ORDER: Final = 4  # ENGINEERING ASSUMPTION: the fills one order is executed in, each with its commission
STARTS: Final = 3  # ENGINEERING ASSUMPTION: the sessions begun inside one closing: the first, and two restarts
LISTED_NS: Final = 5 * MINUTE  # how far back the reconciler asks the venue for executions (hftlab.book.Reconciler)
_ORDER_ID: Final = "hft-" + "s" * 24 + "-" + "0" * 20  # the longest client order id there is (hftlab.core.ids)
_PAIR: Final = "EUR.USD"  # every pair is seven characters
_STATE: Final = max((state.value for state in ConnectionState), key=len)


def _size(row: object) -> int:
    """The bytes of one line of the tape, or of one value in a list on a line with the comma that follows it."""
    return len(json.dumps(row, separators=(",", ":"), ensure_ascii=True)) + 1


def _fill() -> Fill:
    return Fill(
        "f" * TEXT, _ORDER_ID, _PAIR, Side.SELL, BIG, BIG, BIG, Liquidity.TAKER, BIG, "USD", BIG, BIG,
    )  # fmt: skip


_QUOTE: Final = _size(encode(Quote(_PAIR, BIG, BIG, BIG, BIG, BIG, BIG)))
_STATED: Final = OrderState.PARTIALLY_FILLED  # the longest name an order's state has
_REPORT: Final = _size(encode(OrderEvent(_ORDER_ID, _STATED, BIG, BIG, BIG, "b" * TEXT, "r" * TEXT)))
_FILL: Final = _size(encode(_fill()))
_COMMISSION: Final = _size(encode(CommissionEvent("f" * TEXT, BIG, "USD", BIG, BIG)))
_SENT: Final = _size(["sent", "cancel", _ORDER_ID, "uncertain", _STATE])
_OPEN: Final = _size(
    [
        _ORDER_ID, "b" * TEXT, _PAIR, Side.SELL.value, BIG, BIG, OrderType.MARKET.value, BIG, TimeInForce.IOC.value,
        _STATED.value,
    ]
)  # fmt: skip
_POSITION: Final = _size([_PAIR, "CASH", -BIG])
_CASH: Final = _size(["USD", -BIG])  # one currency of the account's cash, as the venue answers (ADR 0031)
_ASK: Final = _size(["ask", "executions_since", _STATE, [], "VenueUnavailable"])
_STOP: Final = _size(["stop", BIG])
_HEAD: Final = 2_048  # a session's `open` and `marks` lines without its instruments: a mark and a hash a journal
_INSTRUMENT: Final = 512  # one instrument as the head describes it


@dataclass(frozen=True, slots=True)
class Limits:
    """Where the limits on the tapes lie, in bytes."""

    soft: int  # from here on nothing is opened
    hard: int  # from here on what is held is closed: the quota less the reserve
    quota: int  # no line is written past this
    reserve: int

    def as_dict(self) -> dict[str, int]:
        return {"soft_bytes": self.soft, "hard_bytes": self.hard, "quota_bytes": self.quota}


def orders(risk: RiskConfig, span_ns: int) -> int:
    """The most orders the risk gate lets through in a span: the desk's limit for every minute begun in it.

    The gate holds the limit over every sixty seconds, wherever they begin. A span is covered by one such
    stretch for every minute begun, so nothing more than this passes, whatever the orders are for.
    """
    return risk.desk.max_orders_per_minute * (span_ns // MINUTE + 1)


def lines(risk: RiskConfig) -> list[list[int]]:
    """For each pair, the orders it takes to close each sleeve's largest line in it, each filled in full."""
    out = []
    for pair in risk.pairs.values():
        caps = [min(pair.max_position, sleeve.max_position) for sleeve in risk.sleeves.values()]
        out.append([-(-cap // pair.max_order_size) for cap in caps])
    return out


def closes(risk: RiskConfig) -> int:
    """The orders it takes to close the largest book the limits allow: every sleeve at its cap, in as many pairs
    as may be open at once, the pairs that take the most orders first."""
    by_pair = sorted((sum(found) for found in lines(risk)), reverse=True)
    return sum(by_pair[: risk.desk.max_open_pairs])


def turns(settings: Settings) -> int:
    """The turns the closing may take before the owner is paged: one a beat, and one at each end."""
    return settings.tape_flatten_ns // settings.beat_ns + 2


def reserve_bytes(settings: Settings, risk: RiskConfig | None) -> int:
    """What is kept free under the quota for closing: the most the tape can take in `tape.flatten_seconds`."""
    pairs = len(settings.pairs)
    switches = [risk.flatten_switch, risk.kill_switch] if risk else ["FLATTEN", "KILL"]
    step = _size(["step", BIG, _STATE, [], [switches, BIG, []], HARD]) + pairs * _QUOTE
    starts = STARTS * (_HEAD + pairs * _INSTRUMENT)
    if risk is None:
        return starts + turns(settings) * step + _STOP + len(DARK)  # nothing can be sent, so nothing is asked
    listed = orders(risk, settings.tape_flatten_ns + LISTED_NS)
    asked = 3 * _ASK + listed * (_OPEN + FILLS_PER_ORDER * _FILL) + pairs * _POSITION
    if risk.inventory is not None:
        # The venue is asked for the account's cash as well: at most the two currencies of every pair, and as
        # often again for what an operator's request about the float asks on the same turn.
        asked += 2 * (_ASK + 2 * pairs * _CASH)
    order = 2 * _SENT + REPORTS_PER_ORDER * _REPORT + FILLS_PER_ORDER * (_FILL + _COMMISSION)
    sent = orders(risk, settings.tape_flatten_ns + MINUTE)
    return starts + turns(settings) * (step + asked) + sent * order + _STOP + len(DARK)


def limits(settings: Settings, risk: RiskConfig | None) -> Limits | None:
    """The limits on the tapes, or None when the settings set no quota. Refused when the quota cannot hold them."""
    quota = settings.tape_quota_bytes
    if quota is None:
        return None
    percent = settings.tape_soft_percent
    reserve = reserve_bytes(settings, risk)
    soft, hard = quota * percent // 100, quota - reserve
    between = orders(risk, 0) * _SENT if risk else 0  # what the entries of one turn can still write: their sends
    if soft < 1 or hard < soft + between:
        least = -(-(reserve + between) * 100 // (100 - percent))
        raise TraderConfigError(
            f"tape.quota_bytes of {quota} cannot hold the reserve. Closing what the limits allow takes up to {reserve} "
            f"bytes of tape, kept free above the soft limit at {percent}%: the quota must be at least {least}. Raise "
            "it, or lower tape.soft_percent, tape.flatten_seconds or the desk's max_orders_per_minute"
        )
    if risk is not None:
        need, sized_for = closes(risk), orders(risk, settings.tape_flatten_ns)
        deepest = max((count for found in lines(risk) for count in found), default=0)
        if need > sized_for or deepest > turns(settings) - 2:
            raise TraderConfigError(
                f"tape.flatten_seconds is too short for the limits: the largest book they allow takes {need} closing "
                f"orders, {deepest} of them one after another, and the time limit has room for {sized_for} orders at "
                f"the desk's max_orders_per_minute and {turns(settings) - 2} beat(s)"
            )
    return Limits(soft, hard, quota, reserve)


def edge_options(settings: Settings, risk: RiskConfig | None) -> dict[str, int]:
    """How `LiveEdge` is told to cut and count the tape, from the settings and the limits in force."""
    found = limits(settings, risk)
    out = {"part_bytes": settings.tape_part_bytes}
    if found is not None:
        out.update(quota_bytes=found.quota, soft_bytes=found.soft, reserve_bytes=found.reserve)
    return out


__all__ = ["NAMES", "Limits", "closes", "edge_options", "limits", "lines", "orders", "reserve_bytes", "turns"]
