"""The capability probe: what the paper account can actually do (roadmap F1, docs/runbooks/ibkr-probe.md).

    python -m hftlab.venues.ibkr.probe --pairs EUR.USD,USD.JPY --window-seconds 30

It connects to a gateway on this machine, in paper mode or not at all: the connection goes through the same
paper-only lock as everything else. It then measures, and writes to stdout and to a file of its own,
var/probe/ibkr-<UTC date>T<UTC time>Z.json (for example ibkr-2026-10-11T213440Z.json):

  * the managed accounts, masked;
  * the gateway's clock against ours;
  * for each pair, whether it resolves as spot (CASH) and as a CFD, with price increment and sizes;
  * how often quotes arrive, the gaps between them and the spread, over a sampling window;
  * whether market depth and historical ticks are there.

By default it places NO orders. With --orders it places one minimum-size limit order per pair, a BUY five per
cent below the bid so that it cannot trade, and waits for the broker's own verdict on it. The gateway
acknowledges an order itself, at once; the broker's servers accept or reject it a moment later. So the probe
waits, for a bounded time, past the gateway's acknowledgement, and reports per pair one of:

  * `accepted_and_working`: acknowledged, and not turned down in the wait. `placed` is true. The order is then
    cancelled and the cancel timed;
  * `rejected`: with the broker's number for it (`reason_code`) and a class from the fixed table in
    `hftlab.venues.ibkr.refusals` (`reason_class`). `placed` is false, and the order, being dead, is not
    cancelled. The broker's own words stay in `notices`, which are for the owner's eyes;
  * `no_answer`: nothing came back in the wait.

`ack_us` is timed to that verdict; `local_ack_us` is the gateway's own first answer. `may_open_position`, at
the top of the report, sums it up: per side tested, the pairs the account may open a position in, the pairs
it was refused, each with its class, and the pairs with no verdict. Whatever happens in between, the probe
cancels in a `finally` and then checks that nothing of its own is left working.

The gateway can be connected to us and cut off from the broker. It then answers no lookup, and says why only
in status messages. The probe reads those from the moment it connects (`hftlab.venues.ibkr.link` lists them),
and when a lookup fails while the latest of them says the link is down, the pair gets that one plain reason,
the report says `upstream_link_down`, no order is placed, and the command exits with status 3.

The paper account gets no market data while the login it belongs to has a session open elsewhere. The gateway
says so in a message of its own (`hftlab.venues.ibkr.link`). The link is up then, and lookups are answered,
but no quote arrives: each pair without quotes gets that one plain reason, the report says
`competing_session`, no order is placed, and the command exits with status 4.

A report is never overwritten. Its name carries the second the probe started, and if a file of that name is
already there the probe prints the report, leaves the file alone and says so.

Exit status: 0 done; 1 an order of the probe's is still working or a position changed; 2 refused, the venue
could not be probed at all, or the report could not be kept because its file already exists; 3 the gateway
was cut off from the broker, so the report measures nothing; 4 the other login's session took the market
data, so the report holds no quotes.

It never overwrites config/venues/ibkr.yaml unless told to with --write-config. Every number in the report is
an integer or decimal text: durations in microseconds, prices in the instrument's own units.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from hftlab.core import (
    Clock,
    Instrument,
    InstrumentKind,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    Quote,
    Side,
    SimClock,
    TimeInForce,
    WallClock,
    client_order_id,
    from_scaled,
    is_ours,
)
from hftlab.core._checks import pair as check_pair
from hftlab.safety.lock import PaperOnlyError
from hftlab.venues.ibkr.link import (
    COMPETING_REASON,
    DOWN_REASON,
    UpstreamLink,
    gateway_code,
    says_competing_session,
)
from hftlab.venues.ibkr.refusals import ORDER_WARNING_CODE, odd_lot_minimum, reason_class
from hftlab.venues.port import (
    ConnectionEvent,
    ConnectionState,
    DepthSnapshot,
    Venue,
    VenueError,
    VenueEvent,
    VenueNotice,
    VenueUnavailable,
)

SCHEMA = 2  # 2: an order's verdict is the broker's, not the gateway's acknowledgement; `may_open_position`
SECOND = 1_000_000_000
SLEEVE = "probe"
KINDS = (InstrumentKind.CASH, InstrumentKind.CFD)
CONFIG_PATH = Path("config/venues/ibkr.yaml")
# What the capability file's `status` may say. The probe proposes `partly_measured` and nothing more: it never
# sees a commission, and it cannot know that it sampled liquid hours. `probed` is set by hand, with a decision.
CONFIG_STATUSES = ("placeholder", "partly_measured", "probed")
EXIT_ATTENTION = 1
EXIT_REFUSED = 2
EXIT_UPSTREAM_DOWN = 3
EXIT_COMPETING_SESSION = 4
# The broker's verdict on one of the probe's orders.
ACCEPTED, REJECTED, NO_ANSWER = "accepted_and_working", "rejected", "no_answer"
SIDE_TESTED = Side.BUY  # the one side the probe's orders take
_ENDED_UNASKED = (OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED)
NOT_MEASURED = (
    "commissions: a commission is only reported on a fill, and the probe never lets its orders fill",
    "weekly login: what happens at the weekly reset has to be watched over a week, not sampled",
    "server clock offset: the gateway answers in whole seconds, so the offset is good to a second at best",
    "the SELL side: the probe's orders are buys, so what the account may sell or short is not shown",
)


@dataclass(frozen=True, slots=True)
class ProbeSettings:
    pairs: tuple[str, ...] = ("EUR.USD", "USD.JPY")
    window_ns: int = 30 * SECOND
    poll_ns: int = SECOND // 20
    depth_levels: int = 5
    depth_window_ns: int = 5 * SECOND
    history_ticks: int = 100
    orders: bool = False
    order_wait_ns: int = 10 * SECOND
    # How long past the gateway's own acknowledgement the broker's servers are given to turn the order down.
    verdict_wait_ns: int = 3 * SECOND
    notice_wait_ns: int = SECOND  # and how long the message that explains a rejection may trail the rejection


# ---- small pure helpers --------------------------------------------------------------------------------------------


def mask(account: str) -> str:
    """An account id with only its letter prefix and last two characters showing: DU1234567 -> DU*****67."""
    prefix = account[: len(account) - len(account.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))]
    hidden = len(account) - len(prefix) - 2
    if hidden <= 0:
        return prefix + "*" * max(len(account) - len(prefix), 0)
    return f"{prefix}{'*' * hidden}{account[-2:]}"


def summary(values: Sequence[int]) -> dict[str, int] | None:
    """Count, minimum, median, 99th percentile and maximum of some integers (nearest rank), or None for none."""
    if not values:
        return None
    ordered = sorted(values)
    last = len(ordered) - 1
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": ordered[last // 2],
        "p99": ordered[(last * 99 + 99) // 100],
        "max": ordered[-1],
    }


def per_second(count: int, duration_ns: int) -> str:
    """A rate as decimal text with two places, by integer arithmetic."""
    if duration_ns <= 0:
        return "0.00"
    return from_scaled(count * SECOND * 100 // duration_ns, 2)


def utc_date(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // SECOND, tz=UTC).date().isoformat()


def utc_stamp(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // SECOND, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_name(prefix: str, probed_at: str) -> str:
    """The report's file name: the venue and the second the probe started, with nothing a file system refuses."""
    return f"{prefix}-{probed_at.replace(':', '')}.json"


def keep(path: Path, text: str) -> bool:
    """Write a new report. False, and nothing written, if a file of that name exists: a report is never replaced."""
    try:
        with path.open("x", encoding="utf-8") as out:
            out.write(text)
    except FileExistsError:
        return False
    return True


def far_below(bid: int, instrument: Instrument) -> int:
    """A buy price five per cent under the bid, on the price grid: far enough that it cannot trade."""
    step = instrument.price_increment
    return max(step, (bid - bid // 20) // step * step)


def smallest_order(instrument: Instrument) -> int:
    step = instrument.size_increment
    return -(-instrument.min_order_size // step) * step


# ---- the probe -----------------------------------------------------------------------------------------------------


class Probe:
    def __init__(self, venue: Venue, clock: Clock, settings: ProbeSettings) -> None:
        self.venue, self.clock, self.settings = venue, clock, settings
        self.notices: list[dict[str, str]] = []
        self.disconnects = 0
        self.last_quote: dict[str, Quote] = {}
        self.link = UpstreamLink()
        self.upstream_down = False  # a lookup failed while the gateway's latest word was that it is cut off
        self.competing = False  # the gateway said the other login's session has the market data
        self.said: dict[str, list[tuple[str, str]]] = {}  # what the venue said about each order: (code, message)
        self._stuck = 0

    def run(self) -> dict[str, Any]:
        """Connect, measure, disconnect. Raises `PaperOnlyError` or `VenueError` if it cannot even connect."""
        venue = self.venue
        venue.connect()
        try:
            self._drain()  # what the gateway said while the connection was being made
            started = self.clock.now_ns()
            accounts = venue.managed_accounts()
            report: dict[str, Any] = {
                "schema": SCHEMA,
                "venue": venue.name,
                "probed_at": utc_stamp(started),
                "accounts": [mask(account) for account in accounts],
                "server_time": self._server_time(),
                "pairs": {pair: self._pair(pair) for pair in self.settings.pairs},
                "orders": None,
            }
            if self.settings.orders and self.upstream_down:
                report["orders"] = {"attempted": False, "placed": False, "reason": DOWN_REASON}
            elif self.settings.orders and self.competing:
                report["orders"] = {"attempted": False, "placed": False, "reason": COMPETING_REASON}
            elif self.settings.orders:
                report["orders"] = self._orders(report["pairs"])
            self._drain()  # a message may trail the event it explains by one poll
            report["may_open_position"] = may_open_position(report["orders"])
            report["disconnects_during_probe"] = self.disconnects
            report["upstream_link_down"] = self.upstream_down
            report["competing_session"] = self.competing
            for notice in self.notices:  # the gateway may name an account in its own words; the report does not
                for account in accounts:
                    notice["message"] = notice["message"].replace(account, mask(account))
            report["notices"] = self.notices
            report["not_measured"] = list(NOT_MEASURED)
            report["proposed_config"] = proposed_config(report, utc_date(started))
            return report
        finally:
            venue.disconnect()

    # ---- pumping ----------------------------------------------------------------------------------------------------

    def _step(self, until_ns: int) -> list[VenueEvent]:
        """One poll of at most the polling interval, never past `until_ns`."""
        now = self.clock.now_ns()
        events = self.venue.poll(max(0, min(self.settings.poll_ns, until_ns - now)))
        self._note(events)
        self._stuck = self._stuck + 1 if self.clock.now_ns() == now and not events else 0
        if self._stuck > 10_000:
            raise VenueError("the clock is not advancing: the probe needs a clock that moves while it waits")
        return events

    def _poll(self, until_ns: int) -> list[VenueEvent]:
        """Everything the venue reports until `until_ns`, remembering notices, disconnects and the last quotes."""
        out: list[VenueEvent] = []
        while self.clock.now_ns() < until_ns:
            out += self._step(until_ns)
        return out

    def _drain(self) -> None:
        """Read what the venue has already queued, without waiting: its messages arrive at any time."""
        self._note(self.venue.poll(0))

    def _note(self, events: Sequence[VenueEvent]) -> None:
        for event in events:
            if isinstance(event, VenueNotice):
                self.notices.append({"code": event.code, "message": event.message})
                self.link.hear(event.code, event.message)
                self.competing = self.competing or says_competing_session(event.code, event.message)
                if event.client_order_id is not None:
                    self.said.setdefault(event.client_order_id, []).append((event.code, event.message))
            elif isinstance(event, ConnectionEvent) and event.state is ConnectionState.DISCONNECTED:
                self.disconnects += 1
            elif isinstance(event, Quote):
                self.last_quote[event.pair] = event

    # ---- measurements -----------------------------------------------------------------------------------------------

    def _server_time(self) -> dict[str, Any]:
        before = self.clock.now_ns()
        server = self.venue.server_time_ns()
        after = self.clock.now_ns()
        return {
            "server": utc_stamp(server),
            "offset_us": (server - (before + after) // 2) // 1_000,
            "round_trip_us": (after - before) // 1_000,
        }

    def _pair(self, pair: str) -> dict[str, Any]:
        forms: dict[str, Any] = {}
        instruments: dict[InstrumentKind, Instrument] = {}
        for kind in KINDS:
            try:
                instrument = self.venue.instrument(pair, kind)
            except VenueError as exc:
                self._drain()  # the gateway may have said why while the request waited
                if isinstance(exc, VenueUnavailable) and self.link.down:
                    return self._cut_off(forms)
                forms[kind.value.lower()] = {"resolves": False, "reason": str(exc)}
                continue
            instruments[kind] = instrument
            forms[kind.value.lower()] = {
                "resolves": True,
                "price_scale": instrument.price_scale,
                "price_increment": from_scaled(instrument.price_increment, instrument.price_scale),
                "min_order_size": instrument.min_order_size,
                "size_increment": instrument.size_increment,
            }
        result: dict[str, Any] = {"forms": forms, "quotes": None, "depth": None, "historical_ticks": None}
        if instruments:
            sampled = instruments.get(InstrumentKind.CASH) or instruments[InstrumentKind.CFD]
            result["sampled_as"] = sampled.kind.value.lower()
            result["quotes"] = self._quotes(sampled)
            result["depth"] = self._depth(sampled)
            result["historical_ticks"] = self._history(sampled)
        return result

    def _cut_off(self, forms: dict[str, Any]) -> dict[str, Any]:
        """A pair whose lookup went unanswered while the gateway was cut off: one reason, and nothing sampled.

        The forms not yet asked for are not asked: each would wait out the same timeout for the same reason.
        """
        self.upstream_down = True
        for kind in KINDS:
            forms.setdefault(kind.value.lower(), {"resolves": False})
        return {"forms": forms, "reason": DOWN_REASON, "quotes": None, "depth": None, "historical_ticks": None}

    def _quotes(self, instrument: Instrument) -> dict[str, Any]:
        self.venue.subscribe_quotes(instrument)
        started = self.clock.now_ns()
        try:
            events = self._poll(started + self.settings.window_ns)
        finally:
            self.venue.unsubscribe_quotes(instrument)
        quotes = [e for e in events if isinstance(e, Quote) and e.pair == instrument.pair]
        window_ns = self.clock.now_ns() - started
        gaps = [(later.recv_ts_ns - earlier.recv_ts_ns) // 1_000 for earlier, later in pairwise(quotes)]
        spreads = [q.spread for q in quotes]
        why = {"reason": COMPETING_REASON} if self.competing and not quotes else {}
        return {
            **why,
            "updates": len(quotes),
            "window_us": window_ns // 1_000,
            "updates_per_second": per_second(len(quotes), window_ns),
            "gap_us": summary(gaps),
            "spread": summary(spreads),
            "spread_mean_x1000": sum(spreads) * 1_000 // len(spreads) if spreads else None,
            "crossed_or_locked": sum(1 for q in quotes if q.spread <= 0),
            "first": self._shown(quotes[0], instrument) if quotes else None,
            "last": self._shown(quotes[-1], instrument) if quotes else None,
        }

    @staticmethod
    def _shown(quote: Quote, instrument: Instrument) -> dict[str, Any]:
        return {
            "bid": instrument.format_price(quote.bid),
            "ask": instrument.format_price(quote.ask),
            "bid_size": quote.bid_size,
            "ask_size": quote.ask_size,
        }

    def _depth(self, instrument: Instrument) -> dict[str, Any]:
        if not self.venue.capabilities.depth:
            return {"available": False, "reason": "the venue offers no market depth"}
        seen_before = len(self.notices)
        try:
            self.venue.subscribe_depth(instrument, self.settings.depth_levels)
        except VenueError as exc:
            return {"available": False, "reason": str(exc)}
        try:
            events = self._poll(self.clock.now_ns() + self.settings.depth_window_ns)
        finally:
            self.venue.unsubscribe_depth(instrument)
        snapshots = [e for e in events if isinstance(e, DepthSnapshot) and e.pair == instrument.pair]
        return {
            "available": bool(snapshots),
            "snapshots": len(snapshots),
            "levels": max((max(len(s.bids), len(s.asks)) for s in snapshots), default=0),
            "notices": [n["code"] for n in self.notices[seen_before:]],
        }

    def _history(self, instrument: Instrument) -> dict[str, Any]:
        if not self.venue.capabilities.historical_quotes:
            return {"available": False, "reason": "the venue offers no historical quotes"}
        try:
            ticks = self.venue.historical_quotes(instrument, self.clock.now_ns(), self.settings.history_ticks)
        except VenueError as exc:
            return {"available": False, "reason": str(exc)}
        return {"available": bool(ticks), "ticks": len(ticks)}

    # ---- the order round trip, only when asked ----------------------------------------------------------------------

    def _orders(self, pairs: dict[str, Any]) -> dict[str, Any]:
        venue = self.venue
        working = venue.open_orders()
        if working:
            reason = f"the account already has {len(working)} working order(s); the probe places none beside them"
            return {"attempted": False, "placed": False, "reason": reason}
        before = self._positions()
        venue.mark_reconciled()
        results: dict[str, Any] = {}
        try:
            for number, pair in enumerate(self.settings.pairs):
                form = pairs[pair].get("sampled_as")
                quote = self.last_quote.get(pair)
                if form is None or quote is None:
                    reason = "no instrument or no quote to price an order from"
                    results[pair] = {"placed": False, "verdict": NO_ANSWER, "reason": reason}
                    continue
                try:
                    instrument = venue.instrument(pair, InstrumentKind(form.upper()))
                    results[pair] = self._round_trip(instrument, quote, number)
                except PaperOnlyError:
                    raise
                except VenueError as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    results[pair] = {"placed": False, "verdict": NO_ANSWER, "reason": reason}
        finally:
            left = self._cancel_everything_of_ours()
        after = self._positions()
        return {
            "attempted": True,
            "placed": any(result["placed"] for result in results.values()),  # the broker accepted at least one
            "side": SIDE_TESTED.value,
            "pairs": results,
            "left_working": left,
            "positions_before": before,
            "positions_after": after,
            "positions_changed": before != after,
        }

    def _positions(self) -> list[dict[str, Any]]:
        return [{"pair": p.pair, "form": p.kind.value.lower(), "quantity": p.quantity} for p in self.venue.positions()]

    def _round_trip(self, instrument: Instrument, quote: Quote, number: int) -> dict[str, Any]:
        now = self.clock.now_ns()
        price, quantity = far_below(quote.bid, instrument), smallest_order(instrument)
        order_id = client_order_id(
            sleeve=SLEEVE, pair=instrument.pair, side=SIDE_TESTED, decision_seq=now // SECOND, attempt=number
        )
        terms = (SIDE_TESTED, quantity, OrderType.LIMIT, TimeInForce.DAY)
        order = Order(order_id, SLEEVE, instrument.pair, *terms, now, price)
        result: dict[str, Any] = {
            "placed": False,
            "verdict": NO_ANSWER,
            "client_order_id": order_id,
            "quantity": quantity,
            "limit_price": instrument.format_price(price),
            "bid_at_the_time": instrument.format_price(quote.bid),
        }
        self.venue.place(order, instrument)
        first = self._wait_for(order_id, {OrderState.WORKING, OrderState.PARTIALLY_FILLED, OrderState.FILLED})
        # Timed to the moment the answer was received, not to the end of the poll that returned it.
        result["local_ack_us"] = None if first is None else (first.ts_ns - now) // 1_000
        result["ack_us"] = None
        if first is None:
            return result
        ended = first if first.state.is_terminal else self._wait_for_verdict(order_id)
        if ended is not None and ended.state in _ENDED_UNASKED:
            # The broker's servers said no, or ended the order by themselves. It is dead: there is nothing to cancel.
            code, found = self._why(order_id, ended)
            result.update(verdict=REJECTED, final_state=ended.state.value, reason_code=code, reason_class=found)
            result["ack_us"] = (ended.ts_ns - now) // 1_000
        else:
            # Acknowledged, and not turned down in the wait: the acknowledgement stands as the verdict.
            result.update(placed=True, verdict=ACCEPTED, ack_us=result["local_ack_us"])
            if ended is None:
                asked = self.clock.now_ns()
                self.venue.cancel(order_id)
                cancelled = self._wait_for(order_id, {OrderState.CANCELLED})
                result["cancel_us"] = None if cancelled is None else (cancelled.ts_ns - asked) // 1_000
                result["state_after_cancelling"] = cancelled.state.value if cancelled else "no answer"
        for code, message in self.said.get(order_id, []):
            minimum = odd_lot_minimum(message) if gateway_code(code) == ORDER_WARNING_CODE else None
            if minimum is not None:  # numbers only: the venue's minimum for good pricing, as its own warning names it
                result["odd_lot_minimum"] = {"currency": minimum[0], "size": minimum[1]}
        return result

    def _wait_for(self, order_id: str, wanted: set[OrderState], wait_ns: int | None = None) -> OrderEvent | None:
        """The first event for `order_id` in a wanted or final state, or None if the wait runs out."""
        deadline = self.clock.now_ns() + (self.settings.order_wait_ns if wait_ns is None else wait_ns)
        while self.clock.now_ns() < deadline:
            for event in self._step(deadline):
                if not isinstance(event, OrderEvent) or event.client_order_id != order_id:
                    continue
                if event.state in wanted or event.state.is_terminal:
                    return event
        return None

    def _wait_for_verdict(self, order_id: str) -> OrderEvent | None:
        """What ended an acknowledged order within the verdict wait, or None: it is still working."""
        return self._wait_for(order_id, set(), self.settings.verdict_wait_ns)

    def _why(self, order_id: str, ended: OrderEvent) -> tuple[str | None, str]:
        """The broker's number for a rejection and its class. The message may arrive a poll after the state."""

        def explained() -> tuple[str, str] | None:
            numbered = [
                (code, message)
                for code, message in self.said.get(order_id, [])
                if gateway_code(code) not in (None, ORDER_WARNING_CODE)
            ]
            return numbered[-1] if numbered else None

        deadline = self.clock.now_ns() + self.settings.notice_wait_ns
        while explained() is None and self.clock.now_ns() < deadline:
            self._step(deadline)
        found = explained()
        if found is None:
            return None, reason_class(None, ended.reason)  # no number arrived: the order's own last word decides
        return found[0], reason_class(gateway_code(found[0]), found[1])

    def _cancel_everything_of_ours(self) -> list[str]:
        """Cancel whatever of the probe's is still working, then return the ids that are STILL working."""

        def ours() -> list[str]:
            return [
                o.client_order_id
                for o in self.venue.open_orders()
                if o.client_order_id is not None and is_ours(o.client_order_id, SLEEVE)
            ]

        with contextlib.suppress(VenueError):
            for order_id in ours():
                with contextlib.suppress(VenueError):
                    self.venue.cancel(order_id)
            deadline = self.clock.now_ns() + self.settings.order_wait_ns
            while ours() and self.clock.now_ns() < deadline:
                self._step(deadline)
            return ours()
        return ["unknown: the venue could not be asked"]


def may_open_position(orders: dict[str, Any] | None) -> dict[str, Any] | None:
    """Per side tested: the pairs the account may open a position in, those it was refused, and those unknown.

    None when no order was sent: nothing was tested. Classes and pair names only, never the broker's words.
    """
    if not orders or not orders.get("attempted"):
        return None
    pairs: dict[str, Any] = orders["pairs"]
    return {
        "sides_tested": [SIDE_TESTED.value],
        SIDE_TESTED.value: {
            "accepted": [pair for pair, found in pairs.items() if found["verdict"] == ACCEPTED],
            "refused": {pair: found["reason_class"] for pair, found in pairs.items() if found["verdict"] == REJECTED},
            "no_verdict": [pair for pair, found in pairs.items() if found["verdict"] == NO_ANSWER],
        },
    }


def proposed_config(report: dict[str, Any], date: str) -> dict[str, Any]:
    """The content the probe proposes for config/venues/ibkr.yaml. Decisions stay null: they are the owner's.

    `resolves` lists the forms the gateway describes, which is not the forms the account may trade. The update
    interval is whatever this run's window saw, and the probe cannot know which session that was: it is written
    in by hand. `spot_position_opening` is what the broker did with the probe's own order, by side: the probe
    buys, so the sell side stays null. `good_pricing_min_size` is the size the broker's odd-lot warning named.
    """
    pairs = []
    for pair, found in report["pairs"].items():
        forms = [name for name, form in found["forms"].items() if form["resolves"]]
        if not forms or "sampled_as" not in found:
            continue
        chosen = found["forms"][found["sampled_as"]]
        gaps = (found["quotes"] or {}).get("gap_us")
        orders = ((report.get("orders") or {}).get("pairs") or {}).get(pair) or {}
        ack_us = orders.get("ack_us") if orders.get("verdict") == ACCEPTED else None  # a rejection is no round trip
        spot = found["sampled_as"] == InstrumentKind.CASH.value.lower()
        bought = {ACCEPTED: "accepted", REJECTED: "refused"}.get(orders.get("verdict", "")) if spot else None
        pairs.append(
            {
                "pair": pair,
                "resolves": forms,
                "price_scale": chosen["price_scale"],
                "price_increment": {name: found["forms"][name]["price_increment"] for name in forms},
                "update_interval_ms": None if gaps is None else gaps["median"] // 1_000,
                "update_interval_session": None,
                "depth_levels": (found["depth"] or {}).get("levels") or None,
                "min_order_size": chosen["min_order_size"],
                "size_increment": chosen["size_increment"],
                "good_pricing_min_size": (orders.get("odd_lot_minimum") or {}).get("size"),
                "spot_position_opening": {
                    "buy": bought,
                    "buy_refusal": orders.get("reason_class") if bought == "refused" else None,
                    "sell": None,
                    "sell_refusal": None,
                },
                "commission": {"rate_bps": None, "minimum": None, "currency": None},
                "order_round_trip_ms": {"median": None if ack_us is None else ack_us // 1_000, "p99": None},
            }
        )
    return {
        "venue": "ibkr_paper",
        "status": "partly_measured",
        "probed_at": date,
        "probe_record": None,
        "access_route": None,
        "instrument_form": None,
        "good_pricing_min_size_source": (
            "broker_odd_lot_warning" if any(pair["good_pricing_min_size"] for pair in pairs) else None
        ),
        "pairs": pairs,
        "weekly_login": {"required": None, "observed_behaviour": None},
    }


# ---- command line --------------------------------------------------------------------------------------------------


def _fake_venue(pairs: Sequence[str], window_ns: int) -> tuple[Venue, Clock]:
    """A fake broker with a moving book for each pair, for a dry run that needs no gateway."""
    from hftlab.venues.fake import FakeConfig, build

    start, interval = 1_760_000_000 * SECOND, 5_000_000
    clock = SimClock(start)
    instruments = []
    for pair in pairs:
        yen = pair.endswith(".JPY")
        spot = {"price_scale": 3 if yen else 5, "min_order_size": 20_000, "size_increment": 1_000, "price_increment": 5}
        instruments.append(Instrument.from_pair(pair, kind=InstrumentKind.CASH, **spot))
        if not yen:  # one pair without a CFD, so the report shows what "does not resolve" looks like
            instruments.append(Instrument.from_pair(pair, kind=InstrumentKind.CFD, **{**spot, "min_order_size": 1_000}))
    broker = build(clock, instruments, FakeConfig(depth_levels=5), seed=1, auto_advance=True)
    count = len(pairs) * (window_ns + 30 * SECOND) // interval + 1  # enough for every window, with room to spare
    for pair in pairs:
        mid = 151_230 if pair.endswith(".JPY") else 108_505
        walk = {"start_ns": start, "count": count, "interval_ns": interval, "mid": mid, "spread": 10, "step": 5}
        broker.market.generate_quotes(pair, **walk)
    return broker, clock


def _real_venue(host: str, port: int, client_id: int) -> tuple[Venue, Clock]:
    from hftlab.venues.ibkr.adapter import IbkrSettings, IbkrVenue
    from hftlab.venues.ibkr.client import IbAsyncClient

    clock = WallClock()
    return IbkrVenue(clock=clock, client=IbAsyncClient(), settings=IbkrSettings(host, port, client_id)), clock


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hftlab.venues.ibkr.probe", description="Measure the paper account.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, help="IB Gateway paper 4002 (default) or TWS paper 7497")
    p.add_argument("--client-id", type=int, default=78, help="an API client id nothing else is using (default 78)")
    p.add_argument("--pairs", default="EUR.USD,USD.JPY", help="comma-separated pairs (default EUR.USD,USD.JPY)")
    p.add_argument("--window-seconds", type=int, default=30, help="how long to sample quotes per pair (default 30)")
    p.add_argument("--orders", action="store_true", help="also place and cancel one far-away order per pair")
    p.add_argument("--out-dir", type=Path, default=Path("var/probe"), help="where the report is written")
    p.add_argument("--write-config", action="store_true", help=f"overwrite {CONFIG_PATH} with the proposed content")
    p.add_argument("--config-path", type=Path, default=CONFIG_PATH, help=argparse.SUPPRESS)
    p.add_argument("--fake", action="store_true", help="dry run against the built-in fake broker; no gateway needed")
    return p


def write_config(path: Path, proposed: dict[str, Any], report_name: str) -> None:
    header = (
        "# Capability file for the Interactive Brokers paper account.\n"
        f"# Written by `python -m hftlab.venues.ibkr.probe --write-config` from {report_name}.\n"
        "# Measured values only, and only partly measured: `resolves` is what the gateway describes, not what the\n"
        "# account may trade, and `update_interval_ms` is this run's window: write the session it was taken in\n"
        "# into `update_interval_session` by hand. `spot_position_opening` is what the broker did with the probe's\n"
        "# own orders.\n"
        "# `access_route`, `instrument_form`, `probe_record` and the commission stay null until the owner records\n"
        "# the decisions and a fill has shown the commission. `status` is one of: " + ", ".join(CONFIG_STATUSES) + ".\n"
    )
    path.write_text(header + yaml.safe_dump(proposed, sort_keys=False), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pairs = tuple(check_pair(pair.strip()) for pair in args.pairs.split(",") if pair.strip())
    except ValueError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not pairs or args.window_seconds < 1:
        print("probe: at least one pair and a window of at least one second are needed", file=sys.stderr)
        return EXIT_REFUSED
    settings = ProbeSettings(pairs=pairs, window_ns=args.window_seconds * SECOND, orders=args.orders)
    if args.fake:
        venue, clock = _fake_venue(pairs, settings.window_ns)
    else:
        venue, clock = _real_venue(args.host, args.port, args.client_id)
    try:
        report = Probe(venue, clock, settings).run()
    except PaperOnlyError as exc:
        print(f"probe: refused by the paper-only lock: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except VenueError as exc:
        print(f"probe: the venue could not be probed: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    prefix = "ibkr" if venue.name == "ibkr_paper" else venue.name
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / report_name(prefix, report["probed_at"])
    text = json.dumps(report, indent=2)
    print(text)
    kept = keep(out, text + "\n")
    if kept:
        print(f"probe: report written to {out}", file=sys.stderr)
    else:
        print(f"probe: {out} already exists and is left as it is; this report is on stdout only", file=sys.stderr)
    if args.write_config and not kept:
        print(f"probe: {args.config_path} left as it is: the report it would cite was not kept", file=sys.stderr)
    elif args.write_config and (report["upstream_link_down"] or report["competing_session"]):
        print(f"probe: {args.config_path} left as it is: nothing was measured", file=sys.stderr)
    elif args.write_config:
        if args.fake and args.config_path == CONFIG_PATH:
            print("probe: a dry run never overwrites the real capability file", file=sys.stderr)
            return EXIT_REFUSED
        write_config(args.config_path, report["proposed_config"], out.name)
        print(f"probe: {args.config_path} overwritten with the proposed content", file=sys.stderr)
    orders = report["orders"] or {}
    if orders.get("left_working") or orders.get("positions_changed"):
        print("probe: ATTENTION: an order is still working or a position changed. Check the account.", file=sys.stderr)
        return EXIT_ATTENTION
    if report["upstream_link_down"]:
        print(f"probe: {DOWN_REASON}. This report measures nothing; see the runbook.", file=sys.stderr)
        return EXIT_UPSTREAM_DOWN
    if report["competing_session"]:
        print(f"probe: {COMPETING_REASON}. This report holds no quotes; see the runbook.", file=sys.stderr)
        return EXIT_COMPETING_SESSION
    return 0 if kept else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
